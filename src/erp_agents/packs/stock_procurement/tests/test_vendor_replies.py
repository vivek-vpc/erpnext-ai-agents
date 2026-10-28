"""Findings F-7, F-8 and F-9 (review of 25 Oct 2026): what happens to a WhatsApp reply from a vendor.

F-7  Every reply was stored and passed to the owner, however many arrived. Now: the first few a day are passed on, one line says there are more, the rest are kept with the
     request, an identical reply is dropped, and a vendor cannot fill the database.
F-8  The vendor's number was recognised by an unsalted SHA-256, which anybody holding a copy of the database can test every phone number against. Now it is a keyed hash
     (HMAC-SHA256, key in the environment); with no key nothing is stored and a reply cannot be matched.
F-9  The 30-day window for a reply was a constant that nothing closed. Now it is a setting, and it closes when the order is received or the price round is compared.
"""
import hashlib
import logging
import sqlite3
from datetime import timedelta

import pytest

from conftest import OWNER, FakeHTTP
from erp_agents.core.approvals.channels.whatsapp import WhatsAppChannel
from erp_agents.core.approvals.inbound import process_inbound
from erp_agents.core.approvals.models import InboundEvent
from erp_agents.core.common import iso
from erp_agents.core.erp.base import SupplierContact
from erp_agents.packs.stock_procurement.settings import FollowUpSettings
from test_followup import APPROVER_NUMBER, NUMBER, erp, late_order, pending, texts_to, wa_app  # noqa: F401  (the follow-up tests' fixtures and helpers)

OTHER_NUMBER = "919876543210"


def chase_sent(app, name="PO-EXT-0001"):
    """A late order of SUP-BUDGET (reachable by WhatsApp only), asked about, and the reminder sent by a person."""
    po = late_order(app, name=name, supplier="SUP-BUDGET", vendor="Budget Traders")
    app.followup.chaser.run()
    assert app.approvals.decide(pending(app, "procurement.chase")[0].id, "send", OWNER, "whatsapp").ok
    return po


def say(app, text, sender=NUMBER):
    process_inbound(InboundEvent(channel="whatsapp", sender=sender, text=text), app.router, app.approvals, app.conversation)


def stored(app):
    return app.db.query_one("SELECT COUNT(*) AS n FROM vendor_replies")["n"]


def passed_on(http):
    return [t for t in texts_to(http, APPROVER_NUMBER) if t.startswith("Reply from vendor")]


def more_replies(http):
    return [t for t in texts_to(http, APPROVER_NUMBER) if t.startswith("More replies from vendor")]


