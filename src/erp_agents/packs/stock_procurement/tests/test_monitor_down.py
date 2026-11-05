"""When the stock check itself cannot run (ERP unreachable), the failure is recorded and, if it keeps happening, the owner is told."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import OWNER
from erp_agents.core.api.app import create_app
from erp_agents.core.erp.mock import MockERP
from ai_munimji_engine.packs.stock_procurement.inventory.messages import KIND_MONITOR_DOWN


class FlakyERP(MockERP):
    down = False

    def list_items(self):
        if self.down:
            raise ConnectionError("HTTPConnectionPool(host='erp.local', port=8080): Max retries exceeded")
        return super().list_items()

    def get_stock(self, *a, **k):
        if self.down_on_stock:
            raise TimeoutError("read timed out")
        return super().get_stock(*a, **k)

    down_on_stock = False


@pytest.fixture
def world(make_app, clock):
    erp = FlakyERP(today=clock().date(), scenarios=False)
    erp.add_item("BOLT", "Bolt", lambda age: 40, 240, group="Fasteners", uom="pcs", lead_time_days=5)
    app = make_app(erp=erp)
    return app, erp, clock


def notices(app):
    return [r for r in app.approvals.list(None, 50) if r.kind == KIND_MONITOR_DOWN]


def test_a_run_that_cannot_read_the_erp_is_recorded_and_changes_nothing_else(world):
    app, erp, clock = world
    erp.down = True
    report = app.inventory.run()
    assert report.failed and report.checked == 0 and "ConnectionError" in report.errors[0] and "Traceback" not in report.errors[0]
    row = app.stock_stores.runs.list(10)[0]
    assert json.loads(row["summary_json"])["failed"] is True and row["dry_run"] == 0
    assert not app.alerts.list(None, None, 10) and not app.approvals.list(None, 10)
    erp.down = False
    assert not app.inventory.run().failed                                       # the next run recovers


def test_a_failure_while_reading_stock_is_recorded_too(world):
    app, erp, clock = world
    erp.down_on_stock = True
    report = app.inventory.run()
    assert report.failed and "TimeoutError" in report.errors[0]
    assert len(app.stock_stores.runs.list(10)) == 1


def test_the_api_returns_the_failed_report_instead_of_a_server_error(make_app, clock):
    erp = FlakyERP(today=clock().date(), scenarios=False)
    erp.add_item("BOLT", "Bolt", lambda age: 40, 240)
    app = make_app(erp=erp, cfg={"auth": {"tokens": [{"token": "o" * 40, "user": "owner", "roles": ["admin", "approver"]}]}})
    erp.down = True
    c = TestClient(create_app(app))
    r = c.post("/api/v1/inventory/run?dry_run=false", headers={"Authorization": "Bearer " + "o" * 40})
    assert r.status_code == 200 and r.json()["failed"] is True
    runs = c.get("/api/v1/inventory/runs", headers={"Authorization": "Bearer " + "o" * 40}).json()
    assert runs[0]["summary"]["failed"] is True


def test_the_owner_is_told_after_n_failures_in_a_row_and_only_once(world):
    app, erp, clock = world
    assert app.settings.system().monitor_failures_before_alert == 3
    erp.down = True
    for i in range(2):
        app.inventory.run()
        clock.advance(hours=1)
    assert notices(app) == []                                                    # two failures: not yet
    app.inventory.run()
    n = notices(app)
    assert len(n) == 1 and n[0].status == "pending"
    assert "3 runs in a row" in n[0].request.headline and "No stock alerts are being raised" in n[0].request.headline
    assert [o.action for o in n[0].request.options] == ["ack", "details"]
    for _ in range(3):
        clock.advance(hours=1)
        app.inventory.run()
    assert len(notices(app)) == 1                                                # no repeat while it is open


def test_a_success_in_between_resets_the_count(world):
    app, erp, clock = world
    erp.down = True
    app.inventory.run(); clock.advance(hours=1); app.inventory.run(); clock.advance(hours=1)
    erp.down = False
    app.inventory.run(); clock.advance(hours=1)
    erp.down = True
    app.inventory.run(); clock.advance(hours=1); app.inventory.run()
    assert notices(app) == []


def test_recovery_cancels_the_open_notice_and_a_later_outage_can_notify_again(world):
    app, erp, clock = world
    erp.down = True
    for _ in range(3):
        app.inventory.run(); clock.advance(hours=1)
    first = notices(app)[0]
    erp.down = False
    app.inventory.run()
    assert app.approvals.get(first.id).status == "cancelled" and "working again" in app.approvals.get(first.id).status_note
    clock.advance(days=2)
    erp.down = True
    for _ in range(3):
        app.inventory.run(); clock.advance(hours=1)
    assert len(notices(app)) == 2 and notices(app)[0].status == "pending"


def test_a_second_notice_waits_a_day_even_if_the_first_was_answered(world):
    app, erp, clock = world
    erp.down = True
    for _ in range(3):
        app.inventory.run(); clock.advance(minutes=30)
    app.approvals.decide(notices(app)[0].id, "ack", OWNER, "app")
    for _ in range(3):
        app.inventory.run(); clock.advance(minutes=30)
    assert len(notices(app)) == 1
    clock.advance(hours=24)
    app.inventory.run()
    assert len(notices(app)) == 2


def test_dry_runs_neither_count_nor_notify(world):
    app, erp, clock = world
    erp.down = True
    for _ in range(5):
        assert app.inventory.run(dry_run=True).failed
    assert notices(app) == []
    app.inventory.run(); app.inventory.run()
    assert notices(app) == []                                                    # only two real failures so far


def test_the_threshold_is_a_setting(world):
    app, erp, clock = world
    app.settings.update_system(OWNER, {"monitor_failures_before_alert": 1})
    erp.down = True
    app.inventory.run()
    assert len(notices(app)) == 1


def test_the_notice_reaches_the_owner_and_its_details_list_the_errors(world):
    app, erp, clock = world
    erp.down = True
    for _ in range(3):
        app.inventory.run(); clock.advance(hours=1)
    rec = notices(app)[0]
    res = app.approvals.decide(rec.id, "details", OWNER, "app")
    assert res.status == "info" and len(res.info_sections[0].lines) == 3 and all("ConnectionError" in l for l in res.info_sections[0].lines)
    assert app.approvals.get(rec.id).status == "pending"
    assert app.approvals.decide(rec.id, "ack", OWNER, "app").status == "decided"
