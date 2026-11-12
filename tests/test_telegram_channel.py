"""The engine's Telegram channel on its own (ai_munimji_engine.core.approvals.channels.telegram): it makes no network call, it is given the HTTP session; the bot token never leaves it;
what it accepts from Telegram and what it sends. The service's end-to-end Telegram tests (tests/test_telegram.py) run the same class through the service's wiring."""
import logging
from types import SimpleNamespace

import pytest

from conftest import FakeHTTP
from ai_munimji_engine.core.approvals.channels.base import decode_action
from ai_munimji_engine.core.approvals.channels.telegram import BOT_DESCRIPTION, KNOWN_HELP, PRODUCT, REPLY_EVERY, MemoryOffset, MemoryThrottle, TelegramChannel
from test_channels import rec

TOKEN = "123456789:AAFakeBotTokenForTests_xyz"
SECRET = "web-hook_secret-1"


def tg(http=None, **kw):
    return TelegramChannel(TOKEN, webhook_secret=kw.pop("secret", SECRET), http=http or FakeHTTP(), **kw)


def update(text="/start", sender=777, chat_type="private", bot=False, update_id=50, reply_to=None):
    message = {"message_id": 5, "text": text, "from": {"id": sender, "is_bot": bot, "first_name": "Priya"}, "chat": {"id": sender, "type": chat_type}}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": message}


def sent(http, method="sendMessage"):
    return [p for u, p, _ in http.calls if u.endswith("/" + method)]


# ------------------------------------------------------------------ the channel makes no call of its own
def test_it_needs_the_http_session_it_is_given_and_a_token_and_a_known_mode():
    with pytest.raises(ValueError, match="http is required"):
        TelegramChannel(TOKEN)
    with pytest.raises(ValueError, match="bot_token"):
        TelegramChannel("", http=FakeHTTP())
    with pytest.raises(ValueError, match="mode"):
        TelegramChannel(TOKEN, mode="carrier-pigeon", http=FakeHTTP())


def test_the_module_imports_no_http_library():
    import ast
    import inspect

    import ai_munimji_engine.core.approvals.channels.telegram as module

    imported = {a.name.split(".")[0] for n in ast.walk(ast.parse(inspect.getsource(module))) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(ast.parse(inspect.getsource(module))) if isinstance(n, ast.ImportFrom) and n.module and not n.level}
    assert not imported & {"requests", "urllib3", "httpx", "socket", "http", "aiohttp"}, imported


# ------------------------------------------------------------------ the bot token is a password
class ExplodingHTTP:
    """The way `requests` fails: the error quotes the address, and the address contains the bot token."""

    def post(self, url, json=None, headers=None, timeout=None):
        raise ConnectionError(f"HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded with url: {url.split('.org')[1]}")


def test_a_network_failure_never_exposes_the_bot_token(caplog):
    ch = tg(ExplodingHTTP())
    for result in (ch.send_text("1", "x"), ch.send_approval(rec(), "1")):
        assert not result.ok and "could not reach Telegram" in result.error
        assert TOKEN not in result.error and TOKEN.split(":")[1] not in result.error and "<bot-token>" in result.error
    with caplog.at_level(logging.INFO):
        ch.update_outcome(rec(), "9", "1", "done")
        ch.acknowledge(SimpleNamespace(ack_token="c1", sender="1"), "ok")
    assert TOKEN.split(":")[1] not in " ".join(r.getMessage() for r in caplog.records)
    with pytest.raises(RuntimeError) as raised:
        ch.poll_once(timeout=0)
    assert TOKEN.split(":")[1] not in str(raised.value) and raised.value.__cause__ is None and raised.value.__suppress_context__


def test_an_error_from_telegram_is_readable_and_redacted():
    http = FakeHTTP()
    http.fail_next.append(("sendMessage", 400, {"ok": False, "error_code": 400, "description": f"Bad Request: chat not found {TOKEN}"}))
    result = tg(http).send_text("1", "x")
    assert "chat not found" in result.error and TOKEN not in result.error


# ------------------------------------------------------------------ what it sends
def test_a_request_goes_out_with_two_buttons_to_a_row_escaped_and_within_telegrams_limits():
    http = FakeHTTP()
    result = tg(http).send_approval(rec(title='Bolt <M8> & <script>alert(1)</script>', long=True), "555")
    assert result.ok and result.external_id
    [message] = sent(http)
    assert message["parse_mode"] == "HTML" and message["chat_id"] == "555" and len(message["text"]) <= 4096
    assert "<script>" not in message["text"] and "&lt;script&gt;" in message["text"]
    rows = message["reply_markup"]["inline_keyboard"]
    assert [len(r) for r in rows] == [2, 2]
    assert all(len(b["callback_data"].encode()) <= 64 for r in rows for b in r)
    assert decode_action(rows[0][0]["callback_data"]) == ("abc123def4", "approve")


def test_it_will_not_send_without_a_chat_id():
    http = FakeHTTP()
    ch = tg(http)
    assert not ch.send_text(None, "x").ok and not ch.send_approval(rec(), "").ok
    assert not http.calls


