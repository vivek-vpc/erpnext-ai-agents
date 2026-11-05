"""Telegram, WhatsApp, console, inbox: payloads, security checks, and a button press end to end."""
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from erp_agents.core.api.app import create_app
from ai_munimji_engine.core.approvals.channels.base import decode_action, encode_action
from erp_agents.core.approvals.channels.registry import build_channels, register_channel_type
from erp_agents.core.approvals.channels.simple import ConsoleChannel, InboxChannel
from erp_agents.core.approvals.channels.telegram import TelegramChannel
from erp_agents.core.approvals.channels.whatsapp import WhatsAppChannel
from erp_agents.core.approvals.inbound import process_inbound
from ai_munimji_engine.core.approvals.models import ApprovalOption, ApprovalRecord, ApprovalRequest, InboundEvent, Section
from erp_agents.core.erp.mock import MockERP

from conftest import FakeHTTP

TG_SECRET = "s3cret"
WA_SECRET = "appsecret"


def rec(title="Bolt <M8> & co", options=None, long=False):
    from datetime import datetime, timezone
    lines = ["x" * 200] * (40 if long else 2)
    req = ApprovalRequest(
        kind="inventory.reorder", title=title, headline="short headline", severity="HIGH",
        sections=[Section(heading="Situation", lines=lines)],
        options=options or [
            ApprovalOption(action="approve", label="Start reorder"),
            ApprovalOption(action="snooze", label="Remind in 3d", description="later"),
            ApprovalOption(action="dismiss", label="Ignore"),
            ApprovalOption(action="details", label="Details", terminal=False),
        ])
    return ApprovalRecord(id="abc123def4", kind=req.kind, status="pending", request=req, created_at=datetime.now(timezone.utc))


# ------------------------------------------------------------------ shared
def test_action_codec_roundtrip_and_size_limit():
    data = encode_action("abc123def4", "approve")
    assert decode_action(data) == ("abc123def4", "approve") and len(data.encode()) <= 64
    assert decode_action("garbage") is None and decode_action("ap||") is None


def test_registry_builds_enabled_channels_and_accepts_new_types():
    chans = build_channels({"console": {"enabled": True}, "inbox": {"enabled": False}})
    assert list(chans) == ["console"]
    register_channel_type("custom", lambda **kw: ConsoleChannel())
    assert "custom" in build_channels({"custom": {}})
    with pytest.raises(ValueError):
        build_channels({"carrier_pigeon": {}})


# ------------------------------------------------------------------ Telegram
def tg(http=None, mode="polling"):
    return TelegramChannel("123:TOKEN", webhook_secret=TG_SECRET, mode=mode, http=http or FakeHTTP())


def test_telegram_message_has_buttons_and_escapes_html():
    http = FakeHTTP()
    res = tg(http).send_approval(rec(), "555")
    assert res.ok and res.external_id
    url, payload, _ = http.calls[0]
    assert url.endswith("/sendMessage") and payload["chat_id"] == "555" and payload["parse_mode"] == "HTML"
    assert "&lt;M8&gt; &amp; co" in payload["text"] and "<M8>" not in payload["text"]
    buttons = [b for row in payload["reply_markup"]["inline_keyboard"] for b in row]
    assert [b["text"] for b in buttons] == ["Start reorder", "Remind in 3d", "Ignore", "Details"]
    assert all(len(b["callback_data"].encode()) <= 64 for b in buttons)
    assert decode_action(buttons[0]["callback_data"]) == ("abc123def4", "approve")


def test_telegram_truncates_long_messages():
    http = FakeHTTP()
    tg(http).send_approval(rec(long=True), "555")
    assert len(http.calls[0][1]["text"]) <= 4096


def test_telegram_failures_are_returned_not_raised():
    http = FakeHTTP()
    http.fail_next.append(("sendMessage", 400, {"ok": False, "error_code": 400, "description": "chat not found"}))
    res = tg(http).send_approval(rec(), "555")
    assert not res.ok and "chat not found" in res.error
    assert not tg().send_approval(rec(), None).ok


def test_telegram_webhook_requires_the_secret():
    ch = tg()
    assert ch.verify_webhook({"X-Telegram-Bot-Api-Secret-Token": TG_SECRET}, {}, b"")
    assert not ch.verify_webhook({"X-Telegram-Bot-Api-Secret-Token": "nope"}, {}, b"")
    assert not ch.verify_webhook({}, {}, b"")
    assert not TelegramChannel("t", webhook_secret=None).verify_webhook({"X-Telegram-Bot-Api-Secret-Token": ""}, {}, b"")


