"""The stock pack's stores do the same thing on every backend.

Each test runs on SQLite and on the in-memory fake (the `stock` fixture, built over the core's `stores` fixture so that one transaction covers both). Only the Protocols in
`ai_munimji_engine.stores.stock` are used. A Frappe site's stores will be added to the same fixture."""
import json

import pytest

from erp_agents.packs.stock_procurement.stores import stock_stores_for

T0, T1, T2, T3 = "2026-10-16T08:00:00+00:00", "2026-10-16T09:00:00+00:00", "2026-10-16T10:00:00+00:00", "2026-10-17T10:00:00+00:00"


@pytest.fixture
def stock(stores):
    return stock_stores_for(stores)


# ------------------------------------------------------------------ alerts
def add_alert(stock, alert_id, item="BOLT", now=T0, kind="BREACH", severity=2):
    stock.alerts.insert(alert_id, item, kind, severity, now, json.dumps({"days_cover": 2}), "low on bolts", "run-1")


def test_a_new_alert_is_awaiting_and_nothing_else_is_set(stock):
    add_alert(stock, "a1")
    row = stock.alerts.get("a1")
    assert (row["item_code"], row["kind"], row["severity"], row["status"], row["created_at"], row["updated_at"], row["run_id"]) == ("BOLT", "BREACH", 2, "awaiting", T0, T0, "run-1")
    assert (row["approval_id"], row["snoozed_until"], row["suppress_until"], row["cooldown_until"], row["status_note"]) == (None, None, None, None, None)
    assert json.loads(row["analysis_json"]) == {"days_cover": 2} and row["reasoning_text"] == "low on bolts"
    assert stock.alerts.get("nope") is None
    with pytest.raises(Exception):
        add_alert(stock, "a1")                                                   # the id is the key


def test_alerts_are_found_by_item_by_status_and_by_the_request_they_carry(stock):
    add_alert(stock, "a1", "BOLT", T0)
    add_alert(stock, "a2", "NUT", T1)
    add_alert(stock, "a3", "BOLT", T2)
    add_alert(stock, "a4", "BOLT", T2)                                           # the same instant as a3: the one made last is the newest
    s = stock.alerts
    s.update_status("a1", "snoozed", "later", T3, T3, None, None)
    s.update_status("a2", "dismissed", "", T3, None, None, None)
    s.set_approval("a3", "req-3", T3)
    assert s.latest_for_item("BOLT")["id"] == "a4" and s.latest_for_item("NUT")["id"] == "a2" and s.latest_for_item("none") is None
    assert [r["id"] for r in s.open_for_item("BOLT", ("awaiting", "snoozed"))] == ["a1", "a3", "a4"]
    assert [r["id"] for r in s.open_for_item("BOLT", ("snoozed",))] == ["a1"]
    assert [r["id"] for r in s.list(None, None, 10)] == ["a4", "a3", "a2", "a1"]
    assert [r["id"] for r in s.list("awaiting", None, 10)] == ["a4", "a3"] and [r["id"] for r in s.list(None, "NUT", 10)] == ["a2"]
    assert [r["id"] for r in s.list(None, None, 2)] == ["a4", "a3"]
    assert s.by_approval("req-3")["id"] == "a3" and s.by_approval("req-9") is None
    assert s.get("a3")["updated_at"] == T3


def test_a_status_change_keeps_the_dates_it_is_not_given(stock):
    add_alert(stock, "a1")
    s = stock.alerts
    s.update_status("a1", "snoozed", "until Monday", T1, T3, T2, None)
    row = s.get("a1")
    assert (row["status"], row["status_note"], row["updated_at"], row["snoozed_until"], row["suppress_until"], row["cooldown_until"]) == ("snoozed", "until Monday", T1, T3, T2, None)
    s.update_status("a1", "actioned", "", T2, None, None, T3)
    row = s.get("a1")
    assert (row["status"], row["status_note"], row["snoozed_until"], row["suppress_until"], row["cooldown_until"]) == ("actioned", "", T3, T2, T3)
    s.set_reasoning("a1", "new text")
    assert s.get("a1")["reasoning_text"] == "new text" and s.get("a1")["updated_at"] == T2          # the reasoning does not touch updated_at


