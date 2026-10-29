"""Fault injection on creating a document in ERPNext (Phase 3 of the hardening brief): ERPNext times out or drops the connection AFTER it stored the draft, answers with a 5xx,
or answers with a body that is cut off. In every case: no second draft, and the person is told the truth. First against the real REST client, then through the approval of an
order, a requisition and a stock transfer."""
from datetime import date

import pytest

from conftest import OWNER
from erp_agents.core.erp.base import PurchaseOrderDraft, RequisitionDraft, StockTransferDraft
from erp_agents.core.workorders import REORDER_NEEDED
from fake_erpnext import FakeERPNext, HybridERP, rest_client
from test_procurement import three, wo_payload

DUE = date(2026, 11, 1)
KINDS = {
    "order": ("Purchase Order", lambda c, ref: c.create_purchase_order(PurchaseOrderDraft("S-A", "BOLT", 10, 1.0, "Nos", DUE, reference=ref, approved_by="priya via whatsapp"))),
    "requisition": ("Material Request", lambda c, ref: c.create_material_request(RequisitionDraft(item_code="BOLT", qty=10, uom="Nos", required_by=DUE, reference=ref,
                                                                                                  approved_by="priya via whatsapp"))),
    "transfer": ("Stock Entry", lambda c, ref: c.create_stock_transfer(StockTransferDraft("BOLT", 30, "pcs", "Stores", "Shop floor", reference=ref, approved_by="priya via whatsapp"))),
}


# ------------------------------------------------------------------ the REST client
@pytest.mark.parametrize("fault", ["lose", "timeout"])
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_a_timeout_or_a_dropped_connection_after_the_draft_was_stored_leaves_one_draft(kind, fault):
    doctype, create = KINDS[kind]
    server = FakeERPNext([fault])
    client = rest_client(server)
    first = create(client, "prop-1")
    assert not first.ok and "could not reach ERPNext" in first.error and len(server.stored(doctype)) == 1       # ERPNext did store it
    second = create(client, "prop-1")
    assert second.ok and second.name == server.stored(doctype)[0]["name"] and len(server.stored(doctype)) == 1       # found by its reference: nothing new
    assert server.stored(doctype)[0]["custom_agent_ref"] == "prop-1" and server.stored(doctype)[0]["custom_agent_approved_by"] == "priya via whatsapp"


@pytest.mark.parametrize("fault", ["500", "503"])
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_a_5xx_stores_nothing_says_so_and_the_retry_makes_the_one_draft(kind, fault):
    doctype, create = KINDS[kind]
    server = FakeERPNext([fault])
    client = rest_client(server)
    first = create(client, "prop-1")
    assert not first.ok and f"HTTP {fault}" in first.error and server.stored(doctype) == []
    second = create(client, "prop-1")
    assert second.ok and len(server.stored(doctype)) == 1


def test_a_refusal_with_a_reason_passes_the_reason_on_and_stores_nothing():
    server = FakeERPNext(["refuse"])
    result = KINDS["order"][1](rest_client(server), "prop-1")
    assert not result.ok and "HTTP 417" in result.error and "Supplier S-A is disabled" in result.error and server.stored("Purchase Order") == []


@pytest.mark.parametrize("fault", ["partial", "nameless"])
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_an_answer_that_is_cut_off_does_not_hide_a_draft_that_was_stored(kind, fault):
    """ERPNext stored the draft and said 200, but the body could not be read (or has no name). The caller must not see an exception, and must not be left thinking nothing was made."""
    doctype, create = KINDS[kind]
    server = FakeERPNext([fault])
    client = rest_client(server)
    result = create(client, "prop-1")
    assert result.ok and result.name == server.stored(doctype)[0]["name"] and len(server.stored(doctype)) == 1       # it looked the draft up by its reference
    assert create(client, "prop-1").name == result.name and len(server.stored(doctype)) == 1


def test_an_unreadable_answer_to_a_request_without_a_reference_says_a_draft_may_exist():
    server = FakeERPNext(["partial"])
    result = KINDS["order"][1](rest_client(server), "")
    assert not result.ok and "may have been created" in result.error and "check ERPNext" in result.error


def test_a_cut_off_answer_whose_draft_cannot_be_found_is_reported_as_not_known():
    server = FakeERPNext(["nameless"])
    server.docs["Purchase Order"] = []
    client = rest_client(server)
    original = server._post

    def forget(request, doctype):                                                                 # stored nothing, said nothing useful
        server.faults.clear()
        server.docs.setdefault(doctype, [])
        return server._reply(request, 200, {"data": {}})
    server._post = forget
    result = KINDS["order"][1](client, "prop-1")
    server._post = original
    assert not result.ok and "did not say" in result.error.lower() and server.stored("Purchase Order") == []


