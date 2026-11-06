"""Purchase requisitions as well as purchase orders, and who is recorded as the creator in ERPNext."""
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import APPROVER, OWNER
from erp_agents.core.api.app import create_app
from erp_agents.core.auth.erpnext import ErpNextLogin
from erp_agents.core.auth.sessions import LoginThrottle, SessionStore
from ai_munimji_engine.core import acting
from ai_munimji_engine.core.acting import ActingAs
from ai_munimji_engine.packs.stock_procurement.settings import ProcurementSettings
from ai_munimji_engine.core.workorders import REORDER_NEEDED
from ai_munimji_engine.packs.stock_procurement.erp import PurchaseOrderDraft, RequisitionDraft
from erp_agents.core.erp.erpnext import ERPNextClient
from erp_agents.core.erp.mock import MockERP
from ai_munimji_engine.packs.stock_procurement.procurement.messages import KIND_PO, KIND_PR, route_for
from test_auth_erpnext import FakeErp
from test_landed_cost import ABROAD, COST_ONLY, LOCAL, offer, wo


@pytest.fixture
def world(make_app, clock):
    erp = MockERP(today=clock().date(), scenarios=False)
    erp.add_item("BOLT", "Bolt", lambda age: 40, 240, group="Fasteners", uom="pcs", lead_time_days=5)
    erp.add_vendors("BOLT", [offer("LOCAL", "Local Co", 10.0), offer("ABROAD", "Overseas Ltd", 9.0, "Vietnam")])
    erp.purchase_taxes = {"LOCAL": LOCAL, "ABROAD": ABROAD}
    app = make_app(erp=erp)
    app.settings.update_procurement(OWNER, COST_ONLY)
    return app, erp


def request_for(app, **settings):
    if settings:
        app.settings.update_procurement(OWNER, settings)
    app.workorders.emit(REORDER_NEEDED, wo())
    app.procurement.run()
    return app.approvals.list(status="pending")[0]


# ------------------------------------------------------------------ which items get a requisition first
def test_the_rule_names_items_and_groups():
    s = ProcurementSettings(requisition_first_groups=["Fasteners"], requisition_first_items=["CAP-1"])
    assert route_for(s, "BOLT", "Fasteners") == "requisition" and route_for(s, "CAP-1", "Other") == "requisition"
    assert route_for(s, "BOLT", "Paint") == "order" and route_for(s, "BOLT", None) == "order"
    assert route_for(ProcurementSettings(), "BOLT", "Fasteners") == "order"                           # nothing set: orders as before


def test_the_rule_is_a_setting_with_clear_limits():
    s = ProcurementSettings()
    assert s.requisition_first_groups == [] and s.requisition_first_items == [] and s.require_personal_login is False
    with pytest.raises(ValueError):
        ProcurementSettings(requisition_first_groups=["x"] * 101)


# ------------------------------------------------------------------ what the owner is asked
def test_an_ordinary_item_offers_the_order_and_a_requisition_as_an_alternative(world):
    app, erp = world
    rec = request_for(app)
    req = rec.request
    assert req.kind == KIND_PO and req.title.startswith("Purchase order:")
    assert [o.action for o in req.options] == ["approve", "snooze", "dismiss", "requisition", "change_qty", "change_vendor", "details"]
    assert [o.label for o in req.options[:4]][0] == "Create order" and req.options[3].label == "Requisition instead" and len(req.options[3].label) <= 20


def test_the_first_three_options_never_move_because_whatsapp_buttons_are_matched_by_position(world):
    app, erp = world
    for settings in ({}, {"requisition_first_groups": ["Fasteners"]}):
        rec = request_for(app, **settings) if settings else request_for(app)
        assert [o.action for o in rec.request.options[:3]] == ["approve", "snooze", "dismiss"]
        app.approvals.decide(rec.id, "dismiss", OWNER, "app")


def test_a_requisition_first_item_is_asked_about_as_a_requisition_and_cannot_be_ordered_directly(world):
    app, erp = world
    req = request_for(app, requisition_first_groups=["Fasteners"]).request
    assert req.kind == KIND_PR and req.title == "Purchase requisition: Bolt (BOLT)"
    assert [o.action for o in req.options] == ["approve", "snooze", "dismiss", "change_qty", "change_vendor", "details"] and req.options[0].label == "Create requisition"
    text = json.dumps(req.model_dump(mode="json"))
    assert "purchase requisition (a Material Request)" in text and "Note to vendor" not in text and "DRAFT purchase order" not in text
    assert req.headline.startswith("Request ") and "Local Co" in req.headline or "Overseas Ltd" in req.headline
    assert req.facts["item_code"] == "BOLT"


