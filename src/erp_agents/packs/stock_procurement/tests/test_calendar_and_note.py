"""Demand calendar (festivals, promotions) and the vendor note on purchase orders."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import APPROVER, OWNER
from erp_agents.core.api.app import create_app
from erp_agents.core.assistant.facts import build_facts
from erp_agents.packs.stock_procurement.settings import CalendarEvent, CalendarSettings, Policy, ProcurementSettings
from erp_agents.core.settings_store import SettingsError
from erp_agents.core.workorders import REORDER_NEEDED
from ai_munimji_engine.packs.stock_procurement.erp import PurchaseOrderDraft, VendorOffer
from erp_agents.core.erp.erpnext import ERPNextClient
from erp_agents.core.erp.mock import MockERP
from erp_agents.packs.stock_procurement.inventory.analysis import STATUS_BREACH, STATUS_EARLY, STATUS_OK, analyze_item
from erp_agents.packs.stock_procurement.inventory.events import Window, active_windows, days_until_demand, describe, factor_on, total_demand
from erp_agents.packs.stock_procurement.procurement.messages import vendor_note_for
from erp_agents.packs.stock_procurement.procurement.scoring import Need, rank_vendors

TODAY = date(2026, 10, 16)


def win(start_in, end_in, pct, name="Sale"):
    return Window(name, "promotion", TODAY + timedelta(days=start_in), TODAY + timedelta(days=end_in), 1 + pct / 100, pct)


# ------------------------------------------------------------------ the maths
def test_no_events_means_exactly_the_old_numbers():
    assert days_until_demand(100, 5, [], TODAY) == 20 and total_demand(5, [], TODAY, 12.5) == 62.5


def test_demand_is_used_up_day_by_day_through_an_event():
    w = [win(0, 9, 100)]                                  # double demand for 10 days from today
    assert days_until_demand(100, 5, w, TODAY) == pytest.approx(10)            # 10 days x 10/day; plain division would say 20
    later = [win(5, 30, 100)]                             # 5 normal days (25), then 75 left at 10/day = 7.5 days
    assert days_until_demand(100, 5, later, TODAY) == pytest.approx(12.5)
    assert total_demand(5, later, TODAY, 12.5) == pytest.approx(100)           # the two functions agree
    assert days_until_demand(100, 5, [win(60, 70, 100)], TODAY) == pytest.approx(20)       # event after the stock is gone: no effect
    assert days_until_demand(-5, 5, w, TODAY) == -1 and days_until_demand(10, 0, w, TODAY) == float("inf")


def test_overlapping_events_use_the_strongest_rise_never_a_product():
    assert factor_on(TODAY, [win(0, 5, 50, "a"), win(0, 5, 100, "b")]) == 2.0
    assert factor_on(TODAY, [win(0, 5, -20, "a"), win(0, 5, -50, "b")]) == 0.5       # no rise anywhere: the deepest fall
    assert factor_on(TODAY, [win(0, 5, 50, "a"), win(0, 5, -50, "b")]) == 1.5
    assert factor_on(TODAY + timedelta(days=9), [win(0, 5, 50)]) == 1.0


def test_events_apply_to_all_items_a_group_or_specific_items():
    mk = lambda **kw: SimpleNamespace(name="E", kind="festival", start=TODAY, end=TODAY + timedelta(days=3), uplift_pct=30, item_groups=[], items=[], **kw)
    everything, grp, item = mk(), SimpleNamespace(**{**vars(mk()), "item_groups": ["Fasteners"]}), SimpleNamespace(**{**vars(mk()), "items": ["BOLT"]})
    assert len(active_windows([everything], "X", None, TODAY)) == 1
    assert [bool(active_windows([grp], c, g, TODAY)) for c, g in (("A", "Fasteners"), ("A", "Paints"), ("A", None))] == [True, False, False]
    assert [bool(active_windows([item], c, "G", TODAY)) for c in ("BOLT", "NUT")] == [True, False]
    past = SimpleNamespace(**{**vars(mk()), "start": TODAY - timedelta(days=9), "end": TODAY - timedelta(days=1)})
    far = SimpleNamespace(**{**vars(mk()), "start": TODAY + timedelta(days=900), "end": TODAY + timedelta(days=905)})
    assert active_windows([past, far], "X", None, TODAY) == []
    assert describe([win(3, 5, 40, "Diwali sale")]).startswith("Diwali sale (+40%, 19 Oct to 21 Oct)")


# ------------------------------------------------------------------ effect on the agent's reasoning
def analyse(on_hand, rate, windows=(), policy=None, **item_kw):
    erp = MockERP(today=TODAY, scenarios=False)
    it = erp.add_item("X", "Item X", lambda age: rate, on_hand, lead_time_days=7, **item_kw)
    return analyze_item(it.info, it.stock, it.usage, policy or Policy(), TODAY, windows)


def test_an_event_shortens_cover_and_says_so_with_both_numbers():
    p = Policy(use_lead_time_rule=False)
    plain = analyse(200, 10, policy=p)
    during = analyse(200, 10, [win(0, 30, 100, "Diwali sale")], p)
    assert plain.cover_days_on_hand == pytest.approx(20) and during.cover_days_on_hand == pytest.approx(10)
    assert during.stockout_date == (TODAY + timedelta(days=10)).isoformat()
    assert any("Diwali sale (+100%" in d and "cover is 10 days instead of 20 days" in d for d in during.drivers)
    assert [w["name"] for w in during.to_dict()["windows"]] == ["Diwali sale"]


def test_an_event_can_bring_an_early_warning_forward():
    p = Policy(use_lead_time_rule=False, lookahead_days=10)
    assert analyse(200, 10, policy=p).status == STATUS_OK                                 # threshold 13 days away: outside the 10-day look-ahead
    a = analyse(200, 10, [win(0, 30, 100)], p)
    assert a.status == STATUS_EARLY and a.days_to_trigger == pytest.approx(6.5)
    assert any("threshold is reached in about 6.5 days instead of 13 days" in d for d in a.drivers)


def test_an_event_after_the_stock_runs_out_changes_nothing():
    p = Policy(use_lead_time_rule=False)
    a, b = analyse(200, 10, policy=p), analyse(200, 10, [win(60, 70, 100)], p)
    assert (a.cover_days_on_hand, a.status, a.drivers) == (b.cover_days_on_hand, b.status, b.drivers)


def test_suggested_quantity_covers_the_expected_demand_including_the_event():
    low = Policy(min_days_cover=None, use_lead_time_rule=False, min_qty=100)                   # on hand 50 <= 100: breach
    plain = analyse(50, 10, policy=low)
    boosted = analyse(50, 10, [win(0, 60, 50, "Festival")], low)
    assert plain.status == boosted.status == STATUS_BREACH
    assert plain.suggested_qty == 10 * (7 + 3 + 30) - 50 == 350
    assert boosted.suggested_qty == 15 * 40 - 50 == 550
    assert "expected demand of 600 pcs over the next 40 days" in boosted.suggested_qty_basis and "including your calendar: Festival (+50%" in boosted.suggested_qty_basis
    assert "pcs/day x (" in plain.suggested_qty_basis                                         # unchanged wording without events


# ------------------------------------------------------------------ settings
def ev(**kw):
    return {"name": "Diwali sale", "kind": "festival", "start": "2026-10-16", "end": "2026-11-10", "uplift_pct": 50, **kw}


def test_calendar_event_validation():
    assert CalendarEvent(**ev()).uplift_pct == 50
    for bad, msg in [({"end": "2026-10-01"}, "end date is before"), ({"uplift_pct": -100}, "greater than -100"), ({"uplift_pct": 5000}, "less than or equal to 1000"),
                     ({"name": ""}, "at least 1"), ({"kind": "party"}, "Input should be"), ({"colour": "red"}, "Extra inputs")]:
        with pytest.raises(ValueError, match=msg):
            CalendarEvent(**ev(**bad))
    assert CalendarSettings().events == [] and CalendarSettings().enabled is True


@pytest.fixture
def world(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()))
    return app


def test_calendar_changes_the_agents_numbers_only_for_matching_items(world):
    app = world
    before_bolt = app.inventory.analyze_one("BOLT-M8")[0].cover_days_on_hand
    before_paint = app.inventory.analyze_one("PAINT-RED")[0].cover_days_on_hand
    app.settings.update_section(OWNER, "calendar", {"events": [ev(item_groups=["Fasteners"], uplift_pct=100)]})
    bolt, _ = app.inventory.analyze_one("BOLT-M8")
    assert bolt.cover_days_on_hand < before_bolt and [w.name for w in bolt.windows] == ["Diwali sale"]
    assert app.inventory.analyze_one("PAINT-RED")[0].cover_days_on_hand == before_paint            # Paints: not in the event
    app.settings.update_section(OWNER, "calendar", {"enabled": False})
    assert app.inventory.analyze_one("BOLT-M8")[0].cover_days_on_hand == before_bolt                # switched off: ignored
    rows = {r["item_code"]: r for r in app.inventory.overview()}
    assert rows["BOLT-M8"]["calendar"] == []


def test_the_message_and_the_explanations_carry_the_calendar(world):
    app = world
    app.settings.update_section(OWNER, "calendar", {"events": [ev(uplift_pct=100)]})
    app.inventory.run()
    rec = next(r for r in app.approvals.list(status="pending") if r.request.facts["item_code"] == "BOLT-M8")
    why = next(s for s in rec.request.sections if s.heading == "Why you are seeing this")
    assert any("Your calendar changes demand (Diwali sale (+100%" in line for line in why.lines)
    details = " ".join(l for s in rec.request.details for l in s.lines)
    assert "Calendar events applied to this item: Diwali sale (+100%, 16 Oct to 10 Nov)" in details
    facts = " | ".join(f.text for f in app.assistant.facts_for(rec.id))                            # so "Ask why" can explain it too
    assert "Your calendar changes demand" in facts and "Calendar events applied" in facts


def test_calendar_through_the_api(make_app, clock):
    cfg = {"auth": {"tokens": [{"token": "owner", "user": "owner", "roles": ["admin", "approver"]}, {"token": "view", "user": "acct", "roles": ["viewer"]}]}}
    app = make_app(erp=MockERP(today=clock().date()), cfg=cfg)
    c = TestClient(create_app(app))
    H = lambda t: {"Authorization": f"Bearer {t}"}
    body = {"set": {"events": [ev(items=["BOLT-M8"], note="Annual sale")]}}
    assert c.put("/api/v1/settings/module/calendar", json=body, headers=H("view")).status_code == 403
    r = c.put("/api/v1/settings/module/calendar", json=body, headers=H("owner")).json()
    assert r["effective"]["events"][0]["start"] == "2026-10-16" and r["effective"]["events"][0]["items"] == ["BOLT-M8"]
    assert c.get("/api/v1/settings/module/calendar", headers=H("view")).json()["effective"]["events"][0]["note"] == "Annual sale"
    bad = c.put("/api/v1/settings/module/calendar", json={"set": {"events": [ev(end="2026-01-01")]}}, headers=H("owner"))
    assert bad.status_code == 422 and "end date is before" in bad.json()["detail"]
    assert "events" in c.get("/api/v1/settings/schema", headers=H("view")).json()["calendar"]
    assert any(a["field"] == "events" for a in app.settings.audit(scope_type="calendar"))


# ------------------------------------------------------------------ vendor note
def wo(code="BOLT-M8", cover=20.0, qty=500.0):
    return {"item_code": code, "item_name": "Bolt M8 x 40", "uom": "pcs", "suggested_qty": qty, "suggested_qty_basis": "basis", "cover_days": cover,
            "lead_time_days": 5.0, "daily_rate": 40.0, "on_hand": 240.0, "severity": "HIGH", "reasons": ["Low."]}


def offers(**over):
    base = dict(supplier="A", name="Alpha", unit_price=10.0, avg_price=10.0, lead_time_days=3, on_time_rate=0.96, orders=10,
                last_order=TODAY - timedelta(days=9), payment_days=30)
    return [VendorOffer(**{**base, **over})]


def best_for(vendors, cover, qty=500.0):
    need = Need(qty, cover, 7, "pcs")
    return rank_vendors(vendors, need, ProcurementSettings()).best, need


def test_note_is_the_owners_template_filled_from_the_proposal():
    best, need = best_for(offers(), 20)
    note = vendor_note_for(wo(), best, need, ProcurementSettings(), TODAY, "REF123")
    assert note == "Please confirm delivery by 19 Oct 2026 for 500 pcs of Bolt M8 x 40. Our reference: REF123."
    custom = ProcurementSettings(vendor_note_template="Deliver {qty} {uom} ({item_code}) to {vendor} price {unit_price} total {total} by {required_by}.")
    assert vendor_note_for(wo(), best, need, custom, TODAY, "R") == "Deliver 500 pcs (BOLT-M8) to Alpha price 10.00 total 5,000.00 by 19 Oct 2026."
    assert vendor_note_for(wo(), best, need, ProcurementSettings(include_vendor_note=False), TODAY, "R") == ""


def test_urgent_and_price_sentences_appear_only_when_a_recorded_fact_calls_for_them():
    best, need = best_for(offers(lead_time_days=9), cover=4)                    # delivery 9 days, stock-out in 4
    assert best.arrives_after_stockout
    note = vendor_note_for(wo(cover=4), best, need, ProcurementSettings(), TODAY, "R")
    assert note.endswith("This order is urgent: please expedite if you can and confirm the earliest delivery date.")
    assert "urgent" not in vendor_note_for(wo(cover=4), best, need, ProcurementSettings(vendor_note_urgent=""), TODAY, "R")
    fine, need2 = best_for(offers(), 20)
    assert "urgent" not in vendor_note_for(wo(), fine, need2, ProcurementSettings(), TODAY, "R")
    jumped, need3 = best_for(offers(unit_price=13.0), 20)
    assert vendor_note_for(wo(), jumped, need3, ProcurementSettings(), TODAY, "R").endswith("Please confirm the unit price of 13.00: it is above your usual price.")


def test_template_can_only_use_the_known_placeholders():
    for bad in ("Dear {contact}, please deliver", "{0} please", "unfinished {qty"):
        with pytest.raises(ValueError, match="vendor_note_template"):
            ProcurementSettings(vendor_note_template=bad)
    assert ProcurementSettings(vendor_note_template="Plain text with no placeholders.").include_vendor_note is True


@pytest.fixture
def po_world(make_app, clock):
    erp = MockERP(today=clock().date(), scenarios=False)
    erp.add_item("BOLT-M8", "Bolt M8 x 40", lambda age: 40, 240, uom="pcs", lead_time_days=5)
    erp.add_vendors("BOLT-M8", offers() + offers(supplier="C", name="Gamma", unit_price=11.0, avg_price=11.0, lead_time_days=2, payment_days=0))
    app = make_app(erp=erp)
    app.workorders.emit(REORDER_NEEDED, wo())
    app.procurement.run()
    return app, erp, app.approvals.list(status="pending")[0]


def test_owner_sees_the_note_before_approving_and_the_po_carries_exactly_that_text(po_world):
    app, erp, rec = po_world
    section = next(s for s in rec.request.sections if s.heading == "Note to vendor")
    prop = app.proposals.list()[0]
    assert section.lines[0] == f"Please confirm delivery by 19 Oct 2026 for 500 pcs of Bolt M8 x 40. Our reference: {prop.id}."
    assert "Configuration → Procurement" in section.lines[1]
    assert prop.ranking["vendor_note"] == section.lines[0]
    assert app.approvals.decide(rec.id, "approve", APPROVER, "whatsapp").ok
    assert erp.purchase_orders[0]["vendor_note"] == section.lines[0]
    facts = " | ".join(f.text for f in build_facts(app.approvals.get(rec.id)))
    assert "Note to vendor: Please confirm delivery by 19 Oct 2026" in facts


def test_no_note_section_when_switched_off_or_when_no_po_is_created(make_app, clock):
    for settings in ({"include_vendor_note": False}, {"po_mode": "off"}):
        erp = MockERP(today=clock().date(), scenarios=False)
        erp.add_item("BOLT-M8", "Bolt M8 x 40", lambda age: 40, 240, uom="pcs", lead_time_days=5)
        erp.add_vendors("BOLT-M8", offers())
        app = make_app(erp=erp)
        app.settings.update_procurement(OWNER, settings)
        app.workorders.emit(REORDER_NEEDED, wo())
        app.procurement.run()
        rec = app.approvals.list(status="pending")[0]
        assert "Note to vendor" not in [s.heading for s in rec.request.sections]
        app.approvals.decide(rec.id, "approve", OWNER, "app")
        assert all(not po["vendor_note"] for po in erp.purchase_orders)


def test_erpnext_puts_the_note_in_terms_and_conditions(erpnext_client_factory=None):
    class Post:
        headers = {}

        def __init__(self):
            self.posts = []

        def get(self, url, params=None, timeout=None):
            return SimpleNamespace(status_code=200, text="", json=lambda: {"data": [] if params else {}})

        def post(self, url, json=None, timeout=None):
            self.posts.append(json)
            return SimpleNamespace(status_code=200, text="", json=lambda: {"data": {"name": "PO-1"}})

    s = Post()
    c = ERPNextClient("http://erp.local", "K", "S", session=s)
    c.create_purchase_order(PurchaseOrderDraft("S-A", "BOLT", 5, 1, "Nos", date(2026, 10, 25), vendor_note="Please confirm delivery."))
    c.create_purchase_order(PurchaseOrderDraft("S-A", "BOLT", 5, 1, "Nos", date(2026, 10, 25)))
    assert s.posts[0]["terms"] == "Please confirm delivery." and "terms" not in s.posts[1]


def test_a_busy_season_can_cover_several_item_groups_at_once(make_app, clock):
    """Settings -> Busy seasons: the console offers every item group as a tick box (25 Oct 2026; before, its single text box kept only one group)."""
    from pathlib import Path
    from fastapi.testclient import TestClient
    from erp_agents.core.api.app import create_app
    from erp_agents.core.erp.mock import MockERP
    app = make_app(erp=MockERP(today=clock().date()), cfg={"auth": {"tokens": [{"token": "o" * 40, "user": "owner", "roles": ["viewer", "approver", "admin"]}]}})
    c = TestClient(create_app(app))
    day = clock().date()
    season = {"name": "Diwali sale", "kind": "festival", "start": day.isoformat(), "end": (day + timedelta(days=10)).isoformat(), "uplift_pct": 40,
              "item_groups": ["Fasteners", "Electrical"], "items": ["OIL-5L"]}
    res = c.put("/api/v1/settings/module/calendar", json={"set": {"enabled": True, "events": [season]}}, headers={"Authorization": "Bearer " + "o" * 40})
    assert res.status_code == 200 and res.json()["effective"]["events"][0]["item_groups"] == ["Fasteners", "Electrical"]
    covered = {code for code in ("BOLT-M8", "GASKET-X", "CABLE-3C", "RELAY-12V", "OIL-5L", "STEEL-2MM", "PAINT-RED") if app.inventory.analyze_one(code)[0].windows}
    assert covered == {"BOLT-M8", "GASKET-X", "CABLE-3C", "RELAY-12V", "OIL-5L"}                 # both groups and the single item; Raw Material and Paints untouched
    page = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert 'data-grp' in page and 'id="ce_gl"' not in page                                         # tick boxes, not the one-value suggestion box
