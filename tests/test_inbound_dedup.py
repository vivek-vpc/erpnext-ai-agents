"""A channel delivers a message again (Phase 3 of the hardening brief). WhatsApp and Telegram retry a webhook that was slow to answer or that failed, and Telegram hands out an
update again after a restart that lost track of its offset. A button press is single-use already (the second one finds the request closed); a typed message is not, and a
person must not get two answers, nor have "change the quantity to 20" applied twice. Messages are recognised by the id the channel gave them (WhatsApp's message id,
Telegram's update id), which are remembered for a few days (schema 3: inbound_seen)."""
import hashlib
import hmac
import json
import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient

from conftest import FakeHTTP
from erp_agents.core import metrics
from erp_agents.core.api.app import create_app
from erp_agents.core.approvals.channels.simple import InboxChannel
from erp_agents.core.approvals.channels.telegram import TelegramChannel
from erp_agents.core.approvals.channels.whatsapp import WhatsAppChannel, encode_action
from erp_agents.core.approvals.inbound_ledger import InboundLedger
from erp_agents.core.erp.mock import MockERP
from erp_agents.core.scheduler import AgentScheduler
from erp_agents.core.storage import Database

WA_SECRET, TG_SECRET, PHONE, CHAT = "wa-app-secret", "tg-secret", "919800000000", "555"
QUESTION = "How much BOLT-M8 do we have?"


@pytest.fixture
def wired(make_app, clock):
    http = FakeHTTP()
    channels = {"telegram": TelegramChannel("123:TOKEN", webhook_secret=TG_SECRET, http=http), "whatsapp": WhatsAppChannel("TOKEN", "PNID", app_secret=WA_SECRET, verify_token="vt", http=http),
                "inbox": InboxChannel()}
    cfg = {"approvals": {"strategy": "all", "recipients": [{"id": "owner", "name": "Owner", "roles": ["admin", "approver"],
                                                              "channels": [{"type": "telegram", "address": CHAT}, {"type": "whatsapp", "address": PHONE}, {"type": "inbox"}]}]},
           "auth": {"tokens": []}}
    app = make_app(erp=MockERP(today=clock().date()), channels=channels, cfg=cfg)
    return app, http, TestClient(create_app(app))


def wa_post(client, *messages):
    body = json.dumps({"entry": [{"changes": [{"value": {"messages": list(messages)}}]}]}).encode()
    sig = "sha256=" + hmac.new(WA_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})


def wa_text(mid, text=QUESTION):
    return {"from": PHONE, "id": mid, "type": "text", "text": {"body": text}}


def wa_press(mid, approval_id, action):
    return {"from": PHONE, "id": mid, "type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": encode_action(approval_id, action)}}}


def tg_post(client, update):
    return client.post("/webhooks/telegram", content=json.dumps(update).encode(), headers={"X-Telegram-Bot-Api-Secret-Token": TG_SECRET, "Content-Type": "application/json"})


def tg_text(update_id, text=QUESTION):
    return {"update_id": update_id, "message": {"message_id": update_id + 10, "from": {"id": int(CHAT), "is_bot": False}, "chat": {"id": int(CHAT), "type": "private"}, "text": text}}


def tg_press(update_id, approval_id, action):
    return {"update_id": update_id, "callback_query": {"id": f"cq{update_id}", "from": {"id": int(CHAT)}, "data": encode_action(approval_id, action)}}


def sent_to_phone(http):
    return [c[1]["text"]["body"] for c in http.calls if c[1] and c[1].get("type") == "text" and c[1].get("to") == PHONE]


def sent_to_chat(http):
    return [c[1]["text"] for c in http.calls if c[0].endswith("/sendMessage") and str(c[1].get("chat_id")) == CHAT]


# ------------------------------------------------------------------ what the channels give us
def test_the_ids_the_channels_give_a_message_are_read(wired):
    app, _, _ = wired
    wa = app.channels["whatsapp"].parse_webhook({"entry": [{"changes": [{"value": {"messages": [wa_text("wamid.AAA"), wa_press("wamid.BBB", "abc", "approve")]}}]}]})
    assert [e.external_id for e in wa] == ["wamid.AAA", "wamid.BBB"]
    tg = app.channels["telegram"]
    assert tg.parse_webhook(tg_text(1001))[0].external_id == "1001" and tg.parse_webhook(tg_press(1002, "abc", "approve"))[0].external_id == "1002"
    assert tg.parse_webhook({"message": tg_text(5)["message"]})[0].external_id is None                           # no id given: nothing to recognise it by