def test_an_answered_request_loses_its_buttons_and_gets_the_outcome_under_it_and_a_failure_changes_nothing(caplog):
    http = FakeHTTP()
    tg(http).update_outcome(rec(), "42", "555", "Recorded: Start reorder.")
    assert [u.rsplit("/", 1)[1] for u, _, _ in http.calls] == ["editMessageReplyMarkup", "sendMessage"]
    assert sent(http)[0]["reply_to_message_id"] == 42 and sent(http, "editMessageReplyMarkup")[0]["reply_markup"] == {"inline_keyboard": []}
    broken = FakeHTTP()
    broken.fail_next.append(("editMessageReplyMarkup", 400, {"ok": False, "error_code": 400, "description": "message is not modified"}))
    with caplog.at_level(logging.INFO):
        tg(broken).update_outcome(rec(), "42", "555", "x")                                   # cosmetic: must not raise
    assert tg(FakeHTTP()).update_outcome(rec(), None, "555", "x") is None


def test_a_button_press_is_answered_on_the_button_and_a_message_in_the_chat():
    http = FakeHTTP()
    ch = tg(http)
    ch.acknowledge(SimpleNamespace(ack_token="cq9", sender="5"), "Done" * 100)
    ch.acknowledge(SimpleNamespace(ack_token=None, sender="5"), "Hello")
    [callback] = sent(http, "answerCallbackQuery")
    assert callback["callback_query_id"] == "cq9" and len(callback["text"]) <= 190
    assert sent(http)[0]["text"] == "Hello" and sent(http)[0]["chat_id"] == "5"


def test_the_application_can_shorten_the_cosmetic_calls():
    seen = []

    class Timed(FakeHTTP):
        def post(self, url, json=None, headers=None, timeout=None):
            seen.append((url.rsplit("/", 1)[1], timeout))
            return super().post(url, json=json, headers=headers, timeout=timeout)

    ch = tg(Timed(), timeout_for=lambda method: 4 if method in ("editMessageReplyMarkup", "answerCallbackQuery") else 0)
    ch.update_outcome(rec(), "1", "5", "x")
    ch.send_text("5", "y")
    assert seen == [("editMessageReplyMarkup", 4), ("sendMessage", 15), ("sendMessage", 15)]     # the outcome has two calls; 0 means "the default"


# ------------------------------------------------------------------ what it accepts
def test_groups_other_bots_and_odd_updates_are_ignored():
    ch = tg()
    assert ch.parse_webhook(update(chat_type="group")) == [] and ch.parse_webhook(update(chat_type="supergroup")) == []
    assert ch.parse_webhook(update(bot=True)) == []
    assert ch.parse_webhook({"message": {"text": "x"}}) == []
    assert ch.parse_webhook({"message": {"from": {"id": 1}, "chat": {"id": 1, "type": "private"}, "sticker": {}}}) == []
    assert ch.parse_webhook({"callback_query": {"id": "1", "data": "ap|abc|approve"}}) == []                     # no sender
    assert ch.parse_webhook({"callback_query": {"id": "1", "from": {"id": 1}, "data": "garbage"}}) == []
    assert ch.parse_webhook({"edited_message": {}}) == [] and ch.parse_webhook({}) == []
    [event] = ch.parse_webhook(update("/start" + "x" * 1000))
    assert len(event.text) <= 300


def test_a_press_and_a_reply_become_events_carrying_the_updates_own_id():
    ch = tg()
    [press] = ch.parse_webhook({"update_id": 9, "callback_query": {"id": "cq1", "from": {"id": 555}, "data": "ap|abc123def4|approve"}})
    assert (press.channel, press.sender, press.approval_id, press.action, press.ack_token, press.external_id) == ("telegram", "555", "abc123def4", "approve", "cq1", "9")
    [reply] = ch.parse_webhook(update("40", sender=555, update_id=10, reply_to=1001))
    assert (reply.text, reply.reply_to, reply.external_id) == ("40", "1001", "10")
    [no_id] = ch.parse_webhook({k: v for k, v in update().items() if k != "update_id"})
    assert no_id.external_id is None


def test_the_webhook_secret_is_checked_in_constant_time_and_a_missing_one_refuses_everything():
    ch = tg()
    assert ch.verify_webhook({"X-Telegram-Bot-Api-Secret-Token": SECRET}, {}, b"") and ch.verify_webhook({"x-telegram-bot-api-secret-token": SECRET}, {}, b"")
    assert not ch.verify_webhook({"X-Telegram-Bot-Api-Secret-Token": "nope"}, {}, b"") and not ch.verify_webhook({}, {}, b"")
    assert not tg(secret=None).verify_webhook({"X-Telegram-Bot-Api-Secret-Token": ""}, {}, b"")


@pytest.mark.parametrize("address,ok", [("555", True), ("1234567890", True), ("", False), (None, False), ("-1001234567890", False), ("@priya", False), ("priya nair", False), ("5 5", False)])
def test_only_a_persons_own_chat_id_is_an_address(address, ok):
    assert (tg().address_problem(address) is None) is ok


