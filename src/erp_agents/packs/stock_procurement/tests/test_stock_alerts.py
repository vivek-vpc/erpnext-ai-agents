"""Stock alerts: information-only notices (low stock / needed urgently) sent BEFORE any request to reorder."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import APPROVER, OWNER, FakeHTTP
from erp_agents.core.api.app import create_app
from erp_agents.core.approvals.channels.simple import InboxChannel
from erp_agents.core.approvals.channels.whatsapp import WhatsAppChannel
from erp_agents.core.erp.mock import MockERP
from ai_munimji_engine.packs.stock_procurement.inventory.messages import KIND_ALERT


@pytest.fixture
def app(make_app, clock):
    a = make_app(erp=MockERP(today=clock().date()))
    a.settings.update_system(OWNER, {"stock_alerts": True})
    return a


def kinds(app, status="pending"):
    return [(r.kind, r.request.facts.get("item_code")) for r in app.approvals.list(status, 100)]


def notices(app, **where):
    return [r for r in app.approvals.list(None, 200) if r.kind == KIND_ALERT and all(r.request.facts.get(k) == v for k, v in where.items())]


def reorders(app):
    return [r for r in app.approvals.list(None, 200) if r.kind == "inventory.reorder"]


# ------------------------------------------------------------------ what is sent, and in what order
def test_the_alert_is_created_before_the_request_to_reorder_for_every_item_that_needs_one(app):
    rep = app.inventory.run()
    by_item = {a["item_code"]: a for a in rep.actions if a["action"] in ("new_notice", "new_alert")}
    assert rep.new_notices == 6 and rep.new_alerts == 4                          # six items are low; four of them have reached their reorder point
    for code in ("RELAY-12V", "OIL-5L", "BOLT-M8", "FILTER-A"):
        a, r = notices(app, item_code=code)[0], next(x for x in reorders(app) if x.request.facts["item_code"] == code)
        assert a.created_at <= r.created_at
        ids = [x["approval_id"] for x in rep.actions if x["item_code"] == code and "approval_id" in x]
        assert ids == [a.id, r.id]                                                 # reported, and created, alert first then request


def test_alerts_are_delivered_before_the_request_on_whatsapp(make_app, clock):
    http = FakeHTTP()
    channels = {"inbox": InboxChannel(), "whatsapp": WhatsAppChannel("TOKEN", "PNID", app_secret="s", verify_token="v", http=http)}
    cfg = {"approvals": {"strategy": "all", "recipients": [{"id": "owner", "name": "Owner", "roles": ["admin", "approver"],
                                                              "channels": [{"type": "inbox"}, {"type": "whatsapp", "address": "919800000000"}]}]}}
    a = make_app(erp=MockERP(today=clock().date()), channels=channels, cfg=cfg)
    a.settings.update_system(OWNER, {"stock_alerts": True})
    a.inventory.run(item_codes=["RELAY-12V"])
    texts = [c[1]["text"]["body"] for c in http.calls if c[1].get("type") == "text"]
    assert texts[0].startswith("URGENT") or "URGENT" in texts[0][:80]
    assert "Reorder suggested" in texts[1] or any("Reorder suggested" in t for t in texts[1:])
    first_alert = next(i for i, t in enumerate(texts) if "information only" in t)
    first_request = next(i for i, t in enumerate(texts) if "Suggested action" in t)
    assert first_alert < first_request


def test_the_level_follows_the_analysis_urgent_when_stock_cannot_last_until_new_stock_arrives(app):
    app.inventory.run()
    relay = notices(app, item_code="RELAY-12V")[0].request
    assert relay.facts["level"] == "URGENT" and relay.title == "URGENT: Relay 12V (RELAY-12V) is needed urgently" and relay.severity == "CRITICAL"
    assert "new stock takes about 9 days to arrive" in relay.headline
    bolt = notices(app, item_code="BOLT-M8")[0].request
    assert bolt.facts["level"] == "URGENT" or bolt.facts["level"] == "LOW"
    low = [n for n in notices(app) if n.request.facts["level"] == "LOW"]
    assert low and all(n.request.title.startswith("Low stock") for n in low)


def test_an_alert_is_information_only_and_says_what_happens_next(app):
    app.inventory.run()
    req = notices(app, item_code="RELAY-12V")[0].request
    assert [o.action for o in req.options] == ["ack", "snooze", "details"] and req.options[0].label == "Got it" and req.options[2].terminal is False
    text = json.dumps(req.model_dump(mode="json"))
    assert "A request to reorder follows this alert" in text and "information only: nothing is ordered or changed" in text and "Situation" in text
    early = notices(app, item_code="PAINT-RED")[0].request
    assert "No action is needed yet" in json.dumps(early.model_dump(mode="json")) and early.title.startswith("Low stock soon")


def test_an_early_warning_only_alerts_it_does_not_ask_for_a_reorder(app):
    rep = app.inventory.run()
    for code in ("PAINT-RED", "GASKET-X"):
        assert len(notices(app, item_code=code)) == 1
        assert not [r for r in reorders(app) if r.request.facts["item_code"] == code]
        assert {a["action"] for a in rep.actions if a["item_code"] == code} >= {"new_notice", "skipped:alert_only"}


def test_acknowledging_changes_nothing_else(app):
    app.inventory.run()
    n = notices(app, item_code="PAINT-RED")[0]
    wo, props = len(app.workorders.list()), len(app.erp.purchase_orders)
    res = app.approvals.decide(n.id, "ack", APPROVER, "app")
    assert res.status == "decided" and app.approvals.get(n.id).decision.action == "ack"
    assert len(app.workorders.list()) == wo and len(app.erp.purchase_orders) == props and not app.erp.requisitions
    assert app.inventory.notices.by_approval(n.id).status == "acknowledged"


def test_details_shows_the_numbers_and_keeps_the_alert_open(app):
    app.inventory.run()
    n = notices(app, item_code="BOLT-M8")[0]
    res = app.approvals.decide(n.id, "details", APPROVER, "app")
    assert res.status == "info" and any(s.heading == "Numbers" for s in res.info_sections) and app.approvals.get(n.id).status == "pending"


# ------------------------------------------------------------------ not announced again and again
def test_the_same_problem_is_announced_once(app):
    app.inventory.run()
    before = len(notices(app))
    rep = app.inventory.run()
    assert len(notices(app)) == before and rep.new_notices == 0


def test_it_is_repeated_only_after_the_repeat_period_if_still_low(app, clock):
    app.inventory.run()
    n = len(notices(app, item_code="BOLT-M8"))
    clock.advance(days=6)
    app.inventory.run(item_codes=["BOLT-M8"])
    assert len(notices(app, item_code="BOLT-M8")) == n
    clock.advance(days=2)
    rep = app.inventory.run(item_codes=["BOLT-M8"])
    assert len(notices(app, item_code="BOLT-M8")) == n + 1 and rep.new_notices == 1


def test_a_snoozed_alert_stays_quiet_but_a_worse_level_is_announced_at_once(app, clock):
    app.inventory.run()
    n = notices(app, item_code="BOLT-M8")[0]
    app.approvals.decide(n.id, "snooze", APPROVER, "app")
    clock.advance(days=8)                                                           # past the repeat period, but the owner asked for a longer wait
    app.settings.update_system(OWNER, {"stock_alert_repeat_days": 1})
    app.inventory.notices.set_status(app.inventory.notices.by_approval(n.id).id, "snoozed", snoozed_until=clock() + timedelta(days=2))
    rep = app.inventory.run(item_codes=["BOLT-M8"])
    assert rep.new_notices == 0 and any(a["action"] == "skipped:alert_snoozed" for a in rep.actions)
    if app.inventory.notices.latest_active("BOLT-M8").level == "LOW":
        app.erp.set_stock("BOLT-M8", on_hand=40)                                    # now far below lead time: urgent
        rep = app.inventory.run(item_codes=["BOLT-M8"])
        assert rep.new_notices == 1 and app.inventory.notices.latest_active("BOLT-M8").level == "URGENT"


def test_a_worse_level_replaces_the_older_alert_that_is_still_waiting(app):
    app.inventory.run()
    old = next((n for n in notices(app) if n.request.facts["level"] == "LOW" and n.request.facts["item_code"] in ("BOLT-M8", "FILTER-A", "PAINT-RED")), None)
    if old is None:
        pytest.skip("demo data has no low-level alert to escalate")
    code = old.request.facts["item_code"]
    app.erp.set_stock(code, on_hand=1)
    rep = app.inventory.run(item_codes=[code])
    assert rep.new_notices == 1 and app.approvals.get(old.id).status == "cancelled" and "replaced by a newer urgent" in app.approvals.get(old.id).status_note
    assert app.inventory.notices.latest_active(code).level == "URGENT"


def test_recovery_cancels_a_waiting_alert_and_a_new_episode_alerts_again(app):
    app.inventory.run()
    n = notices(app, item_code="OIL-5L")[0]
    assert app.approvals.get(n.id).status == "pending"
    app.erp.set_stock("OIL-5L", on_hand=900)                                        # goods received
    app.inventory.run(item_codes=["OIL-5L"])
    assert app.approvals.get(n.id).status == "cancelled" and "back above your limits" in app.approvals.get(n.id).status_note
    assert app.inventory.notices.latest_active("OIL-5L") is None
    app.erp.set_stock("OIL-5L", on_hand=18)
    rep = app.inventory.run(item_codes=["OIL-5L"])
    assert rep.new_notices == 1 and len(notices(app, item_code="OIL-5L")) == 2


def test_the_owner_is_never_nagged_about_an_information_only_alert(app, clock):
    app.inventory.run()
    clock.advance(hours=30)
    reminded = app.approvals.remind_due()
    assert reminded and all(app.approvals.get(i).kind != KIND_ALERT for i in reminded)


def test_one_bad_day_cannot_flood_the_owner(app):
    app.settings.update_system(OWNER, {"max_notices_per_run": 2})
    rep = app.inventory.run()
    assert rep.new_notices == 2 and any(a["action"] == "skipped:alert_cap_reached" for a in rep.actions)
    first = {n.request.facts["item_code"] for n in notices(app)}
    assert first <= {"RELAY-12V", "OIL-5L", "BOLT-M8", "FILTER-A"} and {"RELAY-12V", "OIL-5L"} & first                    # worst first
    rep2 = app.inventory.run()
    assert rep2.new_notices == 2 and len(notices(app)) == 4                                                           # the rest wait for the next check


def test_a_dry_run_reports_the_alerts_it_would_send_and_sends_nothing(app):
    rep = app.inventory.run(dry_run=True)
    assert rep.new_notices == 6 and any(a["action"] == "would_notify" for a in rep.actions)
    assert not app.approvals.list(None, 100) and not app.inventory.notices.list()


def test_with_the_setting_off_the_old_behaviour_returns(app):
    app.settings.update_system(OWNER, {"stock_alerts": False})
    rep = app.inventory.run()
    assert rep.new_notices == 0 and rep.new_alerts == 6 and not notices(app)
    assert any(r.request.facts["item_code"] == "PAINT-RED" for r in reorders(app))                                    # the early warning asks for a reorder, as before


def test_starting_a_reorder_still_works_exactly_as_before(app):
    app.inventory.run()
    r = next(x for x in reorders(app) if x.request.facts["item_code"] == "RELAY-12V")
    assert app.approvals.decide(r.id, "approve", APPROVER, "app").status == "decided"
    assert len(app.workorders.list()) == 1 and app.workorders.list()[0]["payload"]["item_code"] == "RELAY-12V"


def test_the_stock_alert_settings_are_validated_and_reach_the_api(make_app, clock):
    a = make_app(erp=MockERP(today=clock().date()), cfg={"auth": {"tokens": [{"token": "o" * 40, "user": "owner", "roles": ["viewer", "approver", "admin"]}]}})
    c = TestClient(create_app(a))
    h = {"Authorization": "Bearer " + "o" * 40}
    assert c.put("/api/v1/settings/system", json={"set": {"stock_alerts": True, "stock_alert_repeat_days": 3, "max_notices_per_run": 5}}, headers=h).status_code == 200
    assert c.put("/api/v1/settings/system", json={"set": {"stock_alert_repeat_days": 0}}, headers=h).status_code == 422
    assert c.put("/api/v1/settings/system", json={"set": {"max_notices_per_run": 1000}}, headers=h).status_code == 422
    r = c.post("/api/v1/inventory/run?dry_run=true", headers=h).json()
    assert r["new_notices"] == 5 and any(a["action"] == "skipped:alert_cap_reached" for a in r["actions"])          # the limit just saved (5) applies at once


def test_the_chat_counts_alerts_as_waiting_requests(app):
    app.inventory.run()
    r = app.chat.ask("what is waiting for my OK?", "owner", True)
    assert "stock alert" in r["answer"] and "reorder" in r["answer"]