def legacy_message(app, clock, sent_at=None, ref="PO-EXT-0009"):
    """A message as the old code recorded it: an unkeyed SHA-256 of the number, no hash_kind."""
    plain = hashlib.sha256(f"whatsapp:{NUMBER}".encode()).hexdigest()
    app.db.execute("INSERT INTO vendor_messages(id, purpose, ref_id, supplier, channel, address_hash, status, approval_id, sent_by, sent_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("legacy1", "chase", ref, "SUP-BUDGET", "whatsapp", plain, "sent", None, "owner", iso(sent_at or clock())))


# ====================================================================== F-7: a flood of replies
def test_a_flood_of_replies_is_passed_on_three_times_then_once_as_a_count(wa_app):
    app, http = wa_app
    chase_sent(app)
    for i in range(10):
        say(app, f"Reply number {i}")
    assert len(passed_on(http)) == 3 and len(more_replies(http)) == 1
    assert stored(app) == 10                                                                                  # all of them are kept with the request


def test_the_same_reply_twice_is_kept_and_passed_on_once(wa_app):
    app, http = wa_app
    chase_sent(app)
    say(app, "It ships on Friday.")
    say(app, "  it SHIPS   on friday.  ")
    assert stored(app) == 1 and len(passed_on(http)) == 1


def test_beyond_the_keep_limit_nothing_more_is_stored(wa_app):
    app, http = wa_app
    app.settings.update_section(OWNER, "followup", {"vendor_reply_relay_per_day": 1, "vendor_reply_keep_per_day": 5})
    chase_sent(app)
    for i in range(9):
        say(app, f"Reply number {i}")
    assert stored(app) == 5 and len(passed_on(http)) == 1 and len(more_replies(http)) == 1


def test_the_limits_start_again_after_a_day(wa_app, clock):
    app, http = wa_app
    chase_sent(app)
    for i in range(5):
        say(app, f"Reply number {i}")
    assert len(passed_on(http)) == 3
    clock.advance(hours=25)
    say(app, "Back again with news")
    assert len(passed_on(http)) == 4 and "Back again" in passed_on(http)[-1]


def test_one_vendors_flood_does_not_silence_another(wa_app):
    app, http = wa_app
    app.erp.contacts["SUP-OTHER"] = SupplierContact("SUP-OTHER", "Meera", mobile="098765 43210")
    chase_sent(app)
    late_order(app, name="PO-EXT-0002", supplier="SUP-OTHER", vendor="Other Traders")
    app.followup.chaser.run()
    for rec in pending(app, "procurement.chase"):
        assert app.approvals.decide(rec.id, "send", OWNER, "whatsapp").ok
    for i in range(8):
        say(app, f"flood {i}")
    say(app, "Other vendor here", sender=OTHER_NUMBER)
    assert any("Other vendor here" in t for t in passed_on(http))


def test_the_limits_are_settings_with_sensible_bounds():
    s = FollowUpSettings()
    assert (s.vendor_reply_relay_per_day, s.vendor_reply_keep_per_day) == (3, 30)
    for bad in ({"vendor_reply_relay_per_day": 0}, {"vendor_reply_keep_per_day": 2}, {"vendor_reply_relay_per_day": 10, "vendor_reply_keep_per_day": 6}):
        with pytest.raises(ValueError):
            FollowUpSettings(**bad)


# ====================================================================== F-8: a keyed hash
def test_the_stored_hash_is_keyed_not_a_plain_sha256_of_the_number(wa_app):
    app, http = wa_app
    chase_sent(app)
    row = dict(app.db.query_one("SELECT address_hash, hash_kind FROM vendor_messages"))
    plain = hashlib.sha256(f"whatsapp:{NUMBER}".encode()).hexdigest()
    assert row["address_hash"] and len(row["address_hash"]) == 64 and row["address_hash"] != plain and row["hash_kind"] == "hmac1"


def test_the_hash_depends_on_the_key_and_is_normalised():
    from erp_agents.packs.stock_procurement.followup.vendors import address_hash

    a, b = b"a" * 32, b"b" * 32
    assert address_hash("email", "Orders@Prime.example ", a) == address_hash("email", "orders@prime.example", a)
    assert address_hash("whatsapp", NUMBER, a) != address_hash("whatsapp", NUMBER, b)
    assert address_hash("whatsapp", NUMBER, a) != address_hash("email", NUMBER, a)
    assert address_hash("whatsapp", NUMBER, None) is None


def test_without_a_key_no_hash_is_stored_a_reply_cannot_be_matched_and_the_log_says_why(wa_app, monkeypatch, caplog):
    app, http = wa_app
    monkeypatch.delenv("ERP_AGENTS_HASH_KEY", raising=False)
    with caplog.at_level(logging.WARNING):
        chase_sent(app)
        say(app, "Ships Friday")
    assert dict(app.db.query_one("SELECT address_hash, hash_kind FROM vendor_messages")) == {"address_hash": None, "hash_kind": None}
    assert stored(app) == 0
    assert any("ERP_AGENTS_HASH_KEY" in r.getMessage() for r in caplog.records)


def test_a_key_that_is_too_short_counts_as_no_key(wa_app, monkeypatch):
    app, http = wa_app
    monkeypatch.setenv("ERP_AGENTS_HASH_KEY", "short")
    chase_sent(app)
    assert app.db.query_one("SELECT address_hash FROM vendor_messages")["address_hash"] is None


def test_a_message_sent_before_the_keyed_hash_is_still_recognised_until_its_window_ends(wa_app, clock):
    app, http = wa_app
    legacy_message(app, clock)
    say(app, "Delivering Monday")
    assert stored(app) == 1
    clock.advance(days=31)
    say(app, "Another note")
    assert stored(app) == 1


def test_hashes_are_erased_when_their_reply_window_ends_and_the_daily_job_does_it(wa_app, clock):
    app, http = wa_app
    legacy_message(app, clock)
    chase_sent(app)
    clock.advance(days=20)
    assert app.followup.messenger.forget_expired() == 0
    clock.advance(days=11)
    app.followup.daily()
    assert app.db.query_one("SELECT COUNT(*) AS n FROM vendor_messages WHERE address_hash IS NOT NULL")["n"] == 0
    assert app.db.query_one("SELECT COUNT(*) AS n FROM vendor_messages")["n"] == 2                          # the record that a message was sent stays; only the hash goes


# ====================================================================== F-9: the reply window
def test_the_reply_window_is_a_setting_with_thirty_days_as_the_default():
    assert FollowUpSettings().vendor_reply_window_days == 30
    for bad in (0, 121):
        with pytest.raises(ValueError):
            FollowUpSettings(vendor_reply_window_days=bad)


def test_a_reply_after_the_set_window_is_not_taken(wa_app, clock):
    app, http = wa_app
    app.settings.update_section(OWNER, "followup", {"vendor_reply_window_days": 2})
    chase_sent(app)
    clock.advance(days=1)
    say(app, "In time")
    clock.advance(days=2)
    say(app, "Too late")
    assert stored(app) == 1


def test_when_the_goods_arrive_the_reply_window_closes(wa_app):
    app, http = wa_app
    po = chase_sent(app)
    say(app, "On its way")
    assert stored(app) == 1
    po["status"] = "Completed"
    assert app.followup.chaser.run()["arrived"] == 1
    say(app, "Sorry, one more thing")
    assert stored(app) == 1
    row = dict(app.db.query_one("SELECT closed_at, address_hash FROM vendor_messages"))
    assert row["closed_at"] and row["address_hash"] is None


def test_when_a_price_round_is_compared_its_reply_window_closes(wa_app, clock):
    app, http = wa_app
    out = app.followup.quotes.prepare("BOLT-M8", 500, "owner", "price check")
    assert app.approvals.decide(out["approval_id"], "send", OWNER, "test").ok
    say(app, "Our price is 8.50 per bolt")
    assert stored(app) == 1
    clock.advance(days=4)
    assert app.followup.quotes.check()["compared"] == 1
    say(app, "Actually 8.20")
    assert stored(app) == 1 and app.db.query_one("SELECT COUNT(*) AS n FROM vendor_messages WHERE purpose='quote' AND closed_at IS NOT NULL")["n"] >= 1


# ====================================================================== doctor and the start-up log
def test_doctor_reports_the_hash_key_without_showing_it(monkeypatch):
    from erp_agents.core import doctor

    cfg = {"channels": {"whatsapp": {"enabled": True}}}
    ok = doctor.check_hash_key(cfg)
    assert [c.status for c in ok] == [doctor.OK] and "test-only-hash-key" not in repr([c.__dict__ for c in ok])
    monkeypatch.delenv("ERP_AGENTS_HASH_KEY")
    missing = doctor.check_hash_key(cfg)
    assert [c.status for c in missing] == [doctor.WARN] and "not set" in missing[0].title
    assert [c.status for c in doctor.check_hash_key({})] == [doctor.INFO]                          # no WhatsApp, nothing to match
    monkeypatch.setenv("ERP_AGENTS_HASH_KEY", "short")
    assert "shorter than 32" in doctor.check_hash_key(cfg)[0].title


def test_starting_with_whatsapp_and_no_key_is_reported_in_the_log(make_app, erp, monkeypatch, caplog):
    monkeypatch.delenv("ERP_AGENTS_HASH_KEY")
    with caplog.at_level(logging.WARNING):
        make_app(erp=erp, channels={"whatsapp": WhatsAppChannel("T", "PNID", http=FakeHTTP())})
    assert any("ERP_AGENTS_HASH_KEY is not set" in r.getMessage() for r in caplog.records)


# ====================================================================== schema version 4
def test_a_version_3_database_gets_the_new_columns_and_keeps_its_rows(tmp_path):
    from erp_agents.core.storage import Database
    from erp_agents.packs.stock_procurement import schema as stock
    from erp_agents.packs.stock_procurement.inventory.alerts import AlertRepo

    path = tmp_path / "v3.db"
    db = Database(str(path))
    AlertRepo(db)
    db.close()
    c = sqlite3.connect(path)                                                                                   # the vendor tables as version 3 left them
    c.executescript(
        "DROP TABLE vendor_messages; DROP TABLE vendor_replies;"
        "CREATE TABLE vendor_messages (id TEXT PRIMARY KEY, purpose TEXT NOT NULL, ref_id TEXT NOT NULL, supplier TEXT NOT NULL, channel TEXT NOT NULL, address_hash TEXT,"
        " status TEXT NOT NULL, error TEXT, approval_id TEXT, sent_by TEXT, sent_at TEXT NOT NULL);"
        "CREATE TABLE vendor_replies (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL, supplier TEXT NOT NULL, purpose TEXT NOT NULL, ref_id TEXT NOT NULL,"
        " channel TEXT NOT NULL, text TEXT NOT NULL, received_at TEXT NOT NULL);"
        "INSERT INTO vendor_messages VALUES ('m1','chase','PO-1','SUP-A','whatsapp','abc123','sent',NULL,NULL,'owner','2026-10-01T00:00:00+00:00');"
        "INSERT INTO vendor_replies(message_id, supplier, purpose, ref_id, channel, text, received_at) VALUES ('m1','SUP-A','chase','PO-1','whatsapp','hello','2026-10-02T00:00:00+00:00');")
    c.execute("UPDATE pack_schema SET version=3 WHERE pack=?", (stock.NAME,))
    c.commit()
    c.close()
    db = Database(str(path))
    AlertRepo(db)
    columns = lambda table: {r["name"] for r in db.query(f"PRAGMA table_info({table})")}                          # noqa: E731
    assert {"hash_kind", "closed_at"} <= columns("vendor_messages") and {"relayed", "text_hash"} <= columns("vendor_replies")
    assert dict(db.query_one("SELECT address_hash, hash_kind, closed_at FROM vendor_messages WHERE id='m1'")) == {"address_hash": "abc123", "hash_kind": None, "closed_at": None}
    assert dict(db.query_one("SELECT text, relayed FROM vendor_replies")) == {"text": "hello", "relayed": 0}
    assert db.query_one("SELECT version FROM pack_schema WHERE pack=?", (stock.NAME,))["version"] == stock.VERSION == 4
    assert list(tmp_path.glob("v3.db.before-schema-*"))                                                         # copied aside first
    db.close()
