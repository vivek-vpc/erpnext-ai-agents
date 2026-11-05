"""What leaves the server when the agents run (docs/PRIVACY.md): no customer or staff personal data in any tool result, and nothing about the asker in what the model is sent."""
import json

import pytest

pytest.importorskip("mcp", reason="the optional 'agents' extra is not installed")

from conftest import APPROVER, OWNER, VIEWER
from erp_agents.core.agents.privacy import KEPT, NEVER_SENT, privacy_summary
from ai_munimji_engine.core.common import Actor
from ai_munimji_engine.core.policy import AgentSettings
from erp_agents.core.erp.mock import MockERP
from ai_munimji_engine.core.packs import PackRegistry
from test_agents import done, good_brief, say, use, with_model

SENT = list(PackRegistry.discover().manifests["stock_procurement"].sends_to_model)        # what the stock pack declares its agents may send; the core adds nothing of its own
PEOPLE = ["jane.doe@example.com", "boss@example.com", "Jane Doe", "+91 98765 43210", "Acme Retail Customer"]
FORBIDDEN_KEYS = {"customer", "customer_name", "email", "phone", "mobile", "address", "contact", "created_by", "approved_by", "modified_by", "owner", "user", "bank", "tax_id", "gstin", "password", "token"}


def keys_of(x):
    if isinstance(x, dict):
        for k, v in x.items():
            yield str(k).lower()
            yield from keys_of(v)
    elif isinstance(x, list):
        for v in x:
            yield from keys_of(v)


@pytest.fixture
def world(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()))
    app.settings.update_section(OWNER, "agents", {"enabled": True})
    return app


def test_a_document_that_names_people_does_not_pass_them_on(world, monkeypatch):
    app = world
    doc = {"doctype": "Purchase Order", "name": "PUR-ORD-2026-00001", "status": "To Receive and Bill", "docstatus": 1, "party": "Prime Supplies", "date": "2026-10-01", "due": "2026-10-20",
           "created_by": "jane.doe@example.com", "approved_by": "boss@example.com", "per_received": 0.0, "per_billed": 0.0, "total": 1000.0, "currency": "INR", "kind": None,
           "customer": "Acme Retail Customer", "contact_mobile": "+91 98765 43210", "lines": [{"item_code": "BOLT-M8", "qty": 100.0, "done_qty": 0.0, "uom": "pcs", "rate": 10.0, "contact": "Jane Doe"}]}
    monkeypatch.setattr(app.erp, "get_documents", lambda *a, **k: [doc])
    out = app.toolkit.call("open_orders", {"item_code": "BOLT-M8"}, VIEWER)
    text = json.dumps(out)
    assert out["documents"] and out["documents"][0]["vendor"] == "Prime Supplies"
    for person in PEOPLE:
        assert person not in text


def test_no_tool_result_has_a_field_that_holds_a_person(world):
    app = world
    app.toolkit.call("prepare_order_request", {"item_code": "BOLT-M8", "qty": 800, "reason": "test"}, APPROVER)
    results = [app.toolkit.call(n, a, OWNER) for n, a in (("stock_overview", {"limit": 25}), ("stock_status", {"item_code": "BOLT-M8"}), ("usage_history", {"item_code": "BOLT-M8"}),
                                                          ("vendor_options", {"item_code": "BOLT-M8", "qty": 100}), ("open_orders", {}), ("open_orders", {"kind": "requisition"}),
                                                          ("waiting_requests", {}))]
    assert FORBIDDEN_KEYS.isdisjoint(set().union(*(set(keys_of(r)) for r in results)))


def test_the_model_is_never_told_who_is_asking(world):
    app = world
    asker = Actor("jane.doe@example.com", frozenset({"approver"}), "chat")
    llm = with_model(app, use("stock_status", item_code="BOLT-M8"), done(), say({"answerable": False, "answer": ""}))
    res = app.agents.ask("How much BOLT-M8 do we have?", asker)
    sent = json.dumps([c["messages"] for c in llm.calls])
    assert "jane.doe" not in sent and "jane.doe" not in json.dumps(app.agent_log.get(res["task_id"])["trace"])
    assert app.agent_log.get(res["task_id"])["asked_by"] == "jane.doe@example.com"                         # who asked is kept here, in this server's own record


def test_the_order_request_an_agent_prepares_names_the_asker_here_but_not_to_the_model(world):
    app = world
    asker = Actor("jane.doe@example.com", frozenset({"approver"}), "chat")
    llm = with_model(app, use("prepare_order_request", item_code="BOLT-M8", qty=500, reason="x"), done(), say({"answerable": True, "answer": "I prepared the request and sent it to the approvers."}))
    app.agents.ask("Create a purchase order for BOLT-M8 for 500", asker)
    reply_to_model = json.dumps([m for c in llm.calls for m in c["messages"] if m.get("role") == "tool" and m.get("tool_name") == "prepare_order_request"])
    assert "jane.doe" not in reply_to_model and "asked for this in chat" not in reply_to_model


def test_nothing_is_sent_to_a_model_while_the_agents_are_off(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()))
    llm = with_model(app, say(good_brief(app)))
    app.agents.brief()
    app.agents.ask("What is running low?", OWNER)
    app.agents.answer_for_chat("Which warehouse is the busiest this month?", "owner", True)
    assert llm.calls == []


def test_the_summary_and_the_defaults_agree(world):
    s = privacy_summary(AgentSettings(), SENT)
    assert s["agents_on"] is False and s["data_leaves_this_server"] is False and s["sent_to_the_model"] == SENT and s["never_sent_to_the_model"] == NEVER_SENT and s["kept_on_this_server"] == KEPT
    assert AgentSettings().enabled is False and AgentSettings().allow_remote is False and AgentSettings().provider == "ollama" and AgentSettings().effective_base_url().startswith("http://127.0.0.1")
    assert all(len(line) > 20 for line in SENT + NEVER_SENT + KEPT)