# ------------------------------------------------------------------ notices
def add_notice(stock, notice_id, item="BOLT", level="LOW", now=T0, approval_id=None):
    stock.notices.insert(notice_id, item, level, now, approval_id)


def test_a_new_notice_is_sent_and_can_get_a_request_and_a_status(stock):
    add_notice(stock, "n1", approval_id="req-1")
    s = stock.notices
    row = s.get("n1")
    assert (row["item_code"], row["level"], row["status"], row["created_at"], row["updated_at"], row["approval_id"], row["snoozed_until"]) == ("BOLT", "LOW", "sent", T0, T0, "req-1", None)
    assert s.by_approval("req-1")["id"] == "n1" and s.by_approval("zzz") is None and s.get("none") is None
    s.set_status("n1", "snoozed", T1, T3)
    s.set_status("n1", "acknowledged", T2, None)                                  # no new date: the old one stays
    row = s.get("n1")
    assert (row["status"], row["updated_at"], row["snoozed_until"]) == ("acknowledged", T2, T3)
    s.set_approval("n1", "req-2", T3)
    assert s.get("n1")["approval_id"] == "req-2" and s.get("n1")["updated_at"] == T3


def test_the_active_notices_of_an_item_and_the_newest_of_them(stock):
    add_notice(stock, "n1", "BOLT", "LOW", T0)
    add_notice(stock, "n2", "BOLT", "URGENT", T1)
    add_notice(stock, "n3", "NUT", "LOW", T1)
    add_notice(stock, "n4", "BOLT", "LOW", T1)                                    # the same instant as n2: the one made last is the newest
    stock.notices.set_status("n4", "cleared", T2, None)
    active = ("sent", "acknowledged", "snoozed")
    assert [r["id"] for r in stock.notices.active_for_item("BOLT", active)] == ["n1", "n2"]
    assert stock.notices.latest_active("BOLT", active)["id"] == "n2" and stock.notices.latest_active("NUT", active)["id"] == "n3"
    assert stock.notices.latest_active("none", active) is None
    assert [r["id"] for r in stock.notices.list(10)] == ["n4", "n3", "n2", "n1"] and [r["id"] for r in stock.notices.list(2)] == ["n4", "n3"]


# ------------------------------------------------------------------ proposals
def add_proposal(stock, proposal_id, item="BOLT", status="awaiting", now=T0, **kw):
    stock.proposals.insert(proposal_id, kw.get("work_order_id", "w1"), item, status, kw.get("supplier"), kw.get("supplier_name"), kw.get("qty"), kw.get("rate"), kw.get("total"),
                           kw.get("ranking_json"), kw.get("note", ""), now)


def test_a_new_proposal_holds_what_was_recommended(stock):
    add_proposal(stock, "p1", supplier="S1", supplier_name="Acme", qty=100.0, rate=2.5, total=250.0, ranking_json='{"a": 1}', note="cheapest")
    row = stock.proposals.get("p1")
    assert (row["work_order_id"], row["item_code"], row["status"], row["supplier"], row["supplier_name"], row["qty"], row["rate"], row["total"], row["ranking_json"],
            row["status_note"], row["created_at"], row["updated_at"]) == ("w1", "BOLT", "awaiting", "S1", "Acme", 100.0, 2.5, 250.0, '{"a": 1}', "cheapest", T0, T0)
    assert (row["approval_id"], row["po_name"], row["snoozed_until"]) == (None, None, None) and stock.proposals.get("none") is None
    with pytest.raises(Exception):
        add_proposal(stock, "p1")