# ------------------------------------------------------------------ WhatsApp
def test_a_whatsapp_message_delivered_twice_is_answered_once(wired):
    app, http, client = wired
    assert wa_post(client, wa_text("wamid.A1")).status_code == 200
    assert len(sent_to_phone(http)) == 1
    assert wa_post(client, wa_text("wamid.A1")).status_code == 200                       # the same message again: WhatsApp did not get our 200 in time
    assert len(sent_to_phone(http)) == 1 and app.db.query_one("SELECT COUNT(*) AS n FROM chat_log")["n"] == 1


def test_two_different_messages_with_the_same_words_are_two_questions(wired):
    app, http, client = wired
    wa_post(client, wa_text("wamid.A1"))
    wa_post(client, wa_text("wamid.A2"))
    assert len(sent_to_phone(http)) == 2


def test_a_batch_with_one_message_seen_before_answers_only_the_new_one(wired):
    app, http, client = wired
    wa_post(client, wa_text("wamid.A1"))
    wa_post(client, wa_text("wamid.A1"), wa_text("wamid.A2", "How much NUT-1 do we have?"))
    assert len(sent_to_phone(http)) == 2


def test_a_button_pressed_message_delivered_again_is_ignored_in_silence(wired):
    app, http, client = wired
    app.inventory.run(item_codes=["BOLT-M8"])
    approval_id = app.approvals.list("pending")[0].id
    wa_post(client, wa_press("wamid.P1", approval_id, "approve"))
    after_first = sent_to_phone(http)
    wa_post(client, wa_press("wamid.P1", approval_id, "approve"))                           # redelivered
    assert sent_to_phone(http) == after_first                                              # no second "Already answered" message
    assert app.approvals.get(approval_id).decision.action == "approve"


def test_a_message_without_an_id_is_handled_as_before(wired):
    app, http, client = wired
    no_id = {"from": PHONE, "type": "text", "text": {"body": QUESTION}}
    wa_post(client, no_id)
    wa_post(client, no_id)
    assert len(sent_to_phone(http)) == 2                                                   # nothing to recognise it by


# ------------------------------------------------------------------ Telegram
def test_a_telegram_update_delivered_twice_is_answered_once(wired):
    app, http, client = wired
    assert tg_post(client, tg_text(1001)).status_code == 200
    assert len(sent_to_chat(http)) == 1
    assert tg_post(client, tg_text(1001)).status_code == 200                               # Telegram retried the webhook
    assert len(sent_to_chat(http)) == 1
    tg_post(client, tg_text(1002))
    assert len(sent_to_chat(http)) == 2


def test_the_same_update_handed_out_again_after_a_restart_is_recognised(wired):
    app, http, client = wired
    channel = app.channels["telegram"]
    update = tg_text(2001)
    for event in channel.parse_webhook(update):
        from erp_agents.core.approvals.inbound import process_inbound
        process_inbound(event, app.router, app.approvals, app.conversation)
    first = len(sent_to_chat(http))
    channel._offset = 0                                                                     # a new process that lost the offset: Telegram hands the update out again
    for event in channel.parse_webhook(update):
        process_inbound(event, app.router, app.approvals, app.conversation)
    assert first == 1 and len(sent_to_chat(http)) == 1


def test_a_telegram_button_delivered_twice_does_not_answer_twice(wired):
    app, http, client = wired
    app.inventory.run(item_codes=["BOLT-M8"])
    approval_id = app.approvals.list("pending")[0].id
    tg_post(client, tg_press(3001, approval_id, "approve"))
    calls_after_first = len(http.calls)
    tg_post(client, tg_press(3001, approval_id, "approve"))
    assert len(http.calls) == calls_after_first                                            # not even the "Already answered" pop-up


# ------------------------------------------------------------------ a message that could not be handled
def test_a_message_whose_handling_failed_may_be_delivered_again_and_tried_again(wired, monkeypatch):
    app, http, client = wired
    real = app.conversation.submit
    calls = []

    def flaky(event, recipient, channel):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the database was locked")
        return real(event, recipient, channel)
    monkeypatch.setattr(app.conversation, "submit", flaky)
    assert wa_post(client, wa_text("wamid.F1")).status_code == 200                         # the route still says 200: the platform is not asked to retry for ever
    assert sent_to_phone(http) == []
    wa_post(client, wa_text("wamid.F1"))                                                    # but if it does deliver it again, it is handled
    assert len(sent_to_phone(http)) == 1 and len(calls) == 2


# ------------------------------------------------------------------ reported once, remembered for a time
def test_a_duplicate_is_one_log_line_and_one_count_and_never_the_text(wired, caplog):
    app, http, client = wired
    metrics.REGISTRY.reset()
    metrics.configure(True)
    caplog.set_level(logging.INFO, logger="erp_agents.calls")
    try:
        wa_post(client, wa_text("wamid.D1", "my private question about the bolts"))
        for _ in range(2):
            wa_post(client, wa_text("wamid.D1", "my private question about the bolts"))
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("inbound_duplicate")]
        assert lines == ["inbound_duplicate channel=whatsapp"] * 2 and not any("private" in r.getMessage() or "wamid" in r.getMessage() for r in caplog.records if "inbound" in r.getMessage())
        assert metrics.INBOUND_DUPLICATES.values[("whatsapp",)] == 2
    finally:
        metrics.configure(False)
        metrics.REGISTRY.reset()