def test_with_orders_switched_off_there_is_no_requisition_shortcut(world):
    app, erp = world
    assert "requisition" not in [o.action for o in request_for(app, po_mode="off").request.options]


# ------------------------------------------------------------------ what is created
def test_approving_a_requisition_first_item_creates_a_requisition_and_no_order(world):
    app, erp = world
    rec = request_for(app, requisition_first_groups=["Fasteners"])
    res = app.approvals.decide(rec.id, "approve", APPROVER, "app")
    assert res.status == "decided" and erp.purchase_orders == [] and len(erp.requisitions) == 1
    r = erp.requisitions[0]
    prop = app.proposals.list()[0]
    assert (r["item_code"], r["qty"], r["uom"]) == ("BOLT", prop.qty, "pcs") and r["reference"] == prop.id and r["approved_by"] == "manager via app"
    assert "Recommended vendor: Local Co" in r["note"] and "each" in r["note"]
    assert prop.status == "ordered" and prop.po_name == r["name"] and prop.ranking["document"] == "requisition"
    assert "draft purchase requisition MR-MOCK-0001" in prop.status_note and "created by the service account" in prop.status_note
    assert app.workorders.list()[0]["status"] == "done"


def test_the_owner_can_choose_a_requisition_for_an_ordinary_item(world):
    app, erp = world
    rec = request_for(app)
    assert app.approvals.decide(rec.id, "requisition", APPROVER, "app").status == "decided"
    assert erp.purchase_orders == [] and len(erp.requisitions) == 1 and app.proposals.list()[0].ranking["document"] == "requisition"


def test_the_ordinary_choice_still_creates_the_purchase_order(world):
    app, erp = world
    rec = request_for(app)
    app.approvals.decide(rec.id, "approve", APPROVER, "app")
    assert len(erp.purchase_orders) == 1 and erp.requisitions == [] and app.proposals.list()[0].ranking["document"] == "order"


def test_a_refused_requisition_is_reported_and_the_decision_stays_open_and_a_retry_makes_one(world):
    app, erp = world
    rec = request_for(app, requisition_first_groups=["Fasteners"])
    erp.fail_purchase_orders = True
    first = app.approvals.decide(rec.id, "approve", APPROVER, "app")
    assert first.status == "error" and "purchase requisition" in first.message and app.approvals.get(rec.id).status == "pending"
    erp.fail_purchase_orders = False
    assert app.approvals.decide(rec.id, "approve", APPROVER, "app").status == "decided" and len(erp.requisitions) == 1


def test_a_decision_made_twice_never_makes_two_requisitions(world):
    app, erp = world
    rec = request_for(app, requisition_first_groups=["Fasteners"])
    app.approvals.decide(rec.id, "approve", APPROVER, "app")
    assert app.approvals.decide(rec.id, "approve", APPROVER, "whatsapp").status == "already_decided" and len(erp.requisitions) == 1


# ------------------------------------------------------------------ who is recorded as the creator
def test_without_a_personal_session_the_service_account_creates_it_and_the_approver_is_written_on_it(world):
    app, erp = world
    app.approvals.decide(request_for(app).id, "approve", APPROVER, "whatsapp")
    po = erp.purchase_orders[0]
    assert po["created_by"] == "service account" and po["approved_by"] == "manager via whatsapp"


def test_with_the_approvers_own_session_the_document_is_created_under_their_name(world):
    app, erp = world
    rec = request_for(app)
    who = ActingAs(user="manager", session=object(), csrf="tok")
    with acting.acting_as(who):
        app.approvals.decide(rec.id, "approve", APPROVER, "app")
    assert erp.purchase_orders[0]["created_by"] == "manager"
    assert "created under manager's own ERPNext login" in app.proposals.list()[0].status_note


def test_somebody_elses_session_is_never_used_for_a_decision(world):
    app, erp = world
    rec = request_for(app)
    with acting.acting_as(ActingAs(user="someone-else@acme.com", session=object(), csrf="tok")):
        app.approvals.decide(rec.id, "approve", APPROVER, "app")                       # the decision was made by "manager"
    assert erp.purchase_orders[0]["created_by"] == "service account"


def test_strict_mode_refuses_an_approval_that_has_no_personal_session_and_creates_nothing(world):
    app, erp = world
    rec = request_for(app, require_personal_login=True)
    res = app.approvals.decide(rec.id, "approve", APPROVER, "whatsapp")
    assert res.status == "error" and "own ERPNext name" in res.message and "approve it in the console" in res.message
    assert erp.purchase_orders == [] and app.approvals.get(rec.id).status == "pending" and app.workorders.list()[0]["status"] == "claimed"
    with acting.acting_as(ActingAs(user="manager", session=object(), csrf="tok")):
        assert app.approvals.decide(rec.id, "approve", APPROVER, "app").status == "decided"
    assert erp.purchase_orders[0]["created_by"] == "manager"


