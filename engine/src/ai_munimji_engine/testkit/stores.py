"""The checks every storage backend of the engine must pass, written once against the Protocols (`ai_munimji_engine.stores`).

A check is a function `fn(stores, stock)`: `stores` is the core's bundle (`CoreStores`), `stock` the stock pack's (`StockStores`), both on the same storage so that one transaction covers
them. It raises `AssertionError` when the backend does something the Protocols do not promise. No test framework is imported: pytest in erpnext-ai-agents runs them on SQLite and on
memory, and unittest in the Frappe app runs them on DocTypes on a bench; `run_checks(stores, stock)` runs them all and says what each did.

Timestamps are ISO text, as the domain code writes them. A check must leave the storage as it found it only in the sense that every check is given an empty one.
"""
from __future__ import annotations

import json
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

T0, T1, T2, T3 = "2026-10-16T08:00:00+00:00", "2026-10-16T09:00:00+00:00", "2026-10-16T10:00:00+00:00", "2026-10-17T10:00:00+00:00"


@dataclass(frozen=True)
class Check:
    name: str
    run: Callable[[Any, Any], None]


@dataclass
class Result:
    name: str
    status: str                  # pass | fail | error
    detail: str = ""


CHECKS: list[Check] = []


def check(fn: Callable[[Any, Any], None]) -> Callable[[Any, Any], None]:
    CHECKS.append(Check(fn.__name__, fn))
    return fn


@contextmanager
def raises(expected: type[BaseException]) -> Iterator[None]:
    """`with raises(ValueError):` passes when the block raises it (or a subclass) and fails when the block raises nothing."""
    try:
        yield
    except expected:
        return
    raise AssertionError(f"expected {expected.__name__}, nothing was raised")