def test_ids_are_forgotten_after_the_set_number_of_days_by_the_housekeeping_job(wired, clock):
    app, http, client = wired
    wa_post(client, wa_text("wamid.OLD"))
    clock.advance(days=6)
    wa_post(client, wa_text("wamid.NEW", "How much NUT-1 do we have?"))
    clock.advance(days=2)                                                                   # the first is now 8 days old, the second 2
    AgentScheduler(app)._maintenance_job()
    rows = {r["external_id"] for r in app.db.query("SELECT external_id FROM inbound_seen")}
    assert rows == {"wamid.NEW"}


def test_the_number_of_days_comes_from_the_settings(make_app, clock):
    app = make_app(cfg={"resilience": {"inbound_memory_days": 2}})
    app.approvals.inbound.claim("whatsapp", "wamid.X")
    clock.advance(days=3)
    assert app.approvals.inbound.purge() == 1


# ------------------------------------------------------------------ the ledger itself
def test_the_ledger_claims_each_id_once_per_channel_and_can_give_one_back():
    db = Database(":memory:")
    ledger = InboundLedger(db, lambda: __import__("datetime").datetime(2026, 10, 29, 9, 0, tzinfo=__import__("datetime").timezone.utc))
    assert ledger.claim("whatsapp", "a") is True and ledger.claim("whatsapp", "a") is False
    assert ledger.claim("telegram", "a") is True                                            # another channel's ids are its own
    ledger.release("whatsapp", "a")
    assert ledger.claim("whatsapp", "a") is True
    assert ledger.claim("whatsapp", "") is True and ledger.claim("whatsapp", "") is True        # no id: nothing is recorded, nothing is a duplicate
    long_id = "x" * 500
    assert ledger.claim("whatsapp", long_id) and not ledger.claim("whatsapp", long_id)


# ------------------------------------------------------------------ schema 3
def downgrade(path, version):
    """What a database written by an older version looks like: the tables of later versions are not there."""
    c = sqlite3.connect(path)
    for table, since in (("inbound_seen", 3), ("job_runs", 2)):
        if since > version:
            c.execute(f"DROP TABLE {table}")
    c.execute("UPDATE schema_info SET version=?", (version,))
    c.commit()
    c.close()


def version_of(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT version FROM schema_info").fetchone()[0]
    finally:
        c.close()


def tables_of(path):
    c = sqlite3.connect(path)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


@pytest.mark.parametrize("old", [1, 2])
def test_an_older_database_is_copied_aside_and_gets_the_new_table(tmp_path, old):
    from erp_agents.core import storage
    path = str(tmp_path / "app.db")
    db = Database(path)
    db.execute("INSERT INTO settings_overrides(scope_type, scope_key, data_json, updated_by, updated_at) VALUES ('item', 'K1', '{}', 't', '2026-10-29')")
    db.close()
    downgrade(path, old)
    assert storage.SCHEMA_VERSION == 3 and "inbound_seen" not in tables_of(path)
    Database(path).close()
    assert version_of(path) == 3 and "inbound_seen" in tables_of(path)
    (copy,) = [p for p in tmp_path.iterdir() if f".before-schema-{old}-" in p.name]
    assert version_of(str(copy)) == old and "inbound_seen" not in tables_of(str(copy))      # the copy is the old database, untouched
    db = Database(path)
    assert db.query_one("SELECT COUNT(*) AS n FROM settings_overrides")["n"] == 1           # nothing was lost
    db.close()


def test_a_backup_made_by_the_previous_version_still_verifies_and_restores(tmp_path):
    from erp_agents.core import ops
    path = str(tmp_path / "app.db")
    Database(path).close()
    backup = tmp_path / "old.db"
    ops.backup_database(path, str(backup))
    downgrade(str(backup), 2)
    verdict = ops.verify_database(str(backup))
    assert verdict.ok and verdict.version == 2, verdict.problems
    target = str(tmp_path / "restored.db")
    ops.restore_database(str(backup), target)
    Database(target).close()
    assert version_of(target) == 3
    gone = tmp_path / "damaged.db"
    ops.backup_database(path, str(gone))
    c = sqlite3.connect(gone)
    c.execute("DROP TABLE inbound_seen")
    c.commit()
    c.close()
    assert not ops.verify_database(str(gone)).ok                                            # a version 3 file without the table is damaged