def test_a_proposal_is_revised_ranked_linked_and_closed(stock):
    add_proposal(stock, "p1", supplier="S1", supplier_name="Acme", qty=100.0, rate=2.5, total=250.0)
    s = stock.proposals
    s.set_ranking("p1", '{"best": "S2"}', T1)
    s.set_approval("p1", "req-1", T1)
    s.revise("p1", "S2", "Beta", 80.0, 3.0, 240.0, "quantity changed", T2)
    row = s.get("p1")
    assert (row["supplier"], row["supplier_name"], row["qty"], row["rate"], row["total"], row["status_note"], row["updated_at"], row["ranking_json"], row["approval_id"],
            row["status"]) == ("S2", "Beta", 80.0, 3.0, 240.0, "quantity changed", T2, '{"best": "S2"}', "req-1", "awaiting")
    s.update_status("p1", "snoozed", "later", T3, {"snoozed_until": T3})
    s.update_status("p1", "ordered", "done", T3, {"po_name": "PUR-0001"})
    row = s.get("p1")
    assert (row["status"], row["status_note"], row["updated_at"], row["po_name"], row["snoozed_until"]) == ("ordered", "done", T3, "PUR-0001", T3)
    assert s.by_approval("req-1")["id"] == "p1" and s.by_approval("zzz") is None
    with pytest.raises(ValueError):
        s.update_status("p1", "ordered", "", T3, {"supplier": "x"})                # only po_name and snoozed_until may be set that way


def test_open_snoozed_and_listed_proposals(stock):
    add_proposal(stock, "p1", "BOLT", "awaiting", T0)
    add_proposal(stock, "p2", "BOLT", "manual", T1)
    add_proposal(stock, "p3", "NUT", "awaiting", T2)
    add_proposal(stock, "p4", "BOLT", "snoozed", T2)
    add_proposal(stock, "p5", "BOLT", "snoozed", T3)
    s = stock.proposals
    s.update_status("p4", "snoozed", "", T2, {"snoozed_until": T1})
    s.update_status("p5", "snoozed", "", T2, {"snoozed_until": T3})
    assert [r["id"] for r in s.open_for_item("BOLT", ("awaiting", "manual"))] == ["p1", "p2"]
    assert [r["id"] for r in s.snoozed_due(T2)] == ["p4"] and [r["id"] for r in s.snoozed_due(T3)] == ["p4", "p5"] and s.snoozed_due(T0) == []
    assert [r["id"] for r in s.list(None, None, 10)] == ["p5", "p3", "p4", "p2", "p1"]
    assert [r["id"] for r in s.list("awaiting", None, 10)] == ["p3", "p1"] and [r["id"] for r in s.list(None, "NUT", 10)] == ["p3"]
    assert len(s.list(None, None, 2)) == 2


# ------------------------------------------------------------------ the run log
def test_runs_are_logged_and_the_newest_real_ones_come_first(stock):
    s = stock.runs
    s.add("r1", "inventory", T0, T0, False, '{"n": 1}')
    s.add("r2", "inventory", T1, T1, True, '{"n": 2}')                            # a dry run
    s.add("r3", "inventory", T2, None, False, '{"n": 3}')
    s.add("r4", "procurement", T3, T3, False, None)
    row = s.get("r1")
    assert (row["agent"], row["started_at"], row["finished_at"], row["dry_run"], row["summary_json"]) == ("inventory", T0, T0, 0, '{"n": 1}') and s.get("none") is None
    assert s.get("r2")["dry_run"] == 1
    assert [(r["started_at"], r["summary_json"]) for r in s.recent("inventory", 10)] == [(T2, '{"n": 3}'), (T0, '{"n": 1}')]
    assert s.recent("inventory", 1) == [{"started_at": T2, "summary_json": '{"n": 3}'}]
    assert [r["id"] for r in s.list(10)] == ["r4", "r3", "r2", "r1"] and [r["id"] for r in s.list(2)] == ["r4", "r3"]


