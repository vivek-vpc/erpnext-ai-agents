"""Telegram end to end (against a faked Telegram): onboarding, who may answer, alerts and orders by button, and the bot token never leaking."""
import itertools
import logging
from types import SimpleNamespace

import pytest
import requests
import yaml
from fastapi.testclient import TestClient

from conftest import OWNER, FakeHTTP
from erp_agents.core.api.app import create_app
from erp_agents.core.approvals.channels.base import decode_action, encode_action
from erp_agents.core.approvals.channels.registry import build_channels
from erp_agents.core.approvals.channels.simple import InboxChannel
from erp_agents.core.approvals.channels.telegram import REPLY_EVERY, TelegramChannel
from erp_agents.core.approvals.channels.whatsapp import WhatsAppChannel
from erp_agents.core.approvals.inbound import process_inbound
from erp_agents.core.config import expand_env
from erp_agents.core.erp.mock import MockERP

TOKEN = "123456789:AAFakeBotTokenForTests_xyz"
SECRET = "web-hook_secret-1"
OWNER_CHAT = "555"
UPDATE_IDS = itertools.count(100)                  # Telegram gives every update its own id; one delivered again has the same (core/approvals/inbound_ledger.py)


def tg(http=None, **kw):
    return TelegramChannel(TOKEN, webhook_secret=kw.pop("secret", SECRET), http=http or FakeHTTP(), **kw)


def msg(text, sender=777, chat_type="private", bot=False):
    return {"update_id": next(UPDATE_IDS), "message": {"message_id": 5, "text": text, "from": {"id": sender, "is_bot": bot, "first_name": "Priya"}, "chat": {"id": sender, "type": chat_type}}}


def press(approval_id, action, sender=int(OWNER_CHAT)):
    return {"update_id": next(UPDATE_IDS), "callback_query": {"id": "cq1", "from": {"id": sender}, "data": encode_action(approval_id, action),
                                               "message": {"message_id": 77, "chat": {"id": sender}}}}


@pytest.fixture
def wired(make_app, clock):
    http = FakeHTTP()
    channels = {"telegram": tg(http), "inbox": InboxChannel()}
    cfg = {"approvals": {"strategy": "all", "recipients": [{"id": "owner", "name": "Owner", "roles": ["admin", "approver"],
                                                              "channels": [{"type": "telegram", "address": OWNER_CHAT}, {"type": "inbox"}]}]}}
    app = make_app(erp=MockERP(today=clock().date()), channels=channels, cfg=cfg)
    return app, http


