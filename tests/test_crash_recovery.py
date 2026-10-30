"""The process stops between "approved" and "recorded" (Phase 3 of the hardening brief).

The order of events when someone presses Create order: ERPNext is asked to create the draft, and only then is the answer written to the database. If the process stops in
between, ERPNext has the draft and the database still says "waiting". These tests stop the process there (the real database file, the real REST client against a small ERPNext
that keeps its drafts), start the application again on the same file, and check that the restart finishes the job once: the answer is recorded with the approver written on the
draft, the approver is told once, and no second draft can follow, not even after the request would have expired."""
import logging

import pytest

from conftest import OWNER, FakeHTTP
from erp_agents.core.approvals.channels.simple import InboxChannel
from erp_agents.core.approvals.channels.telegram import TelegramChannel
from erp_agents.core.approvals.channels.whatsapp import WhatsAppChannel
from erp_agents.core.approvals.inbound import process_inbound
from erp_agents.core.approvals.models import InboundEvent
from erp_agents.core.scheduler import AgentScheduler
from erp_agents.core.workorders import REORDER_NEEDED
from fake_erpnext import FakeERPNext, HybridERP
from test_procurement import three, wo_payload

CHAT, PHONE, OTHER = "555", "919800000000", "919811111111"
CFG = {"approvals": {"strategy": "all", "recipients": [
    {"id": "owner", "name": "Owner", "roles": ["admin", "approver"], "channels": [{"type": "telegram", "address": CHAT}, {"type": "whatsapp", "address": PHONE}, {"type": "inbox"}]},
    {"id": "second", "name": "Second", "roles": ["approver"], "channels": [{"type": "whatsapp", "address": OTHER}]}]}}


class Crash(BaseException):
    """The process stops here: not an Exception, so nothing in the application can catch it and carry on."""


def stop_after_creating(erp):
    """Let ERPNext create the document, then stop the process before the answer can be written down."""
    for name in ("create_purchase_order", "create_material_request", "create_stock_transfer"):
        def crashing(draft, acting=None, real=getattr(erp, name)):
            real(draft, acting)
            raise Crash()
        setattr(erp, name, crashing)


def start(make_app, clock, server, db_path, crash=False):
    """An application on the database file and the ERPNext server: the first start, or (same arguments) the restart."""
    erp = HybridERP(server, today=clock().date(), scenarios=False)
    erp.add_item("BOLT-M8", "Bolt M8 x 40", lambda age: 40, 240, group="Fasteners", uom="pcs", lead_time_days=5)
    erp.add_vendors("BOLT-M8", three())
    erp.add_item("BOLT", "Bolt", lambda age: 10, 0, group="Fasteners", uom="pcs", warehouses={"Stores": {"on_hand": 100, "reserved": 60}, "Shop floor": {"on_hand": 40}})
    tg, wa = FakeHTTP(), FakeHTTP()
    channels = {"telegram": TelegramChannel("123:TOKEN", webhook_secret="s", http=tg), "whatsapp": WhatsAppChannel("T", "PNID", app_secret="s", verify_token="v", http=wa), "inbox": InboxChannel()}
    app = make_app(erp=erp, channels=channels, cfg=CFG, db_path=db_path)
    if crash:
        stop_after_creating(erp)
    return app, wa


def press(app, approval_id, action="approve", channel="telegram", sender=CHAT):
    return process_inbound(InboundEvent(channel=channel, sender=sender, approval_id=approval_id, action=action, ack_token="cq1" if channel == "telegram" else None), app.router, app.approvals)


def request_for_an_order(app):
    app.workorders.emit(REORDER_NEEDED, wo_payload())
    assert app.procurement.run().proposed == 1
    return next(r for r in app.approvals.list("pending") if r.kind.startswith("procurement."))


def wa_texts(http, number):
    return [c[1]["text"]["body"] for c in http.calls if c[1] and c[1].get("type") == "text" and c[1].get("to") == number]


def announced(texts):
    return [t for t in texts if "was created in ERPNext" in t]


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "assistant.db")