# ------------------------------------------------------------------ what the assistant said
def test_an_answer_is_kept_under_its_key_and_replaced_by_the_next(stock):
    s = stock.assistant
    assert s.cached_answer("k1") is None
    s.put_answer("k1", "req-1", "why?", '{"a": 1}', "llama", "priya", T0)
    s.put_answer("k2", "req-1", "and then?", '{"a": 2}', "llama", "sam", T1)
    s.put_answer("k3", "req-2", "other", '{"a": 3}', "llama", "sam", T1)
    assert s.cached_answer("k1") == '{"a": 1}'
    s.put_answer("k1", "req-1", "why again?", '{"a": 4}', "llama", "priya", T2)
    assert s.cached_answer("k1") == '{"a": 4}'
    assert [(r["question"], r["result_json"], r["asked_by"], r["created_at"]) for r in s.answers_for("req-1")] == [("and then?", '{"a": 2}', "sam", T1), ("why again?", '{"a": 4}', "priya", T2)]
    assert s.answers_for("req-9") == []


def test_the_chat_log_keeps_what_was_asked_in_order(stock):
    s = stock.assistant
    assert s.chats(10) == []
    s.log_chat("priya", T0, "what is low?", "stock_overview", "answered", "rules")
    s.log_chat("sam", T1, "order bolts", "prepare_order", "refused", "ai-routed")
    s.log_chat("priya", T2, "why?", "ask", "answered", "rules")
    rows = s.chats(10)
    assert [(r["id"], r["asked_by"], r["asked_at"], r["question"], r["intent"], r["status"], r["source"]) for r in rows] == [
        (1, "priya", T0, "what is low?", "stock_overview", "answered", "rules"), (2, "sam", T1, "order bolts", "prepare_order", "refused", "ai-routed"),
        (3, "priya", T2, "why?", "ask", "answered", "rules")]
    assert [r["id"] for r in s.chats(2)] == [2, 3]


# ------------------------------------------------------------------ vendor messages and replies
def add_message(stock, message_id, purpose="chase", ref_id="PO-1", supplier="Acme", channel="whatsapp", address_hash="h1", hash_kind="hmac1", status="sent", error=None,
                approval_id="req-1", sent_at=T0):
    stock.vendors.add_message(message_id, purpose, ref_id, supplier, channel, address_hash, hash_kind, status, error, approval_id, "priya via telegram", sent_at)


def test_messages_are_listed_by_order_or_request_oldest_first(stock):
    add_message(stock, "m1", sent_at=T1, error=None)
    add_message(stock, "m2", sent_at=T0, status="failed", error="no number")
    add_message(stock, "m3", purpose="quote", ref_id="Q-1", sent_at=T2, approval_id="req-2")
    s = stock.vendors
    assert [(r["id"], r["supplier"], r["channel"], r["status"], r["error"], r["approval_id"], r["sent_at"]) for r in s.messages("chase", "PO-1")] == [
        ("m2", "Acme", "whatsapp", "failed", "no number", "req-1", T0), ("m1", "Acme", "whatsapp", "sent", None, "req-1", T1)]
    assert [(r["supplier"], r["channel"], r["status"], r["error"], r["sent_at"]) for r in s.messages_for_approval("req-1")] == [("Acme", "whatsapp", "failed", "no number", T0),
                                                                                                                              ("Acme", "whatsapp", "sent", None, T1)]
    assert s.messages("quote", "Q-1")[0]["id"] == "m3" and s.messages("chase", "none") == [] and s.messages_for_approval("none") == []
    assert s.last_sent_channel("req-1") == "whatsapp" and s.last_sent_channel("req-2") == "whatsapp" and s.last_sent_channel("none") is None
    add_message(stock, "m4", channel="email", sent_at=T3, approval_id="req-1")
    assert s.last_sent_channel("req-1") == "email"