def post_webhook(app, body, secret=SECRET):
    c = TestClient(create_app(app))
    return c.post("/webhooks/telegram", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": secret} if secret else {})


def sent_to(http, chat):
    return [p for u, p, _ in http.calls if u.endswith("/sendMessage") and str(p.get("chat_id")) == str(chat)]


# ------------------------------------------------------------------ the bot token is a password
class ExplodingHTTP:
    """The way `requests` fails: the error quotes the address, and the address contains the bot token."""

    def post(self, url, json=None, headers=None, timeout=None):
        raise requests.ConnectionError(f"HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded with url: {url.split('.org')[1]}")


def test_a_network_failure_never_exposes_the_bot_token(caplog):
    ch = tg(ExplodingHTTP())
    from test_channels import rec
    results = [ch.send_text("1", "x"), ch.send_approval(rec(), "1")]
    for r in results:
        assert not r.ok and "could not reach Telegram" in r.error
        assert TOKEN not in r.error and TOKEN.split(":")[1] not in r.error and "<bot-token>" in r.error
    with caplog.at_level(logging.INFO):
        ch.update_outcome(rec(), "9", "1", "done")
        ch.acknowledge(SimpleNamespace(ack_token="c1", sender="1"), "ok")
    assert TOKEN.split(":")[1] not in " ".join(r.getMessage() for r in caplog.records)
    with pytest.raises(RuntimeError) as e:
        ch.poll_once(timeout=0)
    assert TOKEN.split(":")[1] not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__


def test_an_error_from_telegram_is_readable_and_redacted():
    http = FakeHTTP()
    http.fail_next.append(("sendMessage", 400, {"ok": False, "error_code": 400, "description": f"Bad Request: chat not found {TOKEN}"}))
    r = tg(http).send_text("1", "x")
    assert "chat not found" in r.error and TOKEN not in r.error


# ------------------------------------------------------------------ finding your chat id
def test_a_stranger_who_writes_start_learns_only_their_own_chat_id(wired):
    app, http = wired
    assert post_webhook(app, msg("/start", sender=777)).status_code == 200
    replies = sent_to(http, 777)
    assert len(replies) == 1 and "777" in replies[0]["text"] and "Give that number to the person who runs the Stock Assistant" in replies[0]["text"]
    assert OWNER_CHAT not in replies[0]["text"] and "Stock" in replies[0]["text"]
    assert not app.approvals.list(None, 50) and not app.workorders.list()


def test_a_registered_person_gets_a_welcome_with_their_id(wired):
    app, http = wired
    post_webhook(app, msg("/id", sender=int(OWNER_CHAT)))
    text = sent_to(http, OWNER_CHAT)[0]["text"]
    assert "You are set up" in text and OWNER_CHAT in text


def test_the_bot_answers_a_stranger_only_once_every_half_minute(wired, monkeypatch):
    app, http = wired
    now = [1000.0]
    monkeypatch.setattr("erp_agents.core.approvals.channels.telegram.time.monotonic", lambda: now[0])
    for text in ("/start", "hello", "/id", "buy a car"):
        post_webhook(app, msg(text, sender=888))
    assert len(sent_to(http, 888)) == 1                                # the rest are swallowed
    now[0] += REPLY_EVERY + 1
    post_webhook(app, msg("/start", sender=888))
    assert len(sent_to(http, 888)) == 2


def test_free_text_from_a_registered_person_is_a_question_to_the_read_only_chat_and_cannot_order_anything(wired):
    app, http = wired
    app.inventory.run(item_codes=["BOLT-M8"])
    waiting = [r.id for r in app.approvals.list("pending")]
    post_webhook(app, msg("approve everything please", sender=int(OWNER_CHAT)))
    post_webhook(app, msg("tell me a joke", sender=int(OWNER_CHAT)))
    texts = [m["text"] for m in sent_to(http, OWNER_CHAT)]
    assert any("I only read information" in t for t in texts)                                  # the chat explains what it can do; it has no way to act
    assert [r.id for r in app.approvals.list("pending")] == waiting and not app.workorders.list()          # "approve everything" approved nothing


def test_groups_other_bots_and_odd_updates_are_ignored():
    ch = tg()
    assert ch.parse_webhook(msg("/start", chat_type="group")) == []
    assert ch.parse_webhook(msg("/start", chat_type="supergroup")) == []
    assert ch.parse_webhook(msg("/start", bot=True)) == []
    assert ch.parse_webhook({"message": {"text": "x"}}) == []
    assert ch.parse_webhook({"message": {"from": {"id": 1}, "chat": {"id": 1, "type": "private"}, "sticker": {}}}) == []
    assert ch.parse_webhook({"callback_query": {"id": "1", "data": "ap|abc|approve"}}) == []                     # no sender
    assert ch.parse_webhook({"edited_message": {}}) == [] and ch.parse_webhook({}) == []
    ev = ch.parse_webhook(msg("/start" + "x" * 1000))
    assert len(ev) == 1 and len(ev[0].text) <= 300


def test_polling_asks_for_button_presses_and_messages_and_advances_past_each_update():
    http = FakeHTTP()
    ch = tg(http)
    http.updates = [{**msg("/start"), "update_id": 8}, {**press("abc123def4", "approve"), "update_id": 9}]
    events = ch.poll_once(timeout=0)
    assert [e.text for e in events] == ["/start", None] and events[1].action == "approve"
    sent = http.calls[0][1]
    assert sent["allowed_updates"] == ["callback_query", "message"]
    ch.poll_once(timeout=0)
    assert http.calls[-1][1]["offset"] == 10


# ------------------------------------------------------------------ addresses
@pytest.mark.parametrize("address,ok", [("555", True), ("1234567890", True), ("", False), (None, False), ("-1001234567890", False), ("@priya", False), ("priya nair", False), ("5 5", False)])
def test_only_a_persons_own_chat_id_is_an_address(address, ok):
    assert (tg().address_problem(address) is None) is ok


def test_a_recipient_without_a_usable_chat_id_is_skipped_and_reported_once(make_app, clock, caplog):
    http = FakeHTTP()
    cfg = {"approvals": {"strategy": "all", "recipients": [
        {"id": "owner", "name": "Owner", "roles": ["admin", "approver"], "channels": [{"type": "telegram", "address": ""}, {"type": "inbox"}]},
        {"id": "boss", "name": "Boss", "roles": ["approver"], "channels": [{"type": "telegram", "address": "-100999"}, {"type": "inbox"}]},
        {"id": "ok", "name": "Ok", "roles": ["approver"], "channels": [{"type": "telegram", "address": OWNER_CHAT}]}]}}
    with caplog.at_level(logging.WARNING):
        app = make_app(erp=MockERP(today=clock().date()), channels={"telegram": tg(http), "inbox": InboxChannel()}, cfg=cfg)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "recipient owner on telegram" in text and "recipient boss on telegram" in text and "group and channel ids are not supported" in text
    app.inventory.run(item_codes=["BOLT-M8"])
    assert {str(p["chat_id"]) for u, p, _ in http.calls if u.endswith("/sendMessage")} == {OWNER_CHAT}          # nobody else was tried


def test_the_other_channels_still_reach_a_person_whose_telegram_id_is_missing(make_app, clock):
    cfg = {"approvals": {"strategy": "all", "recipients": [{"id": "owner", "name": "Owner", "roles": ["admin", "approver"], "channels": [{"type": "telegram", "address": ""}, {"type": "inbox"}]}]}}
    app = make_app(erp=MockERP(today=clock().date()), channels={"telegram": tg(), "inbox": InboxChannel()}, cfg=cfg)
    app.inventory.run(item_codes=["BOLT-M8"])
    assert len(app.approvals.list("pending")) == 1


# ------------------------------------------------------------------ webhook mode
def test_webhook_registration_checks_what_telegram_requires():
    http = FakeHTTP()
    ch = tg(http)
    with pytest.raises(ValueError, match="https"):
        ch.set_webhook("http://example.com/webhooks/telegram")
    with pytest.raises(ValueError, match="letters, digits"):
        tg(secret="bad secret!").set_webhook("https://x.example/webhooks/telegram")
    with pytest.raises(ValueError, match="webhook_secret"):
        tg(secret=None).set_webhook("https://x.example/webhooks/telegram")
    ch.set_webhook("https://x.example/webhooks/telegram")
    url, payload, _ = http.calls[-1]
    assert url.endswith("/setWebhook") and payload["secret_token"] == SECRET and payload["allowed_updates"] == ["callback_query", "message"]
    with pytest.raises(ValueError):
        TelegramChannel(TOKEN, mode="carrier-pigeon")


def test_the_webhook_refuses_a_missing_or_wrong_secret_and_accepts_the_right_one(wired):
    app, http = wired
    assert post_webhook(app, msg("/start"), secret=None).status_code == 401
    assert post_webhook(app, msg("/start"), secret="nope").status_code == 401
    assert not http.calls
    assert post_webhook(app, msg("/start")).status_code == 200 and http.calls


# ------------------------------------------------------------------ the real work: alerts, reorders and orders by button
def test_a_stock_alert_arrives_with_plain_buttons_and_got_it_changes_nothing(make_app, clock):
    http = FakeHTTP()
    cfg = {"approvals": {"strategy": "all", "recipients": [{"id": "owner", "name": "Owner", "roles": ["admin", "approver"], "channels": [{"type": "telegram", "address": OWNER_CHAT}]}]}}
    app = make_app(erp=MockERP(today=clock().date()), channels={"telegram": tg(http)}, cfg=cfg)
    app.settings.update_system(OWNER, {"stock_alerts": True})
    app.inventory.run(item_codes=["RELAY-12V"])
    first, second = sent_to(http, OWNER_CHAT)[:2]
    assert "URGENT" in first["text"] and "information only" in first["text"] and "Suggested action" not in first["text"]
    labels = [b["text"] for row in first["reply_markup"]["inline_keyboard"] for b in row]
    assert labels == ["Got it", "Remind in 3d", "Details"]
    assert "Reorder suggested" in second["text"]                                         # the request follows the alert
    alert_id = decode_action(first["reply_markup"]["inline_keyboard"][0][0]["callback_data"])[0]
    assert post_webhook(app, press(alert_id, "ack")).status_code == 200
    assert app.approvals.get(alert_id).decision.action == "ack" and app.approvals.get(alert_id).decision.channel == "telegram"
    assert not app.workorders.list() and not app.erp.purchase_orders
    assert any(u.endswith("/editMessageReplyMarkup") for u, _, _ in http.calls)            # the buttons disappear from the alert


def test_hostile_item_names_cannot_break_or_inject_into_a_telegram_message(make_app, clock):
    http = FakeHTTP()
    erp = MockERP(today=clock().date())
    object.__setattr__(erp.items["BOLT-M8"].info, "name", '<b onclick="x">Bolt</b> <a href="http://evil">click</a> & <script>alert(1)</script>')
    cfg = {"approvals": {"strategy": "all", "recipients": [{"id": "owner", "name": "Owner", "roles": ["admin", "approver"], "channels": [{"type": "telegram", "address": OWNER_CHAT}]}]}}
    app = make_app(erp=erp, channels={"telegram": tg(http)}, cfg=cfg)
    app.settings.update_system(OWNER, {"stock_alerts": True})
    app.inventory.run(item_codes=["BOLT-M8"])
    for p in sent_to(http, OWNER_CHAT):
        t = p["text"]
        assert "<script>" not in t and "<a href" not in t and "<b onclick" not in t and "&lt;script&gt;" in t
        assert len(t) <= 4096 and all(len(b["callback_data"].encode()) <= 64 for row in p["reply_markup"]["inline_keyboard"] for b in row)


def test_a_reorder_and_then_the_purchase_order_can_be_approved_entirely_from_telegram(wired):
    app, http = wired
    app.inventory.run(item_codes=["BOLT-M8"])
    reorder = next(r for r in app.approvals.list("pending") if r.kind == "inventory.reorder")
    assert post_webhook(app, press(reorder.id, "approve")).status_code == 200
    app.procurement.run()
    po_req = next(r for r in app.approvals.list("pending") if r.kind == "procurement.po")
    assert any(po_req.id in b["callback_data"] for p in sent_to(http, OWNER_CHAT) for row in p.get("reply_markup", {}).get("inline_keyboard", []) for b in row)       # the order request reached Telegram
    assert post_webhook(app, press(po_req.id, "approve")).status_code == 200
    assert len(app.erp.purchase_orders) == 1
    assert app.erp.purchase_orders[0]["approved_by"] == "owner via telegram"                  # ERPNext shows who approved, and where


def test_a_stranger_pressing_a_button_changes_nothing(wired):
    app, http = wired
    app.inventory.run(item_codes=["BOLT-M8"])
    rid = app.approvals.list("pending")[0].id
    post_webhook(app, press(rid, "approve", sender=999))
    assert app.approvals.get(rid).status == "pending" and not app.workorders.list()
    assert any(u.endswith("/answerCallbackQuery") and "not registered" in (p or {}).get("text", "") for u, p, _ in http.calls)


def test_a_failed_telegram_delivery_is_retried_later(wired):
    app, http = wired
    http.fail_next.append(("sendMessage", 502, {"ok": False, "error_code": 502, "description": "Bad Gateway"}))
    app.inventory.run(item_codes=["BOLT-M8"])
    assert len(sent_to(http, OWNER_CHAT)) == 1                                              # the first try failed
    assert app.approvals.retry_failed_deliveries() >= 1
    assert len(sent_to(http, OWNER_CHAT)) == 2


def test_failover_prefers_telegram_and_uses_whatsapp_only_when_it_fails(make_app, clock):
    http = FakeHTTP()
    channels = {"telegram": tg(http), "whatsapp": WhatsAppChannel("T", "P", app_secret="s", verify_token="v", http=http)}
    cfg = {"approvals": {"strategy": "failover", "recipients": [{"id": "owner", "name": "Owner", "roles": ["admin", "approver"],
                                                                   "channels": [{"type": "telegram", "address": OWNER_CHAT}, {"type": "whatsapp", "address": "919800000000"}]}]}}
    app = make_app(erp=MockERP(today=clock().date()), channels=channels, cfg=cfg)
    app.inventory.run(item_codes=["BOLT-M8"])
    assert not [c for c in http.calls if "graph.facebook.com" in c[0]]
    http.fail_next.append(("sendMessage", 400, {"ok": False, "error_code": 400, "description": "chat not found"}))
    app.inventory.run(item_codes=["OIL-5L"])
    assert [c for c in http.calls if "graph.facebook.com" in c[0]]


# ------------------------------------------------------------------ configuration
def test_the_live_config_turns_telegram_on_only_when_the_token_exists(monkeypatch):
    raw = yaml.safe_load(open("config/settings.erpnext.yaml", encoding="utf-8"))
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_MODE", "TELEGRAM_WEBHOOK_SECRET", "OWNER_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    off = expand_env(raw)
    assert "telegram" not in build_channels({"telegram": off["channels"]["telegram"]})
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("OWNER_TELEGRAM_CHAT_ID", OWNER_CHAT)
    on = expand_env(raw)
    ch = build_channels({"telegram": on["channels"]["telegram"]})["telegram"]
    assert isinstance(ch, TelegramChannel) and ch.mode == "polling" and ch.webhook_secret is None
    assert {"type": "telegram", "address": OWNER_CHAT} in on["approvals"]["recipients"][0]["channels"]
    monkeypatch.setenv("TELEGRAM_MODE", "webhook")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    ch = build_channels({"telegram": expand_env(raw)["channels"]["telegram"]})["telegram"]
    assert ch.mode == "webhook" and ch.webhook_secret == SECRET


# ------------------------------------------------------------------ the helper commands
def run_cli(app, action, url=None):
    from erp_agents.core.cli import _telegram_action
    return _telegram_action(app, app.channels["telegram"], SimpleNamespace(action=action, url=url))


def test_status_names_the_bot_the_mode_and_whether_each_recipient_is_ready(wired, capsys):
    app, http = wired
    http.post = (lambda orig: lambda url, json=None, headers=None, timeout=None: (
        type("R", (), {"status_code": 200, "json": lambda s: {"ok": True, "result": {"username": "stock_bot", "first_name": "Stock", "url": "", "pending_update_count": 0}}})()
        if url.endswith(("/getMe", "/getWebhookInfo")) else orig(url, json=json, headers=headers, timeout=timeout)))(http.post)
    assert run_cli(app, "status") == 0
    out = capsys.readouterr().out
    assert "@stock_bot" in out and "mode: polling" in out and f"Recipient owner: chat id {OWNER_CHAT} ok" in out and TOKEN not in out


def test_test_sends_a_message_to_every_ready_recipient(wired, capsys):
    app, http = wired
    assert run_cli(app, "test") == 0
    assert "delivered" in capsys.readouterr().out and "test message" in sent_to(http, OWNER_CHAT)[0]["text"]
    http.fail_next.append(("sendMessage", 403, {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}))
    assert run_cli(app, "test") == 1
    assert "FAILED" in capsys.readouterr().out


def test_chats_lists_people_who_wrote_to_the_bot_without_marking_anything_read(wired, capsys):
    app, http = wired
    http.updates = [msg("/start", sender=4242), msg("hi", sender=4242), msg("x", sender=7, chat_type="group")]
    assert run_cli(app, "chats") == 0
    out = capsys.readouterr().out
    assert "4242" in out and "Priya" in out and out.count("chat id") >= 1 and " 7 " not in out
    sent = http.calls[-1][1]
    assert "offset" not in sent and sent["allowed_updates"] == ["message"]


# ------------------------------------------------------------------ the polling loop that `erp-agents serve` runs in the background
def test_the_background_poller_answers_messages_survives_errors_and_never_logs_the_token(make_app, clock, caplog):
    import threading
    import time as _time
    from erp_agents.core.scheduler import AgentScheduler

    class Flaky(FakeHTTP):
        """Telegram that fails twice (the way requests does, with the token in the address), then works."""

        def __init__(self):
            super().__init__()
            self.failures = 2

        def post(self, url, json=None, headers=None, timeout=None):
            if url.endswith("/getUpdates") and self.failures:
                self.failures -= 1
                raise requests.ConnectionError(f"Max retries exceeded with url: {url.split('.org')[1]}")
            if url.endswith("/getUpdates") and not self.updates:
                _time.sleep(0.02)                                      # a real long poll waits; do not spin
            return super().post(url, json=json, headers=headers, timeout=timeout)

    http = Flaky()
    ch = tg(http)
    cfg = {"approvals": {"strategy": "all", "recipients": [{"id": "owner", "name": "Owner", "roles": ["admin", "approver"], "channels": [{"type": "telegram", "address": OWNER_CHAT}]}]}}
    app = make_app(erp=MockERP(today=clock().date()), channels={"telegram": ch}, cfg=cfg)
    sched = AgentScheduler(app)
    real_wait = sched._stop.wait
    sched._stop.wait = lambda t=None: real_wait(0.01)                  # do not really sleep through the retry delay
    http.updates = [msg("/start", sender=4321)]
    with caplog.at_level(logging.WARNING):
        t = threading.Thread(target=sched._poll_telegram, args=(ch,), daemon=True)
        t.start()
        deadline = _time.time() + 5
        while _time.time() < deadline and not sent_to(http, 4321):
            _time.sleep(0.02)
        sched._stop.set()
        t.join(timeout=3)
    assert not t.is_alive()
    assert any(u.endswith("/deleteWebhook") for u, _, _ in http.calls)           # polling and a webhook cannot coexist: the poller clears it first
    assert sent_to(http, 4321) and "4321" in sent_to(http, 4321)[0]["text"]       # the message that arrived after two failures was answered
    log_text = " ".join(r.getMessage() for r in caplog.records)
    assert "telegram polling error" in log_text and TOKEN.split(":")[1] not in log_text and "<bot-token>" in log_text