# ------------------------------------------------------------------ an order
def test_a_restart_finishes_an_order_that_was_created_but_never_recorded(make_app, clock, db_path):
    server = FakeERPNext()
    app, _ = start(make_app, clock, server, db_path, crash=True)
    pending = request_for_an_order(app)
    with pytest.raises(Crash):
        press(app, pending.id)
    assert len(server.stored("Purchase Order")) == 1                                       # ERPNext has the draft...
    app.db.close()
    again, wa = start(make_app, clock, server, db_path)
    assert again.approvals.get(pending.id).status == "pending" and again.proposals.list()[0].status == "awaiting"        # ...and the database still says "waiting"

    def never(*a, **k):
        raise AssertionError("finishing an answer must not ask the ERP to create or remove anything")
    for name in ("create_purchase_order", "create_material_request", "create_stock_transfer", "cancel_purchase_order"):
        setattr(again.erp, name, never)
    assert again.approvals.reconcile_pending() == [pending.id]
    record = again.approvals.get(pending.id)
    name = server.stored("Purchase Order")[0]["name"]
    assert record.status == "decided" and (record.decision.action, record.decision.actor_id, record.decision.channel) == ("approve", "owner", "telegram")
    assert "finished after a restart" in record.decision.note
    prop = again.proposals.list()[0]
    assert prop.status == "ordered" and prop.po_name == name and again.workorders.list()[0]["status"] == "done"
    assert len(server.stored("Purchase Order")) == 1                                       # nothing was created again


def test_the_approvers_are_told_what_happened_once(make_app, clock, db_path):
    server = FakeERPNext()
    app, _ = start(make_app, clock, server, db_path, crash=True)
    pending = request_for_an_order(app)
    with pytest.raises(Crash):
        press(app, pending.id)
    app.db.close()
    again, wa = start(make_app, clock, server, db_path)
    again.approvals.reconcile_pending()
    again.approvals.reconcile_pending()                                                     # a second pass has nothing to do
    name = server.stored("Purchase Order")[0]["name"]
    for number in (PHONE, OTHER):
        told = announced(wa_texts(wa, number))
        assert len(told) == 1 and name in told[0] and "had stopped before it could write the answer down" in told[0], (number, told)


def test_it_is_one_log_line_and_one_count_and_a_second_pass_adds_nothing(make_app, clock, db_path, caplog):
    from erp_agents.core import metrics
    metrics.REGISTRY.reset()
    caplog.set_level(logging.INFO, logger="erp_agents.calls")
    try:
        server = FakeERPNext()
        app, _ = start(make_app, clock, server, db_path, crash=True)
        pending = request_for_an_order(app)
        with pytest.raises(Crash):
            press(app, pending.id)
        app.db.close()
        again, _ = start(make_app, clock, server, db_path)
        metrics.configure(True)                                                             # (building an application sets metrics as its own settings say: off)
        assert again.approvals.reconcile_pending() == [pending.id] and again.approvals.reconcile_pending() == []
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("decision_recovered")]
        assert len(lines) == 1 and "kind=procurement.po" in lines[0]
        assert metrics.DECISIONS_RECOVERED.values[("procurement.po",)] == 1
    finally:
        metrics.configure(False)
        metrics.REGISTRY.reset()


def test_the_request_is_finished_before_it_can_expire_so_no_second_draft_follows(make_app, clock, db_path):
    """Without this, the request expires, the reorder is asked again as a NEW proposal with a new reference, and approving that one makes a second draft."""
    server = FakeERPNext()
    app, _ = start(make_app, clock, server, db_path, crash=True)
    pending = request_for_an_order(app)
    with pytest.raises(Crash):
        press(app, pending.id)
    app.db.close()
    clock.advance(days=30)                                                                  # the application was down for a month
    again, _ = start(make_app, clock, server, db_path)
    assert AgentScheduler(again)._maintenance_job() is True                                 # the housekeeping that expires old requests
    assert again.approvals.get(pending.id).status == "decided"                              # ...finished this one first
    assert again.procurement.run().proposed == 0 and len(server.stored("Purchase Order")) == 1


def test_when_no_draft_exists_nothing_is_changed_and_the_button_still_works(make_app, clock, db_path):
    server = FakeERPNext()
    app, _ = start(make_app, clock, server, db_path)
    pending = request_for_an_order(app)                                                     # the process stopped before ERPNext was even asked
    app.db.close()
    again, _ = start(make_app, clock, server, db_path)
    assert again.approvals.reconcile_pending() == [] and again.approvals.get(pending.id).status == "pending"
    assert press(again, pending.id).ok and len(server.stored("Purchase Order")) == 1