def test_a_reply_is_matched_to_the_latest_open_message_with_the_same_hash(stock):
    add_message(stock, "m1", address_hash="H", hash_kind="hmac1", sent_at=T0)
    add_message(stock, "m2", address_hash="H", hash_kind="hmac1", sent_at=T2)
    add_message(stock, "m3", address_hash="H", hash_kind=None, sent_at=T1)       # an old, unkeyed hash
    add_message(stock, "m4", address_hash="H", hash_kind="hmac1", sent_at=T3, status="failed")
    s = stock.vendors
    assert s.open_message("H", "hmac1", T0)["id"] == "m2"                         # not the failed one, not an older one
    assert s.open_message("H", "", T0)["id"] == "m3" and s.open_message("H", "hmac1", T3) is None and s.open_message("X", "hmac1", T0) is None
    assert s.close_replies("chase", "PO-1", T3) == 4
    assert s.open_message("H", "hmac1", T0) is None


def test_closing_an_order_closes_its_messages_once_and_forgets_their_hash(stock):
    add_message(stock, "m1", ref_id="PO-1", address_hash="H1", sent_at=T0)
    add_message(stock, "m2", ref_id="PO-1", address_hash="H2", sent_at=T1)
    add_message(stock, "m3", ref_id="PO-2", address_hash="H3", sent_at=T1)
    s = stock.vendors
    assert s.open_chase_refs() == {"PO-1", "PO-2"}
    assert s.close_replies("chase", "PO-1", T2) == 2 and s.close_replies("chase", "PO-1", T3) == 0
    assert s.open_chase_refs() == {"PO-2"} and s.open_message("H1", "hmac1", T0) is None and s.open_message("H3", "hmac1", T0)["id"] == "m3"
    assert s.forget_hashes_before(T1) == 0                                        # m1 and m2 lost their hash on closing; m3 was sent at T1, not before
    assert s.forget_hashes_before(T2) == 1 and s.open_message("H3", "hmac1", T0) is None


def test_replies_are_kept_counted_and_recognised_when_repeated(stock):
    add_message(stock, "m1", approval_id="req-1")
    add_message(stock, "m2", ref_id="PO-2", approval_id="req-2")
    s = stock.vendors
    s.add_reply("m1", "Acme", "chase", "PO-1", "whatsapp", "will ship Friday", T0, 1, "hash-a")
    s.add_reply("m1", "Acme", "chase", "PO-1", "whatsapp", "sorry for the delay", T1, 2, "hash-b")
    s.add_reply("m1", "Acme", "chase", "PO-1", "whatsapp", "more", T2, 0, "hash-c")
    s.add_reply("m2", "Beta", "chase", "PO-2", "whatsapp", "ok", T2, 1, "hash-d")
    assert [(r["supplier"], r["channel"], r["text"], r["received_at"]) for r in s.replies("chase", "PO-1")] == [
        ("Acme", "whatsapp", "will ship Friday", T0), ("Acme", "whatsapp", "sorry for the delay", T1), ("Acme", "whatsapp", "more", T2)]
    assert [(r["supplier"], r["text"]) for r in s.replies_for_approval("req-2")] == [("Beta", "ok")] and s.replies_for_approval("none") == []
    assert s.reply_seen("Acme", "hash-a", T0) and not s.reply_seen("Acme", "hash-a", T1) and not s.reply_seen("Beta", "hash-a", T0) and not s.reply_seen("Acme", "zzz", T0)
    assert s.count_replies("Acme", T0) == 3 and s.count_replies("Acme", T1) == 2 and s.count_replies("Acme", T0, 1) == 1 and s.count_replies("Acme", T0, 2) == 1
    assert s.count_replies("Acme", T0, 0) == 1 and s.count_replies("Nobody", T0) == 0