def run_checks(stores: Any, stock: Any, only: Optional[set[str]] = None, fresh: Optional[Callable[[], tuple[Any, Any]]] = None) -> list[Result]:
    """Run the checks (or those named in `only`). Each needs an empty storage: give `fresh()`, which returns a new (stores, stock) pair, or run one check per call."""
    results = []
    for c in CHECKS:
        if only is not None and c.name not in only:
            continue
        try:
            s, k = fresh() if fresh else (stores, stock)
            c.run(s, k)
            results.append(Result(c.name, "pass"))
        except AssertionError as exc:
            results.append(Result(c.name, "fail", f"{exc}\n{traceback.format_exc(limit=3)}"))
        except Exception as exc:  # noqa: BLE001 - an error is a result too
            results.append(Result(c.name, "error", f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"))
    return results


# =====================================================================================================================
# the core's stores
# =====================================================================================================================
def add_request(stores, approval_id, kind="procurement.po", created=T0, expires=None, ref_type="item", ref_id="BOLT", request=None):
    stores.approvals.insert(approval_id, kind, json.dumps(request or {"title": f"request {approval_id}"}), ref_type, ref_id, created, expires)


# ------------------------------------------------------------------ the ids of delivered messages
@check
def an_id_is_claimed_once_per_channel_and_can_be_given_back(stores, stock):
    s = stores.inbound
    assert s.claim("whatsapp", "m1", T0) is True and s.claim("whatsapp", "m1", T1) is False
    assert s.claim("telegram", "m1", T0) is True                                   # another channel's ids are its own
    s.release("whatsapp", "m1")
    assert s.claim("whatsapp", "m1", T2) is True
    s.release("whatsapp", "never-seen")                                            # nothing to give back: no error


@check
def old_ids_are_forgotten_and_the_count_is_reported(stores, stock):
    s = stores.inbound
    s.claim("whatsapp", "old", T0)
    s.claim("whatsapp", "mid", T1)
    s.claim("whatsapp", "new", T2)
    assert s.purge_before(T1) == 1                                                 # strictly before the cutoff
    assert s.claim("whatsapp", "old", T3) is True and s.claim("whatsapp", "mid", T3) is False
    assert s.purge_before("2000-01-01T00:00:00+00:00") == 0


# ------------------------------------------------------------------ what the scheduler has done
@check
def a_job_run_is_recorded_from_start_to_end(stores, stock):
    s = stores.jobs
    assert s.get("inventory_check") is None and s.all() == []
    s.started("inventory_check", T0)
    row = s.get("inventory_check")
    assert (row["last_start"], row["last_end"], row["last_ok"], row["last_error"], row["runs"], row["failures"]) == (T0, None, None, None, 1, 0)
    s.finished("inventory_check", T1, True, None)
    s.started("inventory_check", T2)
    s.finished("inventory_check", T3, False, "ValueError")
    row = s.get("inventory_check")
    assert (row["last_start"], row["last_end"], row["last_ok"], row["last_error"], row["runs"], row["failures"]) == (T2, T3, 0, "ValueError", 2, 1)


@check
def a_job_that_never_started_leaves_no_row_and_the_rows_come_back_by_name(stores, stock):
    s = stores.jobs
    s.finished("ghost", T0, True, None)
    assert s.get("ghost") is None
    s.started("b_job", T0)
    s.started("a_job", T0)
    assert [r["job"] for r in s.all()] == ["a_job", "b_job"]


# ------------------------------------------------------------------ the hand-off queue
@check
def a_work_order_is_pending_until_its_status_changes(stores, stock):
    s = stores.workorders
    s.insert("w1", "REORDER_NEEDED", '{"item": "BOLT"}', "inventory", T0)
    row = s.get("w1")
    assert (row["type"], row["status"], row["payload_json"], row["source"], row["created_at"], row["updated_at"]) == ("REORDER_NEEDED", "pending", '{"item": "BOLT"}', "inventory", T0, T0)
    s.set_status("w1", "claimed", T1)
    row = s.get("w1")
    assert (row["status"], row["created_at"], row["updated_at"]) == ("claimed", T0, T1)
    s.set_status("nope", "done", T2)                                               # unknown id: nothing happens
    assert s.get("nope") is None


@check
def work_orders_are_listed_newest_first_and_can_be_filtered(stores, stock):
    s = stores.workorders
    s.insert("w1", "REORDER_NEEDED", "{}", "", T0)
    s.insert("w2", "OTHER", "{}", "", T1)
    s.insert("w3", "REORDER_NEEDED", "{}", "", T1)                                 # the same instant as w2: the one made first stays first
    s.set_status("w1", "done", T2)
    assert [r["id"] for r in s.list(None, None, 10)] == ["w2", "w3", "w1"]
    assert [r["id"] for r in s.list(None, "REORDER_NEEDED", 10)] == ["w3", "w1"]
    assert [r["id"] for r in s.list("done", None, 10)] == ["w1"]
    assert [r["id"] for r in s.list(None, None, 1)] == ["w2"]


@check
def a_work_order_id_cannot_be_used_twice(stores, stock):
    stores.workorders.insert("w1", "T", "{}", "", T0)
    with raises(Exception):
        stores.workorders.insert("w1", "T", "{}", "", T1)


# ------------------------------------------------------------------ approval requests
@check
def a_new_request_is_pending_with_nothing_else_filled_in(stores, stock):
    add_request(stores, "a1", expires=T2)
    row = stores.approvals.get("a1")
    assert (row["kind"], row["status"], row["ref_type"], row["ref_id"], row["created_at"], row["expires_at"]) == ("procurement.po", "pending", "item", "BOLT", T0, T2)
    assert (row["decision_json"], row["decided_at"], row["last_reminder_at"], row["reminder_count"], row["status_note"]) == (None, None, None, 0, None)
    assert json.loads(row["request_json"]) == {"title": "request a1"}
    assert stores.approvals.get("missing") is None


@check
def exactly_one_answer_wins(stores, stock):
    add_request(stores, "a1")
    s = stores.approvals
    assert s.decide_if_pending("a1", '{"action": "yes"}', T1) is True
    assert s.decide_if_pending("a1", '{"action": "no"}', T2) is False              # another channel was too late
    assert s.cancel_if_pending("a1", "late") is False and s.expire_if_pending("a1") is False
    row = s.get("a1")
    assert (row["status"], row["decision_json"], row["decided_at"], row["status_note"]) == ("decided", '{"action": "yes"}', T1, None)
    assert s.decide_if_pending("missing", "{}", T1) is False


@check
def a_request_can_be_cancelled_with_a_reason_or_expire_once(stores, stock):
    add_request(stores, "a1")
    add_request(stores, "a2")
    s = stores.approvals
    assert s.cancel_if_pending("a1", "no longer needed") is True and s.cancel_if_pending("a1", "again") is False
    assert (s.get("a1")["status"], s.get("a1")["status_note"]) == ("cancelled", "no longer needed")
    assert s.expire_if_pending("a2") is True and s.expire_if_pending("a2") is False
    assert (s.get("a2")["status"], s.get("a2")["status_note"]) == ("expired", None)


@check
def requests_are_listed_newest_first_filtered_by_status(stores, stock):
    add_request(stores, "a1", created=T0)
    add_request(stores, "a2", created=T2)
    add_request(stores, "a3", created=T1)
    add_request(stores, "a4", created=T1)                                          # the same instant as a3: the one made first stays first
    stores.approvals.decide_if_pending("a3", "{}", T3)
    s = stores.approvals
    assert [r["id"] for r in s.list(None, 10)] == ["a2", "a3", "a4", "a1"]
    assert [r["id"] for r in s.list("pending", 10)] == ["a2", "a4", "a1"]
    assert [r["id"] for r in s.list("decided", 10)] == ["a3"]
    assert [r["id"] for r in s.list(None, 2)] == ["a2", "a3"]


@check
def the_latest_request_of_a_kind(stores, stock):
    add_request(stores, "a1", kind="monitor.down", created=T0)
    add_request(stores, "a2", kind="monitor.down", created=T2)
    add_request(stores, "a3", kind="other", created=T3)
    row = stores.approvals.latest_of_kind("monitor.down")
    assert (row["created_at"], row["status"]) == (T2, "pending")
    assert stores.approvals.latest_of_kind("none") is None


@check
def only_pending_requests_with_a_passed_expiry_are_due(stores, stock):
    add_request(stores, "due", expires=T1)
    add_request(stores, "exact", expires=T2)
    add_request(stores, "later", expires=T3)
    add_request(stores, "never")
    add_request(stores, "answered", expires=T0)
    stores.approvals.decide_if_pending("answered", "{}", T0)
    assert sorted(stores.approvals.due_for_expiry(T2)) == ["due", "exact"]


@check
def a_reminder_is_counted_and_dated(stores, stock):
    add_request(stores, "a1")
    stores.approvals.add_reminder("a1", T1)
    stores.approvals.add_reminder("a1", T2)
    row = stores.approvals.get("a1")
    assert (row["reminder_count"], row["last_reminder_at"]) == (2, T2)
    stores.approvals.add_reminder("missing", T2)                                   # nothing happens


@check
def search_needs_every_term_in_the_request_the_answer_the_note_the_kind_or_the_id(stores, stock):
    add_request(stores, "a1", request={"title": "Reorder Hex Bolt M8", "why": "stock is low"})
    add_request(stores, "a2", request={"title": "Reorder washers"}, kind="procurement.po")
    add_request(stores, "id-777", request={"title": "Something"})
    stores.approvals.decide_if_pending("a2", json.dumps({"action": "yes", "note": "approved by Priya"}), T1)
    stores.approvals.cancel_if_pending("id-777", "duplicate of another")
    s = stores.approvals

    def ids(*terms, **kw):
        return [r["id"] for r in s.search(list(terms), kw.get("kind"), kw.get("status"), kw.get("since"), kw.get("before"), 50)]

    assert ids("hex", "BOLT") == ["a1"]                                            # every term, ignoring case
    assert ids("reorder") == ["a1", "a2"]                                         # made at the same instant: in the order made
    assert ids("priya") == ["a2"]                                                  # in the answer
    assert ids("duplicate") == ["id-777"]                                          # in the note
    assert ids("procurement") == ["a1", "a2", "id-777"]                             # in the kind
    assert ids("id-777") == ["id-777"] and ids("id-77") == []                       # the id must be given whole
    assert ids("hex", "washers") == []


@check
def search_takes_percent_and_underscore_as_plain_text(stores, stock):
    add_request(stores, "a1", request={"title": "100% discount"})
    add_request(stores, "a2", request={"title": "1000 pieces"})
    add_request(stores, "a3", request={"title": "a_b"})
    add_request(stores, "a4", request={"title": "axb"})
    s = stores.approvals
    assert [r["id"] for r in s.search(["100%"], None, None, None, None, 50)] == ["a1"]
    assert [r["id"] for r in s.search(["a_b"], None, None, None, None, 50)] == ["a3"]


@check
def search_filters_by_kind_status_and_dates_and_limits_newest_first(stores, stock):
    add_request(stores, "a1", kind="inventory.alert", created="2026-10-10T08:00:00+00:00")
    add_request(stores, "a2", kind="procurement.po", created="2026-10-12T08:00:00+00:00")
    add_request(stores, "a3", kind="procurement.po", created="2026-10-14T08:00:00+00:00")
    stores.approvals.decide_if_pending("a3", "{}", T0)
    s = stores.approvals

    def ids(**kw):
        return [r["id"] for r in s.search([], kw.get("kind"), kw.get("status"), kw.get("since"), kw.get("before"), kw.get("limit", 50))]

    assert ids() == ["a3", "a2", "a1"]
    assert ids(kind="procurement") == ["a3", "a2"] and ids(kind="PO") == ["a3", "a2"]
    assert ids(status="pending") == ["a2", "a1"] and ids(status="decided") == ["a3"]
    assert ids(since="2026-10-12") == ["a3", "a2"] and ids(before="2026-10-13") == ["a2", "a1"]     # text comparison on the ISO date
    assert ids(since="2026-10-11", before="2026-10-13") == ["a2"]
    assert ids(limit=1) == ["a3"]


@check
def the_answers_and_the_requests_about_the_same_thing_can_be_read(stores, stock):
    add_request(stores, "a1", created=T0, ref_type="item", ref_id="BOLT")
    add_request(stores, "a2", created=T2, ref_type="item", ref_id="BOLT", kind="procurement.manage")
    add_request(stores, "a3", created=T1, ref_type="item", ref_id="NUT")
    stores.approvals.decide_if_pending("a1", '{"action": "yes", "actor_id": "priya"}', T1)
    s = stores.approvals
    assert s.decision_texts() == ['{"action": "yes", "actor_id": "priya"}']
    related = s.with_reference("item", "BOLT", "a1")
    assert [(r["id"], r["kind"], r["status"], r["created_at"], r["status_note"]) for r in related] == [("a2", "procurement.manage", "pending", T2, None)]
    assert s.with_reference("item", "BOLT", "a2")[0]["id"] == "a1"
    assert s.with_reference(None, None, "a1") == []


@check
def the_counts_the_monitoring_reads(stores, stock):
    s = stores.approvals
    assert s.pending_summary() == {"n": 0, "oldest": None}
    add_request(stores, "a1", kind="procurement.po", created=T1)
    add_request(stores, "a2", kind="procurement.po", created=T0)
    add_request(stores, "a3", kind="procurement.po", created=T2)
    add_request(stores, "a4", kind="inventory.alert", created=T2)
    add_request(stores, "a5", kind="inventory.alert", created=T2)
    s.decide_if_pending("a1", '{"action": "yes"}', T3)
    s.decide_if_pending("a3", '{"action": "no"}', T3)
    s.decide_if_pending("a4", '{"action": ""}', T3)                                # an answer without an action counts under its status
    s.expire_if_pending("a5")
    assert s.pending_summary() == {"n": 1, "oldest": T0}
    got = sorted((r["kind"], r["outcome"], r["n"]) for r in s.outcome_counts())
    assert got == [("inventory.alert", "decided", 1), ("inventory.alert", "expired", 1), ("procurement.po", "no", 1), ("procurement.po", "yes", 1)]


@check
def deliveries_are_kept_in_the_order_made_and_counted(stores, stock):
    s = stores.approvals
    add_request(stores, "a1")
    add_request(stores, "a2")
    s.add_delivery("a1", "owner", "telegram", "chat-1", "sent", "m1", None, T0)
    s.add_delivery("a1", "owner", "whatsapp", "+91", "failed", None, "timeout", T1)
    s.add_delivery("a2", "owner", "telegram", "chat-1", "sent", "m2", None, T1)
    rows = s.deliveries("a1")
    assert [(r["channel"], r["status"], r["attempts"], r["external_id"], r["error"]) for r in rows] == [("telegram", "sent", 1, "m1", None), ("whatsapp", "failed", 1, None, "timeout")]
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows) and len({r["id"] for r in rows}) == 2
    assert [r["channel"] for r in s.deliveries("a1", "sent")] == ["telegram"]
    assert sorted((r["channel"], r["status"], r["n"]) for r in s.delivery_counts()) == [("telegram", "sent", 2), ("whatsapp", "failed", 1)]


@check
def only_failed_deliveries_of_pending_requests_below_the_attempt_limit_are_retried(stores, stock):
    s = stores.approvals
    add_request(stores, "pending")
    add_request(stores, "answered")
    s.decide_if_pending("answered", "{}", T1)
    s.add_delivery("pending", "owner", "telegram", "c", "failed", None, "x", T0)
    s.add_delivery("pending", "owner", "whatsapp", "w", "sent", "m", None, T0)
    s.add_delivery("answered", "owner", "telegram", "c", "failed", None, "x", T0)
    failed = s.failed_deliveries(5)
    assert [(r["approval_id"], r["channel"]) for r in failed] == [("pending", "telegram")]
    s.update_delivery(failed[0]["id"], "failed", None, "again", 4, T1)
    assert [r["attempts"] for r in s.failed_deliveries(5)] == [4] and s.failed_deliveries(4) == []
    s.update_delivery(failed[0]["id"], "sent", "m9", None, 5, T2)
    assert s.failed_deliveries(5) == [] and [r["external_id"] for r in s.deliveries("pending", "sent")] == ["m9", "m"]


@check
def a_chat_message_is_traced_back_to_the_request_it_carried(stores, stock):
    s = stores.approvals
    add_request(stores, "a1")
    add_request(stores, "a2")
    s.add_delivery("a1", "owner", "telegram", "chat-1", "sent", "55", None, T0)
    s.add_delivery("a2", "owner", "telegram", "chat-1", "sent", "55", None, T1)    # the same message id reused later: the newest delivery wins
    s.add_delivery("a1", "owner", "telegram", "chat-2", "sent", "56", None, T1)
    assert s.approval_for_message("telegram", "55", "chat-1") == "a2"
    assert s.approval_for_message("telegram", "56", "chat-2") == "a1"
    assert s.approval_for_message("telegram", "55", "chat-9") is None and s.approval_for_message("whatsapp", "55", "chat-1") is None


# ------------------------------------------------------------------ the seal
@check
def the_chain_is_appended_to_and_read_back_in_order(stores, stock):
    s = stores.audit
    assert s.is_empty() and s.last_hash() is None and s.entries() == []
    s.append(T0, "approval", "a1", "decided", "d1", "0" * 64, "h1")
    s.append(T1, "setting", "5", "changed", "d2", "h1", "h2")
    assert not s.is_empty() and s.last_hash() == "h2"
    assert [(e["seq"], e["source"], e["ref_id"], e["event"], e["digest"], e["prev"], e["hash"], e["at"]) for e in s.entries()] == [
        (1, "approval", "a1", "decided", "d1", "0" * 64, "h1", T0), (2, "setting", "5", "changed", "d2", "h1", "h2", T1)]
    assert s.sealed_at("approval", "a1") == T0 and s.sealed_at("approval", "a9") is None and s.sealed_at("setting", "a1") is None


@check
def the_requests_that_still_need_a_seal_are_the_closed_ones_without_an_entry(stores, stock):
    add_request(stores, "pending", created=T0)
    add_request(stores, "first", created=T0)
    add_request(stores, "second", created=T1)
    add_request(stores, "sealed", created=T0)
    stores.approvals.decide_if_pending("first", "{}", T3)
    stores.approvals.decide_if_pending("second", "{}", T2)
    stores.approvals.expire_if_pending("sealed")
    stores.audit.append(T3, "approval", "sealed", "expired", "d", "p", "h")
    assert [r["id"] for r in stores.audit.unsealed_approvals()] == ["second", "first"]        # by when they were answered
    assert stores.audit.unsealed_count() == 2
    assert stores.audit.current_approval("first")["status"] == "decided" and stores.audit.current_approval("gone") is None


@check
def settings_changes_are_sealed_from_the_last_one_on(stores, stock):
    changes = [("min_qty", "1", "2"), ("max_qty", "5", "9"), ("lead", "3", "4")]
    stores.settings.save("global", "*", '{"a": 1}', "priya", T0, changes)
    assert stores.audit.last_sealed_setting() == 0
    assert [r["id"] for r in stores.audit.settings_after(0)] == [1, 2, 3]
    stores.audit.append(T1, "setting", "2", "changed", "d", "p", "h")
    assert stores.audit.last_sealed_setting() == 2
    assert [r["id"] for r in stores.audit.settings_after(2)] == [3]
    assert stores.audit.current_setting(3)["field"] == "lead" and stores.audit.current_setting(99) is None


# ------------------------------------------------------------------ settings
@check
def saved_values_replace_each_other_and_are_removed_with_none(stores, stock):
    s = stores.settings
    assert s.get("system", "*") is None
    s.save("system", "*", '{"a": 1}', "priya", T0, [("a", '"<inherited>"', "1")])
    s.save("system", "*", '{"a": 2}', "sam", T1, [("a", "1", "2")])
    assert s.get("system", "*") == '{"a": 2}' and s.get("system", "other") is None and s.get("item", "*") is None
    s.save("system", "*", None, "sam", T2, [("a", "2", '"<inherited>"')])
    assert s.get("system", "*") is None


@check
def every_change_is_written_to_the_audit_newest_first_and_can_be_filtered(stores, stock):
    s = stores.settings
    s.save("item", "BOLT", '{"min_qty": 5}', "priya", T0, [("min_qty", "null", "5")])
    s.save("group", "Fasteners", '{"lead": 2}', "sam", T1, [("lead", "1", "2"), ("buffer", "0", "3")])
    rows = s.audit(10)
    assert [(r["id"], r["actor"], r["scope_type"], r["scope_key"], r["field"], r["old_json"], r["new_json"], r["ts"]) for r in rows] == [
        (3, "sam", "group", "Fasteners", "buffer", "0", "3", T1), (2, "sam", "group", "Fasteners", "lead", "1", "2", T1), (1, "priya", "item", "BOLT", "min_qty", "null", "5", T0)]
    assert [r["id"] for r in s.audit(10, "item")] == [1] and [r["id"] for r in s.audit(10, None, "Fasteners")] == [3, 2]
    assert [r["id"] for r in s.audit(2)] == [3, 2]
    assert sorted(s.audit_actors()) == ["priya", "sam"]


# ------------------------------------------------------------------ the agents' record
def add_task(stores, task_id, started, agent="inventory", task="brief", approval_id=None, provider="ollama", model="llama3.1", remote=False):
    stores.agents.add_task(task_id, agent, task, started, model, provider, remote, approval_id, "priya", "what is low?")


@check
def a_task_is_running_until_it_is_finished(stores, stock):
    add_task(stores, "t1", T0, approval_id="a1")
    row = stores.agents.get("t1")
    assert (row["status"], row["steps"], row["tool_calls"], row["remote"], row["finished_at"], row["result_json"], row["trace_json"], row["provider"]) == ("running", 0, 0, 0, None, None, None, "ollama")
    stores.agents.finish_task("t1", "ok", T1, '{"answer": 1}', "fine", 3, 2, 1.5, 100, 40, "[]")
    row = stores.agents.get("t1")
    assert (row["status"], row["finished_at"], row["result_json"], row["note"], row["steps"], row["tool_calls"], row["llm_seconds"], row["prompt_tokens"], row["output_tokens"],
            row["trace_json"]) == ("ok", T1, '{"answer": 1}', "fine", 3, 2, 1.5, 100, 40, "[]")
    assert stores.agents.get("nope") is None


@check
def tasks_are_listed_newest_started_first_and_filtered(stores, stock):
    add_task(stores, "t1", T0, agent="inventory", task="brief")
    add_task(stores, "t2", T1, agent="purchasing", task="review", approval_id="a1")
    add_task(stores, "t3", T1, agent="inventory", task="review", approval_id="a1")          # started at the same instant as t2: the later one first
    s = stores.agents
    assert [r["id"] for r in s.list(None, None, None, 10)] == ["t3", "t2", "t1"]
    assert [r["id"] for r in s.list("inventory", None, None, 10)] == ["t3", "t1"]
    assert [r["id"] for r in s.list(None, "review", None, 10)] == ["t3", "t2"]
    assert [r["id"] for r in s.list(None, None, "a1", 10)] == ["t3", "t2"]
    assert [r["id"] for r in s.list(None, None, None, 1)] == ["t3"]


@check
def the_latest_task_counts_a_fallback_but_not_a_failure(stores, stock):
    for tid, started, status in (("t1", T0, "ok"), ("t2", T1, "failed"), ("t3", T2, "fallback")):
        add_task(stores, tid, started)
        stores.agents.finish_task(tid, status, started, None, "", 0, 0, 0.0, 0, 0, "[]")
    assert stores.agents.latest("inventory", "brief", "ok")["id"] == "t3"
    stores.agents.finish_task("t3", "failed", T2, None, "", 0, 0, 0.0, 0, 0, "[]")
    assert stores.agents.latest("inventory", "brief", "ok")["id"] == "t1"
    assert stores.agents.latest("purchasing", "brief", "ok") is None


@check
def tasks_made_for_a_request_or_naming_it_in_their_result(stores, stock):
    add_task(stores, "t1", T1, approval_id="a1")
    add_task(stores, "t2", T0)
    add_task(stores, "t3", T2)
    stores.agents.finish_task("t2", "ok", T0, '{"request_id": "a1", "x": 1}', "", 0, 0, 0.0, 0, 0, "[]")
    stores.agents.finish_task("t3", "ok", T2, '{"request_id": "other"}', "", 0, 0, 0.0, 0, 0, "[]")
    assert [r["id"] for r in stores.agents.tasks_about("a1", '"request_id": "a1"')] == ["t2", "t1"]          # oldest started first


@check
def tool_calls_come_back_by_task_oldest_first_or_newest_first_without_one(stores, stock):
    s = stores.agents
    s.add_tool_call(T0, "agent", "mcp", "stock_overview", "{}", True, 12, "t1", None)
    s.add_tool_call(T1, "agent", "mcp", "prepare_order", '{"a": 1}', False, 7, "t2", "refused")
    s.add_tool_call(T2, "agent", "mcp", "stock_overview", "{}", True, 5, "t1", None)
    assert [r["id"] for r in s.tool_calls("t1", 10)] == [1, 3]
    assert [r["id"] for r in s.tool_calls(None, 2)] == [3, 2]
    row = s.tool_calls("t2", 10)[0]
    assert (row["ok"], row["ms"], row["error"], row["tool"], row["args_json"]) == (0, 7, "refused", "prepare_order", '{"a": 1}')


@check
def tokens_and_usage_are_summed_since_a_moment(stores, stock):
    add_task(stores, "t1", T0, provider="ollama", model="llama", remote=False)
    add_task(stores, "t2", T1, provider="anthropic", model="claude", remote=True)
    add_task(stores, "t3", T2, provider="anthropic", model="claude", remote=True)
    for tid, started, pin, pout, secs in (("t1", T0, 10, 5, 1.0), ("t2", T1, 100, 50, 2.0), ("t3", T2, 300, 100, 3.0)):
        stores.agents.finish_task(tid, "ok", started, None, "", 1, 0, secs, pin, pout, "[]")
    s = stores.agents
    assert s.tokens_since(T0, False) == 565 and s.tokens_since(T0, True) == 550 and s.tokens_since(T2, True) == 400 and s.tokens_since(T3, False) == 0
    by_model = s.usage_by_model(T1)
    assert [(r["provider"], r["model"], r["remote"], r["tasks"], r["tokens_in"], r["tokens_out"], r["seconds"]) for r in by_model] == [("anthropic", "claude", 1, 2, 400, 150, 5.0)]
    assert s.usage_by_model(T0)[0]["provider"] == "anthropic"                      # most tokens first
    daily = s.usage_by_day(T0)
    assert [(r["day"], r["remote"], r["tokens_in"], r["tokens_out"], r["tasks"]) for r in daily] == [("2026-10-16", 0, 10, 5, 1), ("2026-10-16", 1, 400, 150, 2)]


@check
def the_counts_the_monitoring_reads_group_by_agent_task_status_provider_and_model(stores, stock):
    add_task(stores, "t1", T0)
    add_task(stores, "t2", T1)
    add_task(stores, "t3", T2, provider="anthropic", model="claude")
    stores.agents.finish_task("t1", "ok", T0, None, "", 1, 0, 0.0, 10, 5, "[]")
    stores.agents.finish_task("t2", "ok", T1, None, "", 1, 0, 0.0, 20, 5, "[]")
    got = [(r["agent"], r["task"], r["status"], r["provider"], r["model"], r["n"], r["tokens_in"], r["tokens_out"]) for r in stores.agents.metrics_snapshot()]
    assert sorted(got) == [("inventory", "brief", "ok", "ollama", "llama3.1", 2, 30, 10), ("inventory", "brief", "running", "anthropic", "claude", 1, 0, 0)]


@check
def a_task_left_running_by_a_stopped_server_is_failed_and_old_traces_are_pruned(stores, stock):
    add_task(stores, "old", T0)
    add_task(stores, "new", T2)
    assert stores.agents.fail_stale(T3, T1) == 1
    old, new = stores.agents.get("old"), stores.agents.get("new")
    assert (old["status"], old["finished_at"], old["note"]) == ("failed", T3, "the server stopped before this finished") and new["status"] == "running"
    stores.agents.finish_task("old", "ok", T0, None, "", 0, 0, 0.0, 0, 0, '[{"m": 1}]')
    stores.agents.finish_task("new", "ok", T2, None, "", 0, 0, 0.0, 0, 0, '[{"m": 2}]')
    stores.agents.add_tool_call(T0, "a", "mcp", "t", "{}", True, 1, "old", None)
    stores.agents.add_tool_call(T2, "a", "mcp", "t", "{}", True, 1, "new", None)
    assert stores.agents.prune(T1) == 1
    assert stores.agents.get("old")["trace_json"] is None and stores.agents.get("new")["trace_json"] == '[{"m": 2}]'
    assert [r["task_id"] for r in stores.agents.tool_calls(None, 10)] == ["new"]


# ------------------------------------------------------------------ one unit of work for all of them
@check
def a_transaction_that_fails_leaves_nothing_behind_in_any_store(stores, stock):
    with raises(RuntimeError):
        with stores.transaction():
            add_request(stores, "a1")
            stores.inbound.claim("whatsapp", "m1", T0)
            stores.workorders.insert("w1", "T", "{}", "", T0)
            raise RuntimeError("the handler failed")
    assert stores.approvals.get("a1") is None and stores.workorders.get("w1") is None
    assert stores.inbound.claim("whatsapp", "m1", T1) is True                       # the id was not kept


@check
def a_transaction_that_succeeds_keeps_everything_and_nested_use_joins_the_outer_one(stores, stock):
    with stores.transaction():
        add_request(stores, "a1")
        with stores.approvals.transaction():
            stores.workorders.insert("w1", "T", "{}", "", T0)
    assert stores.approvals.get("a1") and stores.workorders.get("w1")
    with raises(RuntimeError):
        with stores.transaction():
            add_request(stores, "a2")
            with stores.transaction():
                add_request(stores, "a3")
            raise RuntimeError("after the inner one finished")
    assert stores.approvals.get("a2") is None and stores.approvals.get("a3") is None      # the inner one joined the outer, which failed


@check
def the_storage_says_it_is_ready(stores, stock):
    checks = stores.health()
    assert [name for name, _, _ in checks] == ["database", "migrations"] and all(ok for _, ok, _ in checks)


# =====================================================================================================================
# the stock pack's stores
# =====================================================================================================================
# ------------------------------------------------------------------ alerts
def add_alert(stock, alert_id, item="BOLT", now=T0, kind="BREACH", severity=2):
    stock.alerts.insert(alert_id, item, kind, severity, now, json.dumps({"days_cover": 2}), "low on bolts", "run-1")


@check
def a_new_alert_is_awaiting_and_nothing_else_is_set(stores, stock):
    add_alert(stock, "a1")
    row = stock.alerts.get("a1")
    assert (row["item_code"], row["kind"], row["severity"], row["status"], row["created_at"], row["updated_at"], row["run_id"]) == ("BOLT", "BREACH", 2, "awaiting", T0, T0, "run-1")
    assert (row["approval_id"], row["snoozed_until"], row["suppress_until"], row["cooldown_until"], row["status_note"]) == (None, None, None, None, None)
    assert json.loads(row["analysis_json"]) == {"days_cover": 2} and row["reasoning_text"] == "low on bolts"
    assert stock.alerts.get("nope") is None
    with raises(Exception):
        add_alert(stock, "a1")                                                   # the id is the key


@check
def alerts_are_found_by_item_by_status_and_by_the_request_they_carry(stores, stock):
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


@check
def a_status_change_keeps_the_dates_it_is_not_given(stores, stock):
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


@check
def a_new_notice_is_sent_and_can_get_a_request_and_a_status(stores, stock):
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


@check
def the_active_notices_of_an_item_and_the_newest_of_them(stores, stock):
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


@check
def a_new_proposal_holds_what_was_recommended(stores, stock):
    add_proposal(stock, "p1", supplier="S1", supplier_name="Acme", qty=100.0, rate=2.5, total=250.0, ranking_json='{"a": 1}', note="cheapest")
    row = stock.proposals.get("p1")
    assert (row["work_order_id"], row["item_code"], row["status"], row["supplier"], row["supplier_name"], row["qty"], row["rate"], row["total"], row["ranking_json"],
            row["status_note"], row["created_at"], row["updated_at"]) == ("w1", "BOLT", "awaiting", "S1", "Acme", 100.0, 2.5, 250.0, '{"a": 1}', "cheapest", T0, T0)
    assert (row["approval_id"], row["po_name"], row["snoozed_until"]) == (None, None, None) and stock.proposals.get("none") is None
    with raises(Exception):
        add_proposal(stock, "p1")


@check
def a_proposal_is_revised_ranked_linked_and_closed(stores, stock):
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
    with raises(ValueError):
        s.update_status("p1", "ordered", "", T3, {"supplier": "x"})                # only po_name and snoozed_until may be set that way


@check
def open_snoozed_and_listed_proposals(stores, stock):
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
@check
def runs_are_logged_and_the_newest_real_ones_come_first(stores, stock):
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
@check
def an_answer_is_kept_under_its_key_and_replaced_by_the_next(stores, stock):
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


@check
def the_chat_log_keeps_what_was_asked_in_order(stores, stock):
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


@check
def messages_are_listed_by_order_or_request_oldest_first(stores, stock):
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


@check
def a_reply_is_matched_to_the_latest_open_message_with_the_same_hash(stores, stock):
    add_message(stock, "m1", address_hash="H", hash_kind="hmac1", sent_at=T0)
    add_message(stock, "m2", address_hash="H", hash_kind="hmac1", sent_at=T2)
    add_message(stock, "m3", address_hash="H", hash_kind=None, sent_at=T1)       # an old, unkeyed hash
    add_message(stock, "m4", address_hash="H", hash_kind="hmac1", sent_at=T3, status="failed")
    s = stock.vendors
    assert s.open_message("H", "hmac1", T0)["id"] == "m2"                         # not the failed one, not an older one
    assert s.open_message("H", "", T0)["id"] == "m3" and s.open_message("H", "hmac1", T3) is None and s.open_message("X", "hmac1", T0) is None
    assert s.close_replies("chase", "PO-1", T3) == 4
    assert s.open_message("H", "hmac1", T0) is None


@check
def closing_an_order_closes_its_messages_once_and_forgets_their_hash(stores, stock):
    add_message(stock, "m1", ref_id="PO-1", address_hash="H1", sent_at=T0)
    add_message(stock, "m2", ref_id="PO-1", address_hash="H2", sent_at=T1)
    add_message(stock, "m3", ref_id="PO-2", address_hash="H3", sent_at=T1)
    s = stock.vendors
    assert s.open_chase_refs() == {"PO-1", "PO-2"}
    assert s.close_replies("chase", "PO-1", T2) == 2 and s.close_replies("chase", "PO-1", T3) == 0
    assert s.open_chase_refs() == {"PO-2"} and s.open_message("H1", "hmac1", T0) is None and s.open_message("H3", "hmac1", T0)["id"] == "m3"
    assert s.forget_hashes_before(T1) == 0                                        # m1 and m2 lost their hash on closing; m3 was sent at T1, not before
    assert s.forget_hashes_before(T2) == 1 and s.open_message("H3", "hmac1", T0) is None


@check
def replies_are_kept_counted_and_recognised_when_repeated(stores, stock):
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
@check
def a_chase_is_started_asked_again_sent_and_closed(stores, stock):
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
    with raises(Exception):
        s.start("PO-1", "Acme", "BOLT", "2026-10-10", T0, "req-9", T0)


@check
def a_receipt_is_checked_once_and_acknowledged_by_its_request(stores, stock):
    s = stock.receipts
    assert s.get("GRN-1") is None
    s.add("GRN-1", "Acme", "clean", "[]", None, T0)
    s.add("GRN-2", "Acme", "flagged", '[{"kind": "price"}]', "req-1", T1)
    s.add("GRN-3", "Beta", "flagged", '[{"kind": "qty"}]', "req-2", T2)
    assert s.get("GRN-1")["status"] == "clean" and s.get("GRN-2")["approval_id"] == "req-1"
    with raises(Exception):
        s.add("GRN-1", "Acme", "clean", "[]", None, T3)
    assert sorted((r["supplier"], r["problems_json"]) for r in s.problems_since(T1)) == [("Acme", '[{"kind": "price"}]'), ("Beta", '[{"kind": "qty"}]')]
    assert len(s.problems_since(T0)) == 3 and s.problems_since(T3) == []
    s.acknowledge("req-1")
    assert s.get("GRN-2")["status"] == "acknowledged" and s.get("GRN-3")["status"] == "flagged" and s.get("GRN-1")["status"] == "clean"


@check
def a_quote_round_is_proposed_updated_and_listed_newest_first(stores, stock):
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
    with raises(ValueError):
        s.update("q1", {"qty": 1})                                                # only the columns the rounds change may be set


# ------------------------------------------------------------------ transfers and counts
@check
def a_transfer_is_proposed_updated_found_by_its_request_and_listed(stores, stock):
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
    with raises(ValueError):
        s.update("t1", {"qty": 99})


@check
def the_count_schedule_is_made_and_updated_but_keeps_the_week_it_was_planned(stores, stock):
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


@check
def a_count_line_is_reviewed_once(stores, stock):
    s = stock.counts
    assert s.reviewed("CNT-1", "BOLT", "Stores") is False
    s.add_review("CNT-1", "BOLT", "Stores", -4.0, "req-1", T0)
    s.add_review("CNT-1", "BOLT", "Stores", -9.0, "req-2", T1)                    # already reviewed: left as it is
    assert s.reviewed("CNT-1", "BOLT", "Stores") is True
    assert s.reviewed("CNT-1", "BOLT", "Shop") is False and s.reviewed("CNT-1", "NUT", "Stores") is False and s.reviewed("CNT-2", "BOLT", "Stores") is False


# ------------------------------------------------------------------ one unit of work with the core
@check
def a_failed_transaction_leaves_nothing_in_the_core_or_the_stock_stores(stores, stock):
    with raises(RuntimeError):
        with stores.transaction():
            stores.approvals.insert("req-1", "procurement.po", "{}", None, None, T0, None)
            add_alert(stock, "a1")
            stock.proposals.insert("p1", "w1", "BOLT", "awaiting", None, None, None, None, None, None, "", T0)
            raise RuntimeError("the handler failed")
    assert stores.approvals.get("req-1") is None and stock.alerts.get("a1") is None and stock.proposals.get("p1") is None