def test_an_unreachable_erpnext_leaves_it_for_the_next_pass(make_app, clock, db_path):
    import requests
    server = FakeERPNext()
    app, _ = start(make_app, clock, server, db_path, crash=True)
    pending = request_for_an_order(app)
    with pytest.raises(Crash):
        press(app, pending.id)
    app.db.close()
    again, _ = start(make_app, clock, server, db_path)
    real = server.send

    def down(request, **kw):
        raise requests.ConnectionError("refused")
    server.send = down
    assert again.approvals.reconcile_pending() == [] and again.approvals.get(pending.id).status == "pending"
    server.send = real
    assert again.approvals.reconcile_pending() == [pending.id]


def test_a_draft_whose_approver_cannot_be_read_is_left_alone_and_reported_once(make_app, clock, db_path, caplog):
    server = FakeERPNext()
    app, _ = start(make_app, clock, server, db_path, crash=True)
    pending = request_for_an_order(app)
    with pytest.raises(Crash):
        press(app, pending.id)
    server.stored("Purchase Order")[0]["custom_agent_approved_by"] = ""                    # somebody cleared the field in ERPNext
    app.db.close()
    again, _ = start(make_app, clock, server, db_path)
    caplog.set_level(logging.WARNING)
    for _ in range(3):
        assert again.approvals.reconcile_pending() == []
    assert again.approvals.get(pending.id).status == "pending"
    assert len([r for r in caplog.records if "approver" in r.getMessage() and pending.id in r.getMessage()]) == 1


def test_a_person_pressing_the_button_and_the_recovery_never_record_the_answer_twice(make_app, clock, db_path):
    server = FakeERPNext()
    app, _ = start(make_app, clock, server, db_path, crash=True)
    pending = request_for_an_order(app)
    with pytest.raises(Crash):
        press(app, pending.id)
    app.db.close()
    again, wa = start(make_app, clock, server, db_path)
    assert press(again, pending.id).ok                                                      # the person tried again before the recovery ran
    assert again.approvals.reconcile_pending() == []
    assert len(server.stored("Purchase Order")) == 1 and len(announced(wa_texts(wa, PHONE))) == 1


def test_answers_that_are_already_closed_are_never_touched(make_app, clock, db_path):
    server = FakeERPNext()
    app, _ = start(make_app, clock, server, db_path)
    pending = request_for_an_order(app)
    assert app.approvals.decide(pending.id, "dismiss", OWNER, "app").ok
    assert app.approvals.reconcile_pending() == [] and app.approvals.get(pending.id).decision.action == "dismiss" and server.stored("Purchase Order") == []


# ------------------------------------------------------------------ a requisition and a stock transfer
def test_a_requisition_is_finished_the_same_way(make_app, clock, db_path):
    server = FakeERPNext()
    app, _ = start(make_app, clock, server, db_path, crash=True)
    pending = request_for_an_order(app)
    with pytest.raises(Crash):
        press(app, pending.id, action="requisition")
    app.db.close()
    again, _ = start(make_app, clock, server, db_path)
    assert again.approvals.reconcile_pending() == [pending.id]
    assert again.approvals.get(pending.id).decision.action == "requisition" and again.proposals.list()[0].po_name == server.stored("Material Request")[0]["name"]
    assert len(server.stored("Material Request")) == 1 and server.stored("Purchase Order") == []


def test_a_stock_transfer_is_finished_the_same_way(make_app, clock, db_path):
    server = FakeERPNext()
    app, _ = start(make_app, clock, server, db_path, crash=True)
    prepared = app.transfers.prepare("BOLT", 30, "Stores", "Shop floor", "anna")
    with pytest.raises(Crash):
        press(app, prepared["approval_id"])
    app.db.close()
    again, _ = start(make_app, clock, server, db_path)
    assert again.approvals.reconcile_pending() == [prepared["approval_id"]]
    t = again.transfers.get(prepared["transfer"])
    assert t["status"] == "created" and t["entry_name"] == server.stored("Stock Entry")[0]["name"] and len(server.stored("Stock Entry")) == 1


