"""Moving stock between warehouses, at the ERPNext adapter: a DRAFT Stock Entry of type Material Transfer, safe to retry, with the approver recorded (fake HTTP session)."""
import json

from ai_munimji_engine.core.acting import ActingAs
from ai_munimji_engine.packs.stock_procurement.erp import StockTransferDraft
from erp_agents.core.erp.erpnext import ERPNextClient


class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, json.dumps(data)

    def json(self):
        return self._d


class EntrySession:
    """A small ERPNext: it stores the Stock Entries it receives and can lose the answer to a POST after storing it."""

    def __init__(self, field_exists=True, lose_first_answer=False, refuse=None):
        self.headers, self.entries, self.posts, self.field_exists, self.lose, self.refuse = {}, [], [], field_exists, lose_first_answer, refuse

    def get(self, url, params=None, timeout=None):
        if "/Custom Field/" in url:
            return Resp({"data": {}}) if self.field_exists else Resp({"exception": "not found"}, 404)
        if url.endswith("/Stock Entry"):
            ref = next(f[2] for f in json.loads(params["filters"]) if f[0] == "custom_agent_ref")
            return Resp({"data": [{"name": e["name"]} for e in self.entries if e.get("custom_agent_ref") == ref]})
        raise AssertionError(url)

    def post(self, url, json=None, timeout=None, headers=None):
        self.posts.append((url, json, headers))
        if self.refuse:
            return Resp(*self.refuse)
        self.entries.append({**json, "name": f"MAT-STE-{len(self.entries) + 1:04d}"})
        if self.lose:
            self.lose = False
            import requests
            raise requests.ConnectionError("the answer was lost")
        return Resp({"data": {"name": self.entries[-1]["name"]}})


def client(session):
    return ERPNextClient("http://erp.local", "K", "S", session=session, company="Demo Co")


def draft(**kw):
    return StockTransferDraft(**{"item_code": "BOLT", "qty": 40.0, "uom": "Nos", "from_warehouse": "Stores - DC", "to_warehouse": "Shop floor - DC",
                                 "remarks": "Requested by anna", "reference": "tr1", "approved_by": "priya@acme.com via app", **kw})


def test_a_draft_material_transfer_is_posted_with_both_warehouses_and_the_approver():
    s = EntrySession()
    res = client(s).create_stock_transfer(draft())
    assert res.ok and res.name == "MAT-STE-0001"
    url, body, _headers = s.posts[0]
    assert url.endswith("/api/resource/Stock Entry") and body["stock_entry_type"] == "Material Transfer" and body["company"] == "Demo Co"
    assert (body["from_warehouse"], body["to_warehouse"]) == ("Stores - DC", "Shop floor - DC")
    (line,) = body["items"]
    assert line == {"item_code": "BOLT", "qty": 40.0, "uom": "Nos", "stock_uom": "Nos", "conversion_factor": 1, "transfer_qty": 40.0, "s_warehouse": "Stores - DC", "t_warehouse": "Shop floor - DC"}
    assert body["custom_agent_ref"] == "tr1" and body["custom_agent_approved_by"] == "priya@acme.com via app" and body["remarks"] == "Requested by anna"
    assert "docstatus" not in body and len(s.posts) == 1                                           # a draft: nothing is submitted, so no stock moves


def test_a_retry_after_a_lost_answer_finds_the_transfer_instead_of_creating_a_second():
    s = EntrySession(lose_first_answer=True)
    first = client(s).create_stock_transfer(draft())
    assert not first.ok and "could not reach" in first.error and len(s.entries) == 1               # ERPNext did create it
    second = client(s).create_stock_transfer(draft())
    assert second.ok and second.name == "MAT-STE-0001" and len(s.entries) == 1
    other = client(s).create_stock_transfer(draft(reference="tr2"))
    assert other.name == "MAT-STE-0002"                                                            # a different request gets its own


def test_the_same_warehouse_twice_is_refused_before_anything_is_sent():
    s = EntrySession()
    res = client(s).create_stock_transfer(draft(to_warehouse="Stores - DC"))
    assert not res.ok and "same" in res.error and s.posts == []


def test_without_the_erpnext_field_nothing_is_created_and_the_fix_is_named():
    s = EntrySession(field_exists=False)
    res = client(s).create_stock_transfer(draft())
    assert not res.ok and "erp-agents erp-setup" in res.error and s.entries == []


def test_erpnext_refusing_the_document_gives_its_own_words():
    s = EntrySession(refuse=({"exception": "frappe.exceptions.ValidationError: Warehouse is disabled"}, 417))
    res = client(s).create_stock_transfer(draft())
    assert not res.ok and "refused the stock transfer" in res.error and "Warehouse is disabled" in res.error


def test_the_approvers_own_session_creates_it_when_there_is_one():
    service, person = EntrySession(), EntrySession()
    res = client(service).create_stock_transfer(draft(), acting=ActingAs("priya@acme.com", person, "csrf123"))
    assert res.ok and service.posts == [] and person.posts[0][2] == {"X-Frappe-CSRF-Token": "csrf123"}      # ERPNext records them as the creator and applies their permissions
    denied = EntrySession(refuse=({"exception": "PermissionError"}, 403))
    res = client(EntrySession()).create_stock_transfer(draft(reference="tr9"), acting=ActingAs("priya@acme.com", denied, ""))
    assert not res.ok and "does not allow priya@acme.com to create a stock transfer" in res.error