def test_through_the_console_the_signed_in_persons_session_is_the_one_used(make_app, clock):
    erp_fake = FakeErp()
    mock = MockERP(today=clock().date(), scenarios=False)
    mock.add_item("BOLT", "Bolt", lambda age: 40, 240, group="Fasteners", uom="pcs", lead_time_days=5)
    mock.add_vendors("BOLT", [offer("LOCAL", "Local Co", 10.0)])
    mock.purchase_taxes = {"LOCAL": LOCAL}
    app = make_app(erp=mock)
    app.login = ErpNextLogin("http://erp.local", session_factory=erp_fake.session)
    app.sessions = SessionStore(app.login)
    app.throttle = LoginThrottle()
    app.settings.update_procurement(OWNER, COST_ONLY)
    c = TestClient(create_app(app))
    token = c.post("/api/v1/auth/login", json={"username": "priya@acme.com", "password": "pw-priya"}).json()["token"]
    h = {"Authorization": "Bearer " + token}
    rec = request_for(app)
    assert c.post(f"/api/v1/approvals/{rec.id}/decision", json={"action": "approve"}, headers=h).status_code == 200
    assert mock.purchase_orders[0]["created_by"] == "priya@acme.com" and mock.purchase_orders[0]["approved_by"] == "priya@acme.com via app"
    # a decision someone else sends in this person's name cannot borrow their session: params are not an identity
    rec2 = request_for(app)
    c2 = TestClient(create_app(app))
    forged = c2.post(f"/api/v1/approvals/{rec2.id}/decision", json={"action": "approve", "params": {"erpnext_user": "priya@acme.com"}}, headers={"Authorization": "Bearer nope"})
    assert forged.status_code == 401


# ------------------------------------------------------------------ the ERPNext adapter
class Resp:
    def __init__(self, data=None, status=200, text=""):
        self._d, self.status_code, self.text = data if data is not None else {}, status, text or json.dumps(data or {})

    def json(self):
        return self._d


class Docs:
    """A little ERPNext that stores what it is sent, per doctype, and says who sent it."""

    def __init__(self, name, fail=None):
        self.name, self.fail, self.headers, self.docs, self.posts = name, fail, {}, [], []

    def get(self, url, params=None, timeout=None):
        if "/Custom Field/" in url:
            return Resp({"data": {}})
        if url.endswith("/Company/Demo%20Co"):
            return Resp({"data": {"default_currency": "INR"}})
        if url.endswith("/Supplier/S-A"):
            return Resp({"data": {"default_currency": "INR"}})
        doctype = url.rsplit("/api/resource/", 1)[1]
        flt = json.loads(params["filters"])
        ref = next(f[2] for f in flt if f[0] == "custom_agent_ref")
        return Resp({"data": [{"name": d["name"]} for d in self.docs if d["doctype"] == doctype and d.get("custom_agent_ref") == ref]})

    def post(self, url, json=None, timeout=None, headers=None):
        doctype = url.rsplit("/api/resource/", 1)[1]
        self.posts.append((doctype, headers))
        if self.fail:
            return Resp(*self.fail)
        doc = {**json, "doctype": doctype, "name": f"{doctype[:2].upper()}-{len(self.docs) + 1}", "owner": self.name}
        self.docs.append(doc)
        return Resp({"data": {"name": doc["name"]}})


def client_with(service, **kw):
    return ERPNextClient("http://erp.local", "K", "S", session=service, company="Demo Co", **kw)


def test_a_document_created_with_the_persons_session_goes_through_their_session_with_the_csrf_token():
    service, person = Docs("agent@x.com"), Docs("priya@acme.com")
    res = client_with(service).create_purchase_order(PurchaseOrderDraft("S-A", "BOLT", 5, 1, "Nos", date(2026, 11, 1), reference="p1", approved_by="priya@acme.com via app"),
                                                   acting=ActingAs("priya@acme.com", person, "csrf-123"))
    assert res.ok and person.docs and not service.docs and person.docs[0]["owner"] == "priya@acme.com"
    assert person.posts[0][1] == {"X-Frappe-CSRF-Token": "csrf-123"} and person.docs[0]["custom_agent_approved_by"] == "priya@acme.com via app"


def test_without_a_personal_session_the_service_account_is_used_and_no_csrf_header_is_sent():
    service = Docs("agent@x.com")
    client_with(service).create_purchase_order(PurchaseOrderDraft("S-A", "BOLT", 5, 1, "Nos", date(2026, 11, 1), reference="p1"))
    assert service.posts[0][1] is None and service.docs[0]["owner"] == "agent@x.com"