def test_telegram_polling_parses_updates_and_advances_offset():
    http = FakeHTTP()
    ch = tg(http)
    http.updates = [{"update_id": 7, "callback_query": {"id": "cq1", "from": {"id": 555}, "data": "ap|abc123def4|approve",
                                                        "message": {"message_id": 9, "chat": {"id": 555}}}},
                    {"update_id": 8, "message": {"text": "hello"}}]
    events = ch.poll_once(timeout=0)
    assert len(events) == 1 and events[0].sender == "555" and events[0].action == "approve" and events[0].ack_token == "cq1"
    ch.poll_once(timeout=0)
    assert http.calls[-1][1]["offset"] == 9


# ------------------------------------------------------------------ WhatsApp
def wa(http=None):
    return WhatsAppChannel("TOKEN", "PNID", app_secret=WA_SECRET, verify_token="vt", http=http or FakeHTTP())


def test_whatsapp_sends_text_then_list_for_four_options():
    http = FakeHTTP()
    res = wa(http).send_approval(rec(), "919800000000")
    assert res.ok
    (u1, p1, h1), (u2, p2, _) = http.calls
    assert "/PNID/messages" in u1 and h1["Authorization"] == "Bearer TOKEN"
    assert p1["type"] == "text" and "*Bolt <M8> & co*" in p1["text"]["body"]
    assert p2["type"] == "interactive" and p2["interactive"]["type"] == "list"
    rows = p2["interactive"]["action"]["sections"][0]["rows"]
    assert [r["title"] for r in rows] == ["Start reorder", "Remind in 3d", "Ignore", "Details"]
    assert all(len(r["title"]) <= 24 and len(r["id"]) <= 200 for r in rows)


def test_whatsapp_uses_buttons_for_three_or_fewer_and_trims_titles():
    http = FakeHTTP()
    opts = [ApprovalOption(action="approve", label="A very long button label here"), ApprovalOption(action="no", label="No")]
    wa(http).send_approval(rec(options=opts), "919800000000")
    inter = http.calls[1][1]["interactive"]
    assert inter["type"] == "button"
    assert all(len(b["reply"]["title"]) <= 20 for b in inter["action"]["buttons"])


def test_whatsapp_closed_window_error_is_reported_as_failed_delivery():
    http = FakeHTTP()
    http.fail_next.append(("messages", 400, {"error": {"message": "Re-engagement message", "code": 131047}}))
    res = wa(http).send_approval(rec(), "919800000000")
    assert not res.ok and "131047" in res.error and len(http.calls) == 1      # did not go on to send buttons


def test_whatsapp_falls_back_to_template_outside_the_24h_window():
    http = FakeHTTP()
    http.fail_next.append(("messages", 400, {"error": {"message": "Re-engagement message", "code": 131047}}))
    ch = WhatsAppChannel("TOKEN", "PNID", app_secret=WA_SECRET, http=http, template_name="stock_approval", template_language="en_US",
                         template_first=False)
    record = rec()
    record.request.headline = "Bolt M8 stock low\nreorder soon"
    res = ch.send_approval(record, "919800000000")
    assert res.ok and len(http.calls) == 2
    sent = http.calls[1][1]
    assert sent["type"] == "template" and sent["template"]["name"] == "stock_approval" and sent["template"]["language"] == {"code": "en_US"}
    body, *buttons = sent["template"]["components"]
    assert "\n" not in body["parameters"][0]["text"] and body["parameters"][0]["text"].startswith("Bolt M8 stock low reorder soon")
    assert [(b["index"], b["parameters"][0]["payload"]) for b in buttons] == [
        ("0", "ap|abc123def4|approve"), ("1", "ap|abc123def4|snooze"), ("2", "ap|abc123def4|dismiss")]


def test_whatsapp_template_goes_first_then_the_full_text():
    http = FakeHTTP()
    ch = WhatsAppChannel("TOKEN", "PNID", http=http, template_name="stock_approval")
    record = rec()
    res = ch.send_approval(record, "919800000000")
    assert res.ok and [c[1]["type"] for c in http.calls] == ["template", "text"]        # no second set of buttons
    var = http.calls[0][1]["template"]["components"][0]["parameters"][0]["text"]
    assert var.startswith("short headline | x") and "\n" not in var and len(var) <= 900


def test_each_request_kind_can_use_its_own_template_or_none():
    http = FakeHTTP()
    ch = WhatsAppChannel("TOKEN", "PNID", http=http, template_name="stock_approval",
                         kind_templates={"procurement.po": "po_approval", "procurement.manual": ""})

    def kind(k):
        r = rec()
        r.kind = k
        return r

    ch.send_approval(kind("inventory.reorder"), "919800000000")      # default template
    ch.send_approval(kind("procurement.po"), "919800000000")         # its own template
    ch.send_approval(kind("procurement.manual"), "919800000000")     # "" = no template: plain messages
    sent = [(c[1]["type"], c[1].get("template", {}).get("name")) for c in http.calls]
    assert sent == [("template", "stock_approval"), ("text", None), ("template", "po_approval"), ("text", None), ("text", None), ("interactive", None)]