# ------------------------------------------------------------------ through the approval
@pytest.fixture
def world(make_app, clock):
    server = FakeERPNext()
    erp = HybridERP(server, today=clock().date(), scenarios=False)
    erp.add_item("BOLT-M8", "Bolt M8 x 40", lambda age: 40, 240, group="Fasteners", uom="pcs", lead_time_days=5)
    erp.add_vendors("BOLT-M8", three())
    erp.add_item("BOLT", "Bolt", lambda age: 10, 0, group="Fasteners", uom="pcs", warehouses={"Stores": {"on_hand": 100, "reserved": 60}, "Shop floor": {"on_hand": 40}})
    return make_app(erp=erp), server


def request_for_an_order(app):
    app.workorders.emit(REORDER_NEEDED, wo_payload())
    app.procurement.run()
    (pending,) = app.approvals.list(status="pending")
    return pending


@pytest.mark.parametrize("fault", ["lose", "timeout"])
def test_approving_an_order_whose_answer_was_lost_is_rolled_back_and_the_second_press_makes_no_second_draft(world, fault):
    app, server = world
    server.faults = [fault]
    pending = request_for_an_order(app)
    first = app.approvals.decide(pending.id, "approve", OWNER, "whatsapp")
    assert not first.ok and first.status == "error" and "could not reach ERPNext" in first.message and "try again" in first.message.lower()
    assert app.approvals.get(pending.id).status == "pending" and app.proposals.list()[0].status == "awaiting"        # the decision was not recorded...
    assert len(server.stored("Purchase Order")) == 1                                                                   # ...though the draft exists
    second = app.approvals.decide(pending.id, "approve", OWNER, "whatsapp")
    assert second.ok and second.status == "decided" and len(server.stored("Purchase Order")) == 1
    prop = app.proposals.list()[0]
    assert prop.status == "ordered" and prop.po_name == server.stored("Purchase Order")[0]["name"]


def test_approving_an_order_when_erpnext_has_a_5xx_changes_nothing_and_the_retry_works(world):
    app, server = world
    server.faults = ["503"]
    pending = request_for_an_order(app)
    first = app.approvals.decide(pending.id, "approve", OWNER, "whatsapp")
    assert not first.ok and "HTTP 503" in first.message and server.stored("Purchase Order") == [] and app.approvals.get(pending.id).status == "pending"
    assert app.approvals.decide(pending.id, "approve", OWNER, "whatsapp").ok and len(server.stored("Purchase Order")) == 1


def test_approving_an_order_whose_answer_is_cut_off_is_recorded_with_the_draft_that_exists(world):
    app, server = world
    server.faults = ["partial"]
    pending = request_for_an_order(app)
    result = app.approvals.decide(pending.id, "approve", OWNER, "whatsapp")
    assert result.ok and result.status == "decided"
    assert app.proposals.list()[0].po_name == server.stored("Purchase Order")[0]["name"] and len(server.stored("Purchase Order")) == 1


def test_a_requisition_whose_answer_was_lost_is_not_made_twice(world):
    app, server = world
    server.faults = ["timeout"]
    pending = request_for_an_order(app)
    first = app.approvals.decide(pending.id, "requisition", OWNER, "app")
    assert not first.ok and len(server.stored("Material Request")) == 1 and app.approvals.get(pending.id).status == "pending"
    assert app.approvals.decide(pending.id, "requisition", OWNER, "app").ok and len(server.stored("Material Request")) == 1 and server.stored("Purchase Order") == []


@pytest.mark.parametrize("fault", ["lose", "timeout", "partial"])
def test_a_stock_transfer_whose_answer_was_lost_or_cut_off_is_not_made_twice(world, fault):
    app, server = world
    server.faults = [fault]
    prepared = app.transfers.prepare("BOLT", 30, "Stores", "Shop floor", "anna")
    first = app.approvals.decide(prepared["approval_id"], "approve", OWNER, "app")
    if fault == "partial":
        assert first.ok                                                                       # the draft exists and was found by its reference
    else:
        assert not first.ok and app.approvals.get(prepared["approval_id"]).status == "pending"
        assert app.approvals.decide(prepared["approval_id"], "approve", OWNER, "app").ok
    assert len(server.stored("Stock Entry")) == 1 and app.transfers.get(prepared["transfer"])["status"] == "created"