def test_erpnext_refusing_the_person_is_reported_in_erpnexts_own_words():
    person = Docs("arjun@acme.com", fail=({"_server_messages": json.dumps([json.dumps({"message": "User <b>arjun@acme.com</b> does not have permission to create a Purchase Order"})])}, 403))
    res = client_with(Docs("agent@x.com")).create_purchase_order(PurchaseOrderDraft("S-A", "BOLT", 5, 1, "Nos", date(2026, 11, 1), reference="p1"),
                                                                acting=ActingAs("arjun@acme.com", person, "t"))
    assert not res.ok and "does not allow arjun@acme.com to create a purchase order" in res.error and "does not have permission" in res.error and "<b>" not in res.error


def test_an_expired_erpnext_session_asks_the_person_to_sign_in_again():
    person = Docs("p@x.com", fail=({"exc_type": "CSRFTokenError"}, 400, '{"exc_type":"CSRFTokenError"}'))
    res = client_with(Docs("agent@x.com")).create_material_request(RequisitionDraft("BOLT", 5, "Nos", date(2026, 11, 1), reference="p1"), acting=ActingAs("p@x.com", person, "old"))
    assert not res.ok and "sign out of the Stock Assistant and sign in again" in res.error


def test_a_requisition_is_a_material_request_of_type_purchase_with_the_recommendation_for_the_buyer():
    service = Docs("agent@x.com")
    res = client_with(service).create_material_request(RequisitionDraft("BOLT", 500, "Nos", date(2026, 11, 1), rate=14.95, note="Recommended vendor: Local Co.", reference="p1", approved_by="priya via app"))
    d = service.docs[0]
    assert res.ok and d["doctype"] == "Material Request" and d["material_request_type"] == "Purchase" and d["company"] == "Demo Co"
    assert d["items"] == [{"item_code": "BOLT", "qty": 500, "uom": "Nos", "schedule_date": "2026-11-01", "rate": 14.95}]
    assert d["custom_agent_ref"] == "p1" and d["custom_agent_approved_by"] == "priya via app" and d["custom_agent_note"] == "Recommended vendor: Local Co." and "docstatus" not in d


def test_a_retried_requisition_finds_the_first_one_and_orders_and_requisitions_do_not_clash():
    service = Docs("agent@x.com")
    c = client_with(service)
    d = RequisitionDraft("BOLT", 5, "Nos", date(2026, 11, 1), reference="p1")
    first, again = c.create_material_request(d), c.create_material_request(d)
    assert first.name == again.name and len(service.docs) == 1
    assert c.create_purchase_order(PurchaseOrderDraft("S-A", "BOLT", 5, 1, "Nos", date(2026, 11, 1), reference="p1")).ok and len(service.docs) == 2     # same proposal id, different document type


def test_setup_makes_the_fields_on_every_document_once():
    class Setup(Docs):
        def __init__(self):
            super().__init__("agent@x.com")
            self.made, self.exist = [], False

        def get(self, url, params=None, timeout=None):
            if "/Custom Field/" in url:
                return Resp({"data": {}}) if self.exist else Resp({}, 404)
            return super().get(url, params, timeout)

        def post(self, url, json=None, timeout=None, headers=None):
            if url.endswith("/Custom Field"):
                self.made.append((json["dt"], json["fieldname"], json["fieldtype"]))
                return Resp({"data": {"name": "x"}})
            return super().post(url, json, timeout, headers)

    s = Setup()
    c = client_with(s)
    assert c.ensure_po_reference_field() == "created"
    assert sorted(s.made) == sorted([("Purchase Order", "custom_agent_ref", "Data"), ("Purchase Order", "custom_agent_approved_by", "Data"), ("Material Request", "custom_agent_ref", "Data"),
                                     ("Material Request", "custom_agent_approved_by", "Data"), ("Material Request", "custom_agent_note", "Small Text"),
                                     ("Stock Entry", "custom_agent_ref", "Data"), ("Stock Entry", "custom_agent_approved_by", "Data")])
    s.exist = True
    assert c.ensure_po_reference_field() == "exists" and len(s.made) == 7


def test_a_requisition_is_refused_with_the_fix_when_its_fields_are_missing():
    class NoFields(Docs):
        def get(self, url, params=None, timeout=None):
            return Resp({}, 404) if "/Custom Field/" in url else super().get(url, params, timeout)

    s = NoFields("agent@x.com")
    res = client_with(s).create_material_request(RequisitionDraft("BOLT", 5, "Nos", date(2026, 11, 1), reference="p1"))
    assert not res.ok and "erp-agents erp-setup" in res.error and "Material Request" in res.error and s.docs == []