# ------------------------------------------------------------------ chases, receipts, quote rounds
def test_a_chase_is_started_asked_again_sent_and_closed(stock):
    s = stock.chases
    assert s.get("PO-1") is None and s.open_chases() == []
    s.start("PO-1", "Acme", "BOLT", "2026-10-10", T0, "req-1", T0)
    s.start("PO-2", "Beta", "NUT", "2026-10-12", T0, "req-2", T0)
    row = s.get("PO-1")
    assert (row["supplier"], row["item_code"], row["status"], row["first_late_on"], row["asked_at"], row["asks"], row["sent_at"], row["sends"], row["approval_id"],
            row["updated_at"]) == ("Acme", "BOLT", "open", "2026-10-10", T0, 1, None, 0, "req-1", T0)
    s.record_send("PO-1", T1)
    s.set_status("PO-2", "dismissed", T1)
    s.reopen("PO-1", T2, "req-3", T2)
    row = s.get("PO-1")
    assert (row["status"], row["asked_at"], row["asks"], row["sent_at"], row["sends"], row["approval_id"], row["updated_at"]) == ("open", T2, 2, T1, 1, "req-3", T2)
    assert [r["po_name"] for r in s.open_chases()] == ["PO-1"] and s.get("PO-2")["status"] == "dismissed"
    with pytest.raises(Exception):
        s.start("PO-1", "Acme", "BOLT", "2026-10-10", T0, "req-9", T0)


def test_a_receipt_is_checked_once_and_acknowledged_by_its_request(stock):
    s = stock.receipts
    assert s.get("GRN-1") is None
    s.add("GRN-1", "Acme", "clean", "[]", None, T0)
    s.add("GRN-2", "Acme", "flagged", '[{"kind": "price"}]', "req-1", T1)
    s.add("GRN-3", "Beta", "flagged", '[{"kind": "qty"}]', "req-2", T2)
    assert s.get("GRN-1")["status"] == "clean" and s.get("GRN-2")["approval_id"] == "req-1"
    with pytest.raises(Exception):
        s.add("GRN-1", "Acme", "clean", "[]", None, T3)
    assert sorted((r["supplier"], r["problems_json"]) for r in s.problems_since(T1)) == [("Acme", '[{"kind": "price"}]'), ("Beta", '[{"kind": "qty"}]')]
    assert len(s.problems_since(T0)) == 3 and s.problems_since(T3) == []
    s.acknowledge("req-1")
    assert s.get("GRN-2")["status"] == "acknowledged" and s.get("GRN-3")["status"] == "flagged" and s.get("GRN-1")["status"] == "clean"


def test_a_quote_round_is_proposed_updated_and_listed_newest_first(stock):
    s = stock.quotes
    s.insert("q1", "BOLT", 100.0, "Nos", '[{"supplier": "S1"}]', "2026-10-20", "priya", T0)
    s.insert("q2", "NUT", 50.0, "Nos", "[]", "2026-10-21", "sam", T1)
    s.insert("q3", "BOLT", 10.0, "Nos", "[]", "2026-10-22", "sam", T1)
    row = s.get("q1")
    assert (row["item_code"], row["qty"], row["uom"], row["suppliers_json"], row["status"], row["reply_by"], row["requested_by"], row["created_at"], row["updated_at"]) == (
        "BOLT", 100.0, "Nos", '[{"supplier": "S1"}]', "proposed", "2026-10-20", "priya", T0, T0)
    assert (row["asked_at"], row["approval_id"], row["comparison_id"], row["status_note"]) == (None, None, None, None) and s.get("none") is None
    s.update("q1", {"status": "asked", "asked_at": T1, "approval_id": "req-1", "reply_by": "2026-10-25", "updated_at": T1})
    s.update("q1", {"status": "compared", "comparison_id": "req-2", "status_note": "two prices", "updated_at": T2})
    row = s.get("q1")
    assert (row["status"], row["asked_at"], row["approval_id"], row["reply_by"], row["comparison_id"], row["status_note"], row["updated_at"]) == (
        "compared", T1, "req-1", "2026-10-25", "req-2", "two prices", T2)
    assert s.ids(None, 10) == ["q2", "q3", "q1"] and s.ids(None, 2) == ["q2", "q3"] and s.ids("compared", 10) == ["q1"] and s.ids("closed", 10) == []
    with pytest.raises(ValueError):
        s.update("q1", {"qty": 1})                                                # only the columns the rounds change may be set


