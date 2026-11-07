"""A database and a request made before the tax figures were kept with a request.

The figures are kept inside the proposal's `ranking_json` text, a column that has always been there, so nothing in the schema changed and no numbered migration was needed:
`tests/test_schema_unchanged.py` pins the database a new installation gets to the one the code made before the work on the Frappe app began (commit 78372cb), and passes. What is
shown here is the other half of that promise, for a database somebody already has: it opens as it was, with its rows, and a request that carries no figures behaves as it did before -
it asks the ERP for the tax setup of each vendor when the quantity is changed - and from then on carries them.
"""
import json
import sqlite3

import pytest

from conftest import OWNER, table_rows, tamper
from erp_agents.packs.stock_procurement.tests.test_tax_figures import COST_ONLY, LOCAL, ABROAD, propose, request_text, the_vendor, world  # noqa: F401 - the fixture


def without_kept_figures(row):
    """The `ranking_json` the code wrote before the figures were kept: the same text without the `tax_setups` key."""
    ranking = json.loads(row["ranking_json"])
    ranking.pop("tax_setups", None)
    return {"ranking_json": json.dumps(ranking, sort_keys=True)}


@pytest.mark.sqlite_only
def test_a_database_made_before_the_figures_were_kept_opens_as_it_was_and_keeps_its_rows(make_app, clock, tmp_path):
    from erp_agents.core.erp.mock import MockERP
    from erp_agents.packs.stock_procurement.tests.test_tax_figures import offer

    path = str(tmp_path / "before.db")
    erp = MockERP(today=clock().date(), scenarios=False)
    erp.add_item("BOLT", "Bolt", lambda age: 40, 240, group="Fasteners", uom="pcs", lead_time_days=5)
    erp.add_vendors("BOLT", [offer("LOCAL", "Local Co", 10.0), offer("ABROAD", "Overseas Ltd", 9.0, "Vietnam")])
    erp.purchase_taxes = {"LOCAL": LOCAL, "ABROAD": ABROAD}
    app = make_app(erp=erp, db_path=path)
    app.settings.update_procurement(OWNER, COST_ONLY)
    propose(app)
    tamper(app, "proposals", {"id": app.proposals.list()[0].id}, without_kept_figures)       # what the code before wrote
    before = {t: table_rows(app, t) for t in ("proposals", "approvals", "work_orders", "agent_runs")}
    versions = sqlite3.connect(path).execute("SELECT version FROM schema_info").fetchall()
    app.stores.close()

    again = make_app(erp=erp, db_path=path)                                  # opened by the code of today
    assert not [p for p in tmp_path.iterdir() if "before-schema" in p.name], "no migration ran, so no copy of the file was made first"
    assert sqlite3.connect(path).execute("SELECT version FROM schema_info").fetchall() == versions
    assert {t: table_rows(again, t) for t in before} == before, "every row is as it was"
    proposal = again.proposals.list()[0]
    assert "tax_setups" not in proposal.ranking and {v["supplier"] for v in proposal.ranking["vendors"]} == {"LOCAL", "ABROAD"}
    assert again.approvals.list(status="pending")[0].request.facts["supplier"] == "LOCAL"


def test_a_request_made_without_kept_figures_asks_the_erp_when_the_quantity_changes_as_before_and_keeps_them_from_then_on(world):
    app, erp = world
    rec = propose(app)
    tamper(app, "proposals", {"id": app.proposals.list()[0].id}, without_kept_figures)
    assert "tax_setups" not in app.proposals.list()[0].ranking
    real, asked = erp.get_purchase_taxes, []
    erp.get_purchase_taxes = lambda sup, item, day: asked.append(sup) or real(sup, item, day)
    res = app.approvals.decide(rec.id, "change_qty", OWNER, "app", {"qty": 800})
    assert res.ok and res.status == "revised"
    assert sorted(asked) == ["ABROAD", "LOCAL"], "with nothing kept, every vendor's tax setup is read, which is what the code before did"
    assert the_vendor(app, "ABROAD")["tax_total"] == 1800.0 and the_vendor(app, "LOCAL")["total"] == 8000.0
    assert set(app.proposals.list()[0].ranking["tax_setups"]) == {"LOCAL", "ABROAD"}, "and from now on the request carries them"
    assert "Taxes are not included" not in request_text(res.approval)
    asked.clear()
    app.approvals.decide(res.approval.id, "change_qty", OWNER, "app", {"qty": 900})
    assert asked == [], "the second change reads nothing"