# ------------------------------------------------------------------ when it runs
def test_the_housekeeping_job_runs_the_recovery_first(make_app, clock, db_path):
    server = FakeERPNext()
    app, _ = start(make_app, clock, server, db_path, crash=True)
    pending = request_for_an_order(app)
    with pytest.raises(Crash):
        press(app, pending.id)
    app.db.close()
    again, _ = start(make_app, clock, server, db_path)
    assert AgentScheduler(again)._maintenance_job() is True and again.approvals.get(pending.id).status == "decided"


def test_serve_runs_the_recovery_before_the_scheduler_starts(tmp_path, monkeypatch):
    import erp_agents.core.cli as cli
    from erp_agents.core.approvals.service import ApprovalService
    calls = []
    monkeypatch.setattr(ApprovalService, "reconcile_pending", lambda self: calls.append(1) or [])
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: calls.append("serving"))
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(f"database: {{path: '{(tmp_path / 'x.db').as_posix()}'}}\nerp: {{type: mock}}\nauth: {{tokens: []}}\n", encoding="utf-8")
    assert cli.main(["--config", str(cfg), "serve"]) == 0
    assert calls == [1, "serving"]


# ------------------------------------------------------------------ the pieces
def generic(kind="test.thing"):
    from erp_agents.core.approvals.models import ApprovalOption, ApprovalRequest, Section
    return ApprovalRequest(kind=kind, title="Do the thing", headline="Please decide", severity="HIGH", sections=[Section(heading="Why", lines=["because"])],
                           options=[ApprovalOption(action="yes", label="Yes", style="primary"), ApprovalOption(action="no", label="No")])


@pytest.mark.parametrize("text, expected", [("owner via telegram", ("owner", "telegram")), ("priya@example.test via app", ("priya@example.test", "app")),
                                            ("  owner   via   whatsapp ", ("owner", "whatsapp")), ("a via b via c", ("a via b", "c")),
                                            ("", None), (None, None), ("owner", None), ("via telegram", None), ("owner via ", None), ("owner via " + "x" * 80, None)])
def test_the_approver_written_on_a_document_is_read_back(text, expected):
    from erp_agents.core.approvals.service import parse_approver
    assert parse_approver(text) == expected


def test_a_kind_decides_what_was_answered_and_a_kind_without_the_hook_is_left_alone(make_app):
    from erp_agents.core.approvals.models import Decision
    from erp_agents.core.approvals.service import KindHandler
    app = make_app()
    seen = []
    app.approvals.register_handler("test.fixed", KindHandler(on_decision=lambda rec, d: seen.append((rec.id, d.action, d.actor_id, d.params)),
                                                             reconcile=lambda rec: Decision(approval_id=rec.id, action="yes", actor_id="priya", channel="app", params={"x": 1}, decided_at=app.clock())))
    app.approvals.register_handler("test.plain", KindHandler())
    one, two = app.approvals.submit(generic("test.fixed")), app.approvals.submit(generic("test.plain"))
    assert app.approvals.reconcile_pending() == [one.id]
    assert seen == [(one.id, "yes", "priya", {"x": 1})] and app.approvals.get(two.id).status == "pending" and app.approvals.get(one.id).status == "decided"


def test_a_hook_that_fails_does_not_stop_the_others_and_complains_once(make_app, caplog):
    from erp_agents.core.approvals.models import Decision
    from erp_agents.core.approvals.service import KindHandler
    app = make_app()

    def broken(rec):
        raise RuntimeError("ERPNext is not answering")
    app.approvals.register_handler("test.broken", KindHandler(reconcile=broken))
    app.approvals.register_handler("test.fine", KindHandler(reconcile=lambda rec: Decision(approval_id=rec.id, action="yes", actor_id="priya", channel="app", decided_at=app.clock())))
    bad, good = app.approvals.submit(generic("test.broken")), app.approvals.submit(generic("test.fine"))
    caplog.set_level(logging.WARNING)
    for _ in range(3):
        app.approvals.reconcile_pending()
    assert app.approvals.get(good.id).status == "decided" and app.approvals.get(bad.id).status == "pending"
    assert len([r for r in caplog.records if bad.id in r.getMessage() and "could not check" in r.getMessage()]) == 1


