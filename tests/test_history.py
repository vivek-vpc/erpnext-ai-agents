""""Why did you do that?": the decision history (core/history.py) and its seal (core/audit.py).

Every past decision is found with the reasons it gave, how it was answered and what came of it; a change made to a sealed record afterwards shows; the AI agents' view of
it names nobody."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from conftest import OWNER, VIEWER
from erp_agents.core.api.app import create_app
from ai_munimji_engine.core.common import Actor
from erp_agents.core.erp.mock import MockERP
from conftest import tamper, remove_rows

PRIYA = Actor("priya@acme.example", frozenset({"admin", "approver"}), "test")


def pending(app, kind):
    return [r for r in app.approvals.list("pending", 100) if r.kind == kind]


@pytest.fixture
def world(make_app, clock):
    """A reorder of BOLT-M8 that went all the way: alert -> Start reorder -> purchase request -> Change quantity -> Create order."""
    app = make_app(erp=MockERP(today=clock().date()))
    app.inventory.run()
    bolt = next(r for r in pending(app, "inventory.reorder") if r.request.facts["item_code"] == "BOLT-M8")
    app.approvals.decide(bolt.id, "approve", PRIYA, "telegram")
    app.procurement.run()
    po = next(r for r in pending(app, "procurement.po") if r.request.facts["item_code"] == "BOLT-M8")
    app.approvals.decide(po.id, "change_qty", PRIYA, "app", {"qty": 900})
    revised = next(r for r in pending(app, "procurement.po") if r.request.facts["item_code"] == "BOLT-M8")
    app.approvals.decide(revised.id, "approve", PRIYA, "app")
    app.settings.update_policy(PRIYA, "item", "BOLT-M8", {"min_days_cover": 10})
    return app, {"reorder": bolt.id, "first_po": po.id, "po": revised.id}


def test_every_decision_is_found_with_its_reasons_and_how_it_ended(world):
    app, ids = world
    res = app.history.search(q="BOLT-M8")
    by_id = {e["id"]: e for e in res["entries"]}
    assert {ids["reorder"], ids["first_po"], ids["po"]} <= set(by_id)
    po = by_id[ids["po"]]
    assert po["status"] == "decided" and po["answer"] == "Create order" and po["answered_by"] == "priya@acme.example" and po["answered_via"] == "app"
    assert "Create order by priya@acme.example via app." in po["outcome"] and "PO-MOCK-" in po["outcome"]                 # what came of it
    assert po["why"] and po["what"].startswith("A purchase request")
    reorder = by_id[ids["reorder"]]
    assert reorder["answer"] == "Start reorder" and any("BOLT-M8" in w or "Bolt" in w or "order" in w.lower() for w in reorder["why"])
    replaced = by_id[ids["first_po"]]
    assert replaced["status"] == "cancelled" and "replaced by a revised request" in replaced["outcome"]


def test_settings_changes_are_decisions_too(world):
    app, _ = world
    (e,) = [e for e in app.history.search(q="min_days_cover")["entries"] if e["type"] == "setting"]
    assert e["answered_by"] == "priya@acme.example" and "10" in e["headline"] and e["item_code"] == "BOLT-M8"


def test_the_filters(world, clock):
    app, ids = world
    assert {e["kind"] for e in app.history.search(kind="procurement.po")["entries"]} == {"procurement.po"}
    assert all(e["item_code"] == "OIL-5L" for e in app.history.search(item="OIL-5L", include_settings=False)["entries"])
    assert {e["id"] for e in app.history.search(who="priya", include_settings=False)["entries"]} >= {ids["reorder"], ids["po"]}
    assert not app.history.search(who="nobody-else")["entries"]
    assert {e["status"] for e in app.history.search(status="pending")["entries"]} == {"pending"}
    today = clock().date().isoformat()
    assert app.history.search(since=today, until=today)["total"] == app.history.search()["total"]
    assert app.history.search(since="2099-01-01")["total"] == 0
    assert app.history.search(q="BOLT-M8 Prime")["total"] <= app.history.search(q="BOLT-M8")["total"]                   # every word must appear
    assert app.history.search(q="100%_")["total"] == 0                                                                  # wildcards are plain text
    with pytest.raises(ValueError, match="not a date"):
        app.history.search(since="last tuesday")


def test_one_decision_in_full(world):
    app, ids = world
    e = app.history.entry(ids["po"])
    assert e["sections"] and e["options"] and e["facts"]["item_code"] == "BOLT-M8" and e["facts"]["qty"] == 900
    assert [v["id"] for v in e["versions"]] == [ids["first_po"]]                                                       # the version it replaced
    assert [r["kind"] for r in e["related"]] == ["procurement.order"]                                                 # and what followed from it
    ctx = {c["heading"]: c["lines"] for c in e["context"]}
    assert "PO-MOCK-" in ctx["Purchase request"][0] and "ordered" in ctx["Purchase request"][0]
    assert e["sealed_at"] and all("address" not in d for d in e["sent_to"])
    assert app.history.entry("nope") is None


def test_the_agents_tool_tells_the_story_without_naming_anyone(world):
    app, _ = world
    out = app.toolkit.call("decision_history", {"item_code": "BOLT-M8", "limit": 10}, VIEWER)
    text = json.dumps(out)
    assert out["found"] >= 3 and "Create order" in text and "a person" in text
    assert "priya" not in text.lower() and "acme.example" not in text
    assert "decision_history" in __import__("erp_agents.packs.stock_procurement.agents.purchase_agent", fromlist=["x"]).ASK_INVENTORY_TOOLS


# ====================================================================== the seal
def test_closed_decisions_and_settings_changes_are_sealed_and_verify(world):
    app, ids = world
    res = app.audit.verify()
    assert res["ok"] and res["entries"] >= 4 and res["not_sealed_yet"] == 0 and len(res["latest_hash"]) == 64
    assert app.history.search(q=ids["po"])["entries"][0]["sealed"]


def test_a_decision_changed_after_it_was_sealed_shows(world):
    app, ids = world
    tamper(app, "approvals", {"id": ids["po"]}, lambda r: {"decision_json": r["decision_json"].replace("priya@acme.example", "someone-else")})
    res = app.audit.verify()
    assert not res["ok"] and any(f"request {ids['po']} was changed after it was sealed" in p for p in res["problems"])


def test_a_deleted_decision_shows(world):
    app, ids = world
    remove_rows(app, "approvals", {"id": ids["reorder"]})
    assert any(f"request {ids['reorder']} was deleted" in p for p in app.audit.verify()["problems"])


def test_an_edited_seal_shows(world):
    app, _ = world
    # Change the first character of the stored digest. It is random hex, so writing a fixed '0' left it unchanged one time in sixteen and the edit went unnoticed because there was none.
    tamper(app, "audit_chain", {"seq": 2}, lambda r: {"digest": ("1" if r["digest"][0] == "0" else "0") + r["digest"][1:]})
    assert any("the seal itself was edited at entry 2" in p for p in app.audit.verify()["problems"])


def test_a_changed_setting_after_sealing_shows(world):
    app, _ = world
    tamper(app, "settings_audit", {"field": "min_days_cover"}, lambda r: {"new_json": "99"})
    assert any("settings change" in p and "was changed after it was sealed" in p for p in app.audit.verify()["problems"])


@pytest.mark.sqlite_only
def test_what_was_closed_before_the_seal_existed_is_sealed_at_start(tmp_path, clock):
    from erp_agents.core.bootstrap import build_app
    from erp_agents.core.config import DEFAULT_CONFIG
    import copy
    db = str(tmp_path / "old.db")
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["database"]["path"] = db
    first = build_app(config=cfg, clock=clock, erp=MockERP(today=clock().date()), db_path=db)
    first.inventory.run()
    rec = first.approvals.list("pending", 5)[0]
    action = next(o.action for o in rec.request.options if o.terminal)
    assert first.approvals.decide(rec.id, action, OWNER, "test").ok
    first.db.close()
    c = sqlite3.connect(db)
    c.execute("DELETE FROM audit_chain")                                                                         # as a database from before the seal existed
    c.commit()
    c.close()
    second = build_app(config=cfg, clock=clock, erp=MockERP(today=clock().date()), db_path=db)
    rows = second.db.query("SELECT event FROM audit_chain WHERE source='approval'")
    assert rows and {r["event"] for r in rows} == {"sealed"} and second.audit.verify()["ok"]
    second.db.close()


def test_sealing_cannot_undo_an_answer(world, monkeypatch):
    app, _ = world
    monkeypatch.setattr(app.audit, "sweep", lambda *a: (_ for _ in ()).throw(RuntimeError("disk full")))
    rec = pending(app, "inventory.reorder")[0]
    assert app.approvals.decide(rec.id, "dismiss", OWNER, "test").ok                                               # the maintenance job seals it later


# ====================================================================== export, routes, command
def test_the_csv_export_cannot_run_formulas(world):
    app, _ = world
    app.settings.update_policy(OWNER, "item", "=HYPERLINK(1)", {"min_qty": 5})
    text = app.history.csv()
    assert text.splitlines()[0].startswith("at,type,id,what,title")
    assert "'=HYPERLINK" in text and ",=HYPERLINK" not in text


TOKENS = [{"token": "o" * 40, "user": "owner", "roles": ["viewer", "approver", "admin"]}, {"token": "a" * 40, "user": "manager", "roles": ["viewer", "approver"]},
          {"token": "v" * 40, "user": "accountant", "roles": ["viewer"]}]
H = {r: {"Authorization": "Bearer " + c * 40} for r, c in (("admin", "o"), ("approver", "a"), ("viewer", "v"))}


def test_the_routes_and_who_may_use_them(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()), cfg={"auth": {"tokens": TOKENS}})
    app.inventory.run()
    rec = app.approvals.list("pending", 5)[0]
    app.approvals.decide(rec.id, "dismiss", OWNER, "test")
    c = TestClient(create_app(app))
    assert c.get("/api/v1/history", headers=H["viewer"]).status_code == 403                                       # decisions are for the people who make them
    body = c.get("/api/v1/history", params={"q": rec.request.facts["item_code"]}, headers=H["approver"]).json()
    assert rec.id in [e["id"] for e in body["entries"]]
    assert c.get(f"/api/v1/history/{rec.id}", headers=H["approver"]).json()["status"] == "decided"
    assert c.get("/api/v1/history/nope", headers=H["approver"]).status_code == 404
    assert c.get("/api/v1/history", params={"since": "soon"}, headers=H["approver"]).status_code == 422
    csv_resp = c.get("/api/v1/history.csv", headers=H["approver"])
    assert csv_resp.status_code == 200 and csv_resp.headers["content-type"].startswith("text/csv") and rec.id in csv_resp.text
    assert c.get("/api/v1/history/verify", headers=H["approver"]).status_code == 403
    assert c.get("/api/v1/history/verify", headers=H["admin"]).json()["ok"]
    assert c.get("/api/v1/history").status_code == 401


def test_the_history_command(tmp_path, capsys):
    from erp_agents.core.cli import main
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(f"database: {{path: '{(tmp_path / 'a.db').as_posix()}'}}\nerp: {{type: mock}}\nsystem: {{stock_alerts: false}}\n", encoding="utf-8")
    assert main(["--config", str(cfg), "run-once"]) == 0
    capsys.readouterr()
    assert main(["--config", str(cfg), "history", "BOLT-M8"]) == 0
    out = capsys.readouterr().out
    assert "decision" in out and "BOLT-M8" in out and "why:" in out
    rid = next(line.split()[2] for line in out.splitlines() if line[:2] == "20")
    assert main(["--config", str(cfg), "history", "--show", rid]) == 0 and f'"id": "{rid}"' in capsys.readouterr().out
    assert main(["--config", str(cfg), "history", "--verify"]) == 0 and "OK:" in capsys.readouterr().out
    assert main(["--config", str(cfg), "history", "--csv", str(tmp_path / "h.csv")]) == 0 and (tmp_path / "h.csv").read_text(encoding="utf-8").startswith("at,type")
    assert main(["--config", str(cfg), "history", "--since", "someday"]) == 2


def test_an_answer_is_sealed_the_moment_it_is_given(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()))
    app.inventory.run()
    rec = pending(app, "inventory.reorder")[0]
    assert app.audit.sealed_at("approval", rec.id) is None
    app.approvals.decide(rec.id, "dismiss", OWNER, "test")
    assert app.audit.sealed_at("approval", rec.id) is not None                                                  # no other write in between
    other = pending(app, "inventory.reorder")[0]
    app.approvals.cancel(other.id, "test")
    assert app.audit.sealed_at("approval", other.id) is not None


def test_both_layers_keep_people_out_of_what_a_model_sees(world):
    app, ids = world
    rec = app.approvals.get(ids["po"])
    assert "priya" not in app.history.outcome(rec, with_people=False) and "by a person" in app.history.outcome(rec, with_people=False)   # the outcome is written without them
    assert app.history._scrub([{"x": "changed by priya@acme.example via app"}]) == [{"x": "changed by a person via app"}]                 # and any name left in the text is taken out


def test_a_database_from_before_the_seal_is_complete_and_gets_the_seal_when_opened(tmp_path):
    from erp_agents.core import ops
    from erp_agents.core.storage import Database
    path = tmp_path / "before.db"
    Database(str(path)).close()
    c = sqlite3.connect(path)
    c.execute("DROP TABLE audit_chain")                                                                          # as a version before the seal left it
    c.commit()
    c.close()
    assert ops.verify_database(str(path)).ok                                                                    # doctor and backup-verify do not call it damaged
    Database(str(path)).close()
    c = sqlite3.connect(path)
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name='audit_chain'").fetchone()                         # opening it adds the table
    c.close()