def test_webhook_registration_checks_what_telegram_requires():
    http = FakeHTTP()
    with pytest.raises(ValueError, match="https"):
        tg(http).set_webhook("http://example.com/hook")
    with pytest.raises(ValueError, match="letters, digits"):
        tg(secret="bad secret!").set_webhook("https://x.example/hook")
    with pytest.raises(ValueError, match="webhook_secret"):
        tg(secret=None).set_webhook("https://x.example/hook")
    tg(http).set_webhook("https://x.example/hook")
    url, payload, _ = http.calls[-1]
    assert url.endswith("/setWebhook") and payload["secret_token"] == SECRET and payload["allowed_updates"] == ["callback_query", "message"]


# ------------------------------------------------------------------ polling
def test_polling_advances_past_each_update_and_the_application_can_keep_the_place():
    http = FakeHTTP()
    offset = MemoryOffset()
    ch = tg(http, offset=offset)
    http.updates = [{**update(), "update_id": 8}, {"update_id": 9, "callback_query": {"id": "c", "from": {"id": 1}, "data": "ap|abc|approve"}}]
    events = ch.poll_once(timeout=0)
    assert [e.text for e in events] == ["/start", None] and events[1].action == "approve"
    assert http.calls[0][1]["allowed_updates"] == ["callback_query", "message"] and offset.get() == 10
    tg(http, offset=offset).poll_once(timeout=0)                                           # another process, the same place
    assert http.calls[-1][1]["offset"] == 10


# ------------------------------------------------------------------ who the bot talks to
def command(text, sender="777"):
    return SimpleNamespace(text=text, sender=sender)


def test_a_stranger_who_writes_start_learns_only_their_own_chat_id():
    http = FakeHTTP()
    assert tg(http).handle_command(command("/start"), known=False) is True
    [message] = sent(http)
    assert message["chat_id"] == "777" and "777" in message["text"] and "Give that number to the person who runs the Stock Assistant" in message["text"]


def test_a_known_person_gets_the_welcome_and_anything_else_they_type_is_not_a_command():
    http = FakeHTTP()
    ch = tg(http)
    assert ch.handle_command(command("/id"), known=True) is True
    assert "You are set up on the Stock Assistant" in sent(http)[0]["text"] and KNOWN_HELP in sent(http)[0]["text"]
    assert ch.handle_command(command("40"), known=True) is False
    assert ch.handle_command(command("/Start@my_bot"), known=True) is True               # a command with the bot's name, in any case


def test_a_stranger_is_answered_once_every_half_minute_whatever_they_type(monkeypatch):
    http = FakeHTTP()
    now = [1000.0]
    monkeypatch.setattr("ai_munimji_engine.core.approvals.channels.telegram.time.monotonic", lambda: now[0])
    ch = tg(http)
    for text in ("/start", "hello", "/id", "buy a car"):
        assert ch.handle_command(command(text, "888"), known=False) is True
    assert len(sent(http)) == 1
    now[0] += REPLY_EVERY + 1
    ch.handle_command(command("/start", "888"), known=False)
    assert len(sent(http)) == 2


def test_the_application_can_keep_the_throttle_where_all_its_processes_see_it():
    refused = []

    class Shared:
        def allow(self, key):
            refused.append(key)
            return False

    http = FakeHTTP()
    assert tg(http, throttle=Shared()).handle_command(command("/start", "9"), known=False) is True
    assert not http.calls and refused == ["9"]
    own = MemoryThrottle(every=5)                                                        # the default one keeps it in this process
    assert own.allow("a") is True and own.allow("a") is False and own.allow("b") is True


def test_the_application_can_use_its_own_words():
    http = FakeHTTP()
    ch = tg(http, product="AI Munimji", bot_description="the AI Munimji bot for ERPNext", known_help="Requests arrive here with buttons.")
    ch.handle_command(command("/start", "1"), known=True)
    ch.handle_command(command("/start", "2"), known=False)
    first, second = (p["text"] for p in sent(http))
    assert first == "You are set up on AI Munimji. Your Telegram chat id is 1.\nRequests arrive here with buttons."
    assert second.startswith("This is the AI Munimji bot for ERPNext. Your Telegram chat id is 2.") and "the person who runs AI Munimji" in second
    assert (PRODUCT, BOT_DESCRIPTION) == ("the Stock Assistant", "the Stock Assistant bot for a company's inventory")


def test_recent_chats_lists_private_chats_without_marking_anything_read():
    http = FakeHTTP()
    http.updates = [update("/start", sender=4242), update("hi", sender=4242), update("x", sender=7, chat_type="group")]
    people = tg(http).recent_chats()
    assert [(p["chat_id"], p["name"]) for p in people] == [("4242", "Priya")]
    assert "offset" not in http.calls[-1][1] and http.calls[-1][1]["allowed_updates"] == ["message"]