def test_an_answer_the_kind_refuses_leaves_the_request_waiting_and_says_why(make_app, caplog):
    from erp_agents.core.approvals.models import Decision, DecisionRefused
    from erp_agents.core.approvals.service import KindHandler

    def refuse(rec, decision):
        raise DecisionRefused("this cannot be applied")
    app = make_app()
    app.approvals.register_handler("test.refusing", KindHandler(on_decision=refuse, reconcile=lambda rec: Decision(approval_id=rec.id, action="yes", actor_id="priya", channel="app",
                                                                                                                   decided_at=app.clock())))
    rec = app.approvals.submit(generic("test.refusing"))
    caplog.set_level(logging.WARNING)
    assert app.approvals.reconcile_pending() == [] and app.approvals.get(rec.id).status == "pending"
    assert any("could not finish the answer" in r.getMessage() and "this cannot be applied" in r.getMessage() for r in caplog.records)


def test_both_erp_implementations_find_a_document_by_its_reference_with_its_approver(clock):
    from erp_agents.core.erp.base import FoundDocument, PurchaseOrderDraft
    from erp_agents.core.erp.mock import MockERP
    from fake_erpnext import rest_client
    due = clock().date()
    draft = PurchaseOrderDraft("S-A", "BOLT", 10, 1.0, "Nos", due, reference="prop-1", approved_by="priya via whatsapp")
    server = FakeERPNext()
    rest = rest_client(server)
    mock = MockERP(today=due, scenarios=False)
    mock.add_item("BOLT", "Bolt", lambda age: 1, 10)
    for erp, doctype in ((rest, "Purchase Order"), (mock, "Purchase Order")):
        assert erp.find_by_reference(doctype, "prop-1") is None
        made = erp.create_purchase_order(draft)
        found = erp.find_by_reference(doctype, "prop-1")
        assert found == FoundDocument(doctype, made.name, "priya via whatsapp")
        assert erp.find_by_reference(doctype, "another") is None and erp.find_by_reference(doctype, "") is None
    server.stored("Purchase Order")[0]["docstatus"] = 2                                       # cancelled in ERPNext: a cancelled draft does not count
    assert rest.find_by_reference("Purchase Order", "prop-1") is None


def test_an_order_that_replaces_another_is_finished_without_taking_the_earlier_one_back_again(make_app, clock, db_path):
    """A changed order is made after the earlier one is removed. Finishing it after a restart must not remove anything: that was done before the process stopped."""
    server = FakeERPNext()
    app, _ = start(make_app, clock, server, db_path, crash=True)
    pending = request_for_an_order(app)
    prop = app.proposals.list()[0]
    app.proposals.set_ranking(prop.id, {**(prop.ranking or {}), "replaces_po": "PUR-ORD-OLD"})
    with pytest.raises(Crash):
        press(app, pending.id)
    app.db.close()
    again, _ = start(make_app, clock, server, db_path)

    def never(*a, **k):
        raise AssertionError("nothing may be removed or created while finishing")
    for name in ("create_purchase_order", "cancel_purchase_order"):
        setattr(again.erp, name, never)
    assert again.approvals.reconcile_pending() == [pending.id]
    ranking = again.proposals.list()[0].ranking
    assert ranking["replaces_po"] is None and ranking["replaced"] == ["PUR-ORD-OLD"]          # the books say the earlier order was replaced, as they do after a normal answer


def test_a_stale_second_pass_cannot_record_the_answer_twice(make_app, clock, db_path):
    server = FakeERPNext()
    app, _ = start(make_app, clock, server, db_path, crash=True)
    pending = request_for_an_order(app)
    with pytest.raises(Crash):
        press(app, pending.id)
    app.db.close()
    again, wa = start(make_app, clock, server, db_path)
    stale = again.approvals.get(pending.id)                                                  # read before the first pass finished it
    decision = again.approvals._handlers[stale.kind].reconcile(stale)
    assert again.approvals.reconcile_pending() == [pending.id]
    second = again.approvals._apply(stale, decision, "", None, recovered=True)               # the other pass, a moment later, with what it read earlier
    assert not second.ok and second.status == "already_decided"
    assert len(announced(wa_texts(wa, PHONE))) == 1 and len(server.stored("Purchase Order")) == 1