# ------------------------------------------------------------------ transfers and counts
def test_a_transfer_is_proposed_updated_found_by_its_request_and_listed(stock):
    s = stock.transfers
    s.insert("t1", "BOLT", 5.0, "Nos", "Stores", "Shop", "restock the shop", "priya", T0)
    s.insert("t2", "NUT", 2.0, "Nos", "Stores", "Shop", None, "sam", T1)
    s.insert("t3", "BOLT", 1.0, "Nos", "Shop", "Stores", None, "sam", T1)
    row = s.get("t1")
    assert (row["item_code"], row["qty"], row["uom"], row["from_warehouse"], row["to_warehouse"], row["status"], row["reason"], row["requested_by"], row["created_at"],
            row["updated_at"]) == ("BOLT", 5.0, "Nos", "Stores", "Shop", "proposed", "restock the shop", "priya", T0, T0)
    assert (row["entry_name"], row["approval_id"], row["status_note"]) == (None, None, None) and s.get("none") is None
    s.update("t1", {"approval_id": "req-1", "updated_at": T1})
    s.update("t1", {"status": "created", "entry_name": "MTN-0001", "status_note": "approved by priya", "updated_at": T2})
    row = s.get("t1")
    assert (row["status"], row["entry_name"], row["status_note"], row["approval_id"], row["updated_at"]) == ("created", "MTN-0001", "approved by priya", "req-1", T2)
    assert s.by_approval("req-1")["id"] == "t1" and s.by_approval("none") is None
    assert [r["id"] for r in s.list(None, 10)] == ["t2", "t3", "t1"] and [r["id"] for r in s.list("proposed", 10)] == ["t2", "t3"] and len(s.list(None, 1)) == 1
    with pytest.raises(ValueError):
        s.update("t1", {"qty": 99})


def test_the_count_schedule_is_made_and_updated_but_keeps_the_week_it_was_planned(stock):
    s = stock.counts
    assert s.schedule("BOLT") is None
    s.upsert_schedule("BOLT", "A", None, "2026-10-16", T0)
    row = s.schedule("BOLT")
    assert (row["abc_class"], row["last_counted_on"], row["due_on"], row["planned_on"], row["updated_at"]) == ("A", None, "2026-10-16", None, T0)
    s.mark_planned("BOLT", "2026-10-16")
    s.upsert_schedule("BOLT", "B", "2026-10-16", "2026-11-16", T1)
    row = s.schedule("BOLT")
    assert (row["abc_class"], row["last_counted_on"], row["due_on"], row["planned_on"], row["updated_at"]) == ("B", "2026-10-16", "2026-11-16", "2026-10-16", T1)
    s.mark_planned("NUT", "2026-10-16")                                           # an item with no row: nothing happens
    assert s.schedule("NUT") is None


def test_a_count_line_is_reviewed_once(stock):
    s = stock.counts
    assert s.reviewed("CNT-1", "BOLT", "Stores") is False
    s.add_review("CNT-1", "BOLT", "Stores", -4.0, "req-1", T0)
    s.add_review("CNT-1", "BOLT", "Stores", -9.0, "req-2", T1)                    # already reviewed: left as it is
    assert s.reviewed("CNT-1", "BOLT", "Stores") is True
    assert s.reviewed("CNT-1", "BOLT", "Shop") is False and s.reviewed("CNT-1", "NUT", "Stores") is False and s.reviewed("CNT-2", "BOLT", "Stores") is False


# ------------------------------------------------------------------ one unit of work with the core
def test_a_failed_transaction_leaves_nothing_in_the_core_or_the_stock_stores(stores, stock):
    with pytest.raises(RuntimeError):
        with stores.transaction():
            stores.approvals.insert("req-1", "procurement.po", "{}", None, None, T0, None)
            add_alert(stock, "a1")
            stock.proposals.insert("p1", "w1", "BOLT", "awaiting", None, None, None, None, None, None, "", T0)
            raise RuntimeError("the handler failed")
    assert stores.approvals.get("req-1") is None and stock.alerts.get("a1") is None and stock.proposals.get("p1") is None