def test_purchase_order_summary_uses_recommendation_and_other_vendors():
    from erp_agents.core.approvals.channels.whatsapp import template_summary
    record = rec()
    record.request.headline = "Order 500 pcs of Bolt from Alpha for 5,000.00."
    record.request.sections = [
        Section(heading="Recommendation", lines=["Order 500 pcs from Alpha.", "Delivery in 3 days.", "3 days before you run out.", "Draft PO."]),
        Section(heading="Stock position", lines=["On hand: 240."]),
        Section(heading="Why this vendor", lines=["Score 88 of 100.", "Total cost lowest."]),
        Section(heading="Other vendors", lines=["Beta: arrives too late.", "Gamma: costs more.", "Delta: no price."]),
    ]
    assert template_summary(record.request) == ("Order 500 pcs of Bolt from Alpha for 5,000.00. | Order 500 pcs from Alpha. | Delivery in 3 days. | "
                                                "3 days before you run out. | Score 88 of 100. | Beta: arrives too late. | Gamma: costs more.")


def test_template_summary_is_one_line_with_the_key_facts():
    from erp_agents.core.approvals.channels.whatsapp import template_summary
    record = rec()
    record.request.headline = "Bolt M8:\n 40 pcs on hand,   5 days left."
    record.request.sections = [
        Section(heading="Situation", lines=["On hand: 40.", "Lasts ~5 days.", "Replenishment in 7 days.", "Fourth line is left out."]),
        Section(heading="Why you are seeing this", lines=["Cover is below your minimum.", "Second reason is left out."]),
        Section(heading="Suggested action", lines=["Suggested order: about 300 pcs.", "How: maths."]),
    ]
    text = template_summary(record.request)
    assert text == ("Bolt M8: 40 pcs on hand, 5 days left. | On hand: 40. | Lasts ~5 days. | Replenishment in 7 days. | "
                    "Cover is below your minimum. | Suggested order: about 300 pcs.")


def test_failed_template_falls_back_to_normal_messages():
    http = FakeHTTP()
    http.fail_next.append(("messages", 400, {"error": {"message": "Template name does not exist", "code": 132001}}))
    ch = WhatsAppChannel("TOKEN", "PNID", http=http, template_name="not_approved_yet")
    res = ch.send_approval(rec(), "919800000000")
    assert res.ok and [c[1]["type"] for c in http.calls] == ["template", "text", "interactive"]


def test_whatsapp_template_is_not_used_for_other_errors():
    http = FakeHTTP()
    http.fail_next.append(("messages", 400, {"error": {"message": "Invalid token", "code": 190}}))
    ch = WhatsAppChannel("TOKEN", "PNID", http=http, template_name="stock_approval", template_first=False)
    assert not ch.send_approval(rec(), "919800000000").ok and len(http.calls) == 1


def test_whatsapp_parses_template_quick_reply_press():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "919800000000", "type": "button", "button": {"text": "Approve", "payload": "ap|abc123def4|approve"}},
        {"from": "919800000000", "type": "button", "button": {"text": "?", "payload": "junk"}},
    ]}}]}]}
    events = wa().parse_webhook(payload)
    assert [(e.approval_id, e.action) for e in events] == [("abc123def4", "approve")]


def test_enabled_flag_from_environment_strings():
    assert list(build_channels({"console": {"enabled": "${WHATSAPP_TOKEN}"}})) == ["console"]    # unexpanded text counts as set
    assert build_channels({"console": {"enabled": ""}}) == {}
    assert build_channels({"console": {"enabled": "false"}}) == {}


def sign(body: bytes, secret=WA_SECRET):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_whatsapp_signature_and_handshake():
    ch = wa()
    body = b'{"x":1}'
    assert ch.verify_webhook({"X-Hub-Signature-256": sign(body)}, {}, body)
    assert not ch.verify_webhook({"X-Hub-Signature-256": sign(body, "wrong")}, {}, body)
    assert not ch.verify_webhook({}, {}, body)
    assert ch.handle_verification({"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"}) == (200, "42")
    assert ch.handle_verification({"hub.mode": "subscribe", "hub.verify_token": "bad", "hub.challenge": "42"})[0] == 403


def test_whatsapp_parses_button_and_list_replies():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "919800000000", "type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": "ap|abc123def4|snooze"}}},
        {"from": "919800000000", "type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": "ap|abc123def4|approve"}}},
        {"from": "919800000000", "type": "text", "text": {"body": "hi"}},
    ]}}]}]}
    events = wa().parse_webhook(payload)
    assert [(e.action, e.text) for e in events] == [("snooze", None), ("approve", None), (None, "hi")]


