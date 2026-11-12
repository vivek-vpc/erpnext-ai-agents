"""The vendor comparison for one reorder that a person has just approved (Group 2, F1, of the production-readiness review).

Approving a reorder makes a work order; the comparison of the vendors for it used to wait for the next scheduled run, up to a day. The app now compares at once, for that one work
order, and the scheduled run does the rest. For that to be safe the run for one work order has to take that work order and no other, and a work order is compared once however many
runs look at it."""
import pytest

from conftest import OWNER
from erp_agents.core.erp.mock import MockERP
from ai_munimji_engine.core.workorders import REORDER_NEEDED
from test_procurement import three, wo_payload


@pytest.fixture
def world(make_app, clock):
    erp = MockERP(today=clock().date(), scenarios=False)
    for code in ("BOLT-M8", "NUT-M8"):
        erp.add_item(code, f"{code} item", lambda age: 40, 240, group="Fasteners", uom="pcs", lead_time_days=5)
        erp.add_vendors(code, three())
    return make_app(erp=erp)


def two_waiting(app):
    first = app.workorders.emit(REORDER_NEEDED, wo_payload("BOLT-M8"))
    second = app.workorders.emit(REORDER_NEEDED, wo_payload("NUT-M8"))
    return first["id"], second["id"]


def waiting_items(app):
    return sorted(w["payload"]["item_code"] for w in app.workorders.list(status="pending", type_=REORDER_NEEDED))


def test_a_run_for_one_work_order_compares_the_vendors_for_that_one_only(world):
    first, second = two_waiting(world)
    report = world.procurement.run(work_orders=[first])
    assert (report.considered, report.proposed) == (1, 1)
    assert [a["item_code"] for a in report.actions] == ["BOLT-M8"]
    assert world.workorders.get(first)["status"] == "claimed"
    assert world.workorders.get(second)["status"] == "pending", "the other reorder is left for the scheduled run"
    assert [r.kind for r in world.approvals.list("pending")] == ["procurement.po"]


def test_the_scheduled_run_after_it_makes_no_second_request_for_the_same_work_order(world):
    first, second = two_waiting(world)
    world.procurement.run(work_orders=[first])
    scheduled = world.procurement.run()
    assert (scheduled.considered, scheduled.proposed) == (1, 1), "only the other reorder is left for it"
    items = sorted(r.request.facts.get("item_code") for r in world.approvals.list("pending") if r.kind == "procurement.po")
    assert items == ["BOLT-M8", "NUT-M8"], "one purchase request for each item, none twice"
    assert len(world.proposals.list()) == 2


def test_a_work_order_that_is_not_waiting_is_not_taken_again(world):
    first, _ = two_waiting(world)
    world.procurement.run(work_orders=[first])
    again = world.procurement.run(work_orders=[first])
    assert again.considered == 0 and again.proposed == 0
    assert len(world.proposals.list()) == 1


def test_an_unknown_id_and_a_repeated_id_are_harmless(world):
    first, _ = two_waiting(world)
    report = world.procurement.run(work_orders=["no-such-order", first, first])
    assert (report.considered, report.proposed) == (1, 1)
    assert len(world.proposals.list()) == 1


def test_an_empty_list_takes_nothing(world):
    two_waiting(world)
    report = world.procurement.run(work_orders=[])
    assert report.considered == 0
    assert waiting_items(world) == ["BOLT-M8", "NUT-M8"]


def test_a_run_for_one_work_order_leaves_the_snoozed_requests_alone(world, clock):
    two_waiting(world)
    world.procurement.run()
    snoozed = world.approvals.list("pending")[0]
    world.approvals.decide(snoozed.id, "snooze", OWNER, "console")
    proposal = world.proposals.by_approval(snoozed.id)
    assert proposal.status == "snoozed"
    clock.advance(days=30)
    world.procurement.run(work_orders=[])
    assert world.proposals.get(proposal.id).status == "snoozed", "waking a snoozed request is the scheduled run's"
    world.procurement.run()
    assert world.proposals.get(proposal.id).status != "snoozed"


def test_without_work_orders_the_run_is_what_it_was(world):
    two_waiting(world)
    report = world.procurement.run()
    assert (report.considered, report.proposed) == (2, 2)