# ------------------------------------------------------------------ console + inbox
def test_console_prints_with_decision_hint_and_inbox_always_delivers():
    out = []
    c = ConsoleChannel(writer=out.append)
    assert c.send_approval(rec(), None).ok and "erp-agents approvals decide abc123def4" in out[0]
    pushed = []
    i = InboxChannel(push_hook=lambda r, a: pushed.append(r.id))
    assert i.send_approval(rec(), "owner").ok and pushed == ["abc123def4"]
    assert InboxChannel(push_hook=lambda r, a: 1 / 0).send_approval(rec(), None).ok      # push failure never blocks the inbox


# ------------------------------------------------------------------ end to end: a button press changes the system
@pytest.fixture
def wired(make_app, clock):
    http = FakeHTTP()
    channels = {
        "telegram": TelegramChannel("123:TOKEN", webhook_secret=TG_SECRET, http=http),
        "whatsapp": WhatsAppChannel("TOKEN", "PNID", app_secret=WA_SECRET, verify_token="vt", http=http),
        "inbox": InboxChannel(),
    }
    cfg = {"approvals": {"strategy": "all", "recipients": [{
        "id": "owner", "name": "Owner", "roles": ["admin", "approver"],
        "channels": [{"type": "telegram", "address": "555"}, {"type": "whatsapp", "address": "919800000000"}, {"type": "inbox"}]}]},
        "auth": {"tokens": []}}
    app = make_app(erp=MockERP(today=clock().date()), channels=channels, cfg=cfg)
    return app, http


def test_alert_reaches_all_three_channels(wired):
    app, http = wired
    app.inventory.run(item_codes=["BOLT-M8"])
    assert len(http.calls_to("api.telegram.org")) == 1
    assert len(http.calls_to("graph.facebook.com")) == 2         # text + interactive
    assert len(app.approvals.list("pending")) == 1


def tg_press(app, approval_id, action, sender="555", secret=TG_SECRET):
    client = TestClient(create_app(app))
    body = {"update_id": 1, "callback_query": {"id": "cq9", "from": {"id": int(sender)}, "data": encode_action(approval_id, action),
                                              "message": {"message_id": 77, "chat": {"id": int(sender)}}}}
    return client.post("/webhooks/telegram", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": secret})


def test_telegram_button_press_approves_and_retires_other_buttons(wired):
    app, http = wired
    app.inventory.run(item_codes=["BOLT-M8"])
    approval_id = app.approvals.list("pending")[0].id
    assert tg_press(app, approval_id, "approve").status_code == 200
    assert app.approvals.get(approval_id).decision.channel == "telegram"
    assert app.workorders.list()[0]["payload"]["item_code"] == "BOLT-M8"
    assert http.calls_to("answerCallbackQuery")                    # spinner dismissed
    assert http.calls_to("editMessageReplyMarkup")                 # buttons removed from the Telegram message


def test_details_button_returns_numbers_and_keeps_request_open(wired):
    app, http = wired
    app.inventory.run(item_codes=["BOLT-M8"])
    approval_id = app.approvals.list("pending")[0].id
    tg_press(app, approval_id, "details")
    texts = [c[1].get("text", "") for c in http.calls_to("sendMessage")]
    assert any("RULES CHECKED" in t.upper() or "Rules checked" in t for t in texts)
    assert app.approvals.get(approval_id).status == "pending"


def test_stranger_and_bad_secret_cannot_decide(wired):
    app, http = wired
    app.inventory.run(item_codes=["BOLT-M8"])
    approval_id = app.approvals.list("pending")[0].id
    assert tg_press(app, approval_id, "approve", sender="999").status_code == 200     # processed, but ignored
    assert app.approvals.get(approval_id).status == "pending" and app.workorders.list() == []
    assert tg_press(app, approval_id, "approve", secret="wrong").status_code == 401
    assert app.approvals.get(approval_id).status == "pending"


def test_whatsapp_reply_and_same_request_answered_twice(wired):
    app, http = wired
    app.inventory.run(item_codes=["BOLT-M8"])
    approval_id = app.approvals.list("pending")[0].id
    client = TestClient(create_app(app))

    def reply(action, secret=WA_SECRET):
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
            {"from": "919800000000", "type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": encode_action(approval_id, action)}}}]}}]}]}).encode()
        return client.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": sign(body, secret), "Content-Type": "application/json"})

    assert reply("approve", secret="forged").status_code == 401
    assert reply("approve").status_code == 200
    assert reply("dismiss").status_code == 200                      # late second answer from another device
    assert app.approvals.get(approval_id).decision.action == "approve"
    assert len(app.workorders.list()) == 1
    assert any("already" in (c[1].get("text", {}).get("body", "") if isinstance(c[1].get("text"), dict) else "").lower() or
               "Recorded" in json.dumps(c[1]) for c in http.calls_to("graph.facebook.com"))
