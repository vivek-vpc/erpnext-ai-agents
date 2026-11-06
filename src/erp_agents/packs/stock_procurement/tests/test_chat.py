"""Chat with the agent: questions about stock, vendor comparison and purchase order / requisition status.
Everything is read-only and every answer is built from live data."""
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import APPROVER, OWNER, VIEWER
from erp_agents.core.api.app import create_app
from erp_agents.packs.stock_procurement.assistant.chat import ChatLimit, ChatService, parse_days, parse_qty, resolve_items, understand
from erp_agents.core.assistant.verify import NOT_ENOUGH
from ai_munimji_engine.packs.stock_procurement.erp import ItemInfo
from erp_agents.core.erp.erpnext import ERPNextClient
from erp_agents.core.erp.mock import MockERP
from test_assistant import FakeLLM
from conftest import tamper

ITEMS = [ItemInfo("FAS-0003", "Hex Bolt SS304 M10x50", "Fasteners & Hardware", "Nos"), ItemInfo("FAS-0004", "Hex Bolt SS304 M12x70", "Fasteners & Hardware", "Nos"),
         ItemInfo("MET-0005", "Copper Rod 10mm", "Metals - Raw Material", "Kg"), ItemInfo("PKG-0003", "Stretch Film 500mm 20 micron", "Packaging Materials", "Roll")]


# ------------------------------------------------------------------ understanding the question
@pytest.mark.parametrize("q,intent", [
    ("What is running low?", "stock_overview"), ("what is out of stock", "stock_overview"), ("anything we need to reorder", "stock_overview"),
    ("How much FAS-0003 do we have?", "item_stock"), ("when will copper rod 10mm run out", "item_stock"), ("tell me about MET-0005", "item_stock"),
    ("compare vendors for FAS-0003", "vendor_compare"), ("which vendor is cheapest for copper rod 10mm", "vendor_compare"), ("why landed cost of FAS-0004 so high", "vendor_compare"),
    ("status of our open purchase orders", "document_status"), ("which requisitions did we create this week", "document_status"), ("any PO overdue?", "document_status"),
    ("what is the status of PUR-ORD-2026-00543", "document_status"), ("is there anything overdue", "document_status"),
    ("what is waiting for my OK", "pending_approvals"), ("do I have approvals pending", "pending_approvals"),
])
def test_questions_are_understood_by_plain_rules(q, intent):
    assert understand(q, ITEMS).intent == intent


def test_an_unrelated_question_is_not_forced_into_an_answer():
    assert understand("tell me a joke", ITEMS).intent is None and understand("hello", ITEMS).intent is None


def test_items_are_found_by_code_by_full_name_and_never_guessed_from_a_vague_word():
    assert [i.code for i in resolve_items("stock of fas-0004?", ITEMS)[0]] == ["FAS-0004"]
    assert [i.code for i in resolve_items("how much hex bolt ss304 m10x50 is left", ITEMS)[0]] == ["FAS-0003"]
    assert [i.code for i in resolve_items("copper rod 10mm", ITEMS)[0]] == ["MET-0005"]
    exact, near = resolve_items("how much hex bolt do we have", ITEMS)
    assert exact == [] and {i.code for i in near} == {"FAS-0003", "FAS-0004"}                 # two items fit equally: ask which, do not pick one
    assert resolve_items("what is running low", ITEMS) == ([], [])


def test_a_code_inside_a_sentence_with_punctuation_still_matches():
    assert [i.code for i in resolve_items("Compare vendors for (FAS-0003), please.", ITEMS)[0]] == ["FAS-0003"]


def test_quantity_and_period_are_read_from_the_question():
    assert parse_qty("compare vendors for FAS-0003 for 500") == 500 and parse_qty("buy 1,250 pcs") == 1250 and parse_qty("order 20 kg of copper") == 20
    assert parse_qty("compare vendors for FAS-0003") is None and parse_qty("FAS-0003 0003") is None
    assert [parse_days(x) for x in ("today", "this week", "last 14 days", "this month", "anything")] == [0, 7, 14, 31, 30]


def test_status_and_kind_of_document_are_read_from_the_question():
    p = understand("which purchase requisitions are still open", ITEMS)
    assert p.intent == "document_status" and p.doctypes == ["Material Request"] and p.status == "open"
    assert understand("draft purchase orders for FAS-0003", ITEMS).doctypes == ["Purchase Order"]
    assert understand("what is the status of orders", ITEMS).doctypes == ["Purchase Order"]


def test_a_follow_up_keeps_talking_about_the_previous_item():
    p = understand("and the vendors?", ITEMS, {"items": ["MET-0005"]})
    assert p.intent == "vendor_compare" and [i.code for i in p.items] == ["MET-0005"] and p.from_context
    assert understand("compare vendors", ITEMS, {"items": ["NOPE"]}).items == []                 # a stale or invented context item is ignored


# ------------------------------------------------------------------ answers from the demo ERP
@pytest.fixture
def world(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()))
    app.inventory.run()
    return app


def ask(app, q, decide=True, ctx=None):
    return app.chat.ask(q, "tester", decide, ctx)


def test_what_is_running_low_summarises_and_lists_the_items(world):
    r = ask(world, "What is running low?")
    assert r["intent"] == "stock_overview" and r["status"] == "answered" and r["source"] == "data"
    rows = world.inventory.overview()
    needing = [x for x in rows if x["status"] in ("BREACH", "EARLY_WARNING")]
    assert f"{len(rows)} items" in r["answer"] and all(x["item_name"] in r["answer"] for x in needing[:6])
    assert r["blocks"][0]["type"] == "table" and len(r["blocks"][0]["rows"]) == len(needing)
    assert r["facts"][0]["id"] == "F1" and any("on hand" in f["text"] for f in r["facts"])
    assert ask(world, "what is out of stock")["answer"] == "Nothing is out of stock."


def test_a_group_question_stays_inside_the_group(world):
    r = ask(world, "what is low in fasteners")
    assert "in Fasteners" in r["answer"] and "Paint" not in r["answer"]


def test_how_much_of_an_item_matches_the_stock_analysis_exactly(world):
    r = ask(world, "How much BOLT-M8 do we have?")
    a, _ = world.inventory.analyze_one("BOLT-M8")
    assert r["intent"] == "item_stock" and r["items"] == ["BOLT-M8"]
    assert f"you have {int(a.on_hand)} pcs" in r["answer"] and "Status: Order soon" in r["answer"] and f"{int(a.suggested_qty):,}" in r["answer"]
    kv = dict(r["blocks"][0]["rows"])
    assert kv["In stock"] == "300 pcs" and kv["Status"] == "Order soon" and kv["New stock takes"] == "about 5 days"
    assert any("Selling about" in f["text"] for f in r["facts"]) and all(f["id"].startswith("F") for f in r["facts"])


def test_an_item_with_no_recent_sales_says_so_instead_of_inventing_days_left(world):
    r = ask(world, "stock of DEAD-Z")
    assert "no recent sales" in r["answer"].lower() and "cannot say how long it lasts" in r["answer"]


def test_a_vague_item_name_asks_which_one_and_never_guesses(world):
    r = ask(world, "how much bolt do we have?")
    assert r["status"] == "clarify" and "Bolt M8 x 40 (BOLT-M8)" in r["answer"] and r["follow_ups"] == ["Tell me about BOLT-M8"]
    r = ask(world, "how much do we have of the thing")
    assert r["status"] in ("clarify", "answered", "help")


def test_vendor_comparison_is_the_procurement_rankings_own_result(world):
    r = ask(world, "Compare vendors for BOLT-M8")
    a, _ = world.inventory.analyze_one("BOLT-M8")
    ranking = world.procurement.preview("BOLT-M8", a.suggested_qty, a.cover_days_on_hand, a.lead_time_days, a.uom)
    best = ranking["vendors"][0]
    assert r["intent"] == "vendor_compare" and f"the best vendor is {best['name']}" in r["answer"] and f"{best['total']:,.2f}" in r["answer"]
    assert "suggested reorder size" in r["answer"]
    table = r["blocks"][0]
    assert [row[0].split(" (")[0] for row in table["rows"]] == [v["name"] for v in ranking["vendors"]] and table["rows"][0][-1] == "Recommended"
    for v in ranking["vendors"][1:]:
        assert v["why_not"] in r["answer"]                                                       # why each other vendor was not chosen, in the ranking's own words


def test_a_quantity_in_the_question_is_used_and_a_missing_one_is_asked_for(world):
    r = ask(world, "compare vendors for relay 12v for 600")
    assert "For 600 pcs of Relay 12V" in r["answer"] and "the quantity you asked about" not in r["answer"]
    world.settings.update_procurement(OWNER, {"fallback_qty": None})
    a, _ = world.inventory.analyze_one("DEAD-Z")
    r = ask(world, "compare vendors for DEAD-Z")
    if not a.suggested_qty:
        assert r["status"] == "clarify" and "How many pcs" in r["answer"] and "compare vendors for dead-z for 500" in r["follow_ups"][0].lower()


def test_no_item_for_a_vendor_question_asks_which(world):
    r = ask(world, "which vendor is cheapest")
    assert r["status"] == "clarify" and "Which item do you mean?" in r["answer"]


def test_a_follow_up_uses_the_item_from_before(world):
    first = ask(world, "How much BOLT-M8 do we have?")
    r = ask(world, "and the vendors?", ctx={"items": first["items"]})
    assert r["intent"] == "vendor_compare" and r["answer"].startswith("(Still talking about the same item.)") and "Bolt M8 x 40" in r["answer"]


def test_purchase_documents_are_read_from_the_erp_and_waiting_proposals_from_the_app(world):
    world.workorders.emit("REORDER_NEEDED", {"item_code": "BOLT-M8", "item_name": "Bolt M8 x 40", "item_group": "Fasteners", "uom": "pcs", "suggested_qty": 500.0, "cover_days": 5.0,
                                              "lead_time_days": 5.0, "daily_rate": 40.0, "on_hand": 10.0, "severity": "HIGH", "reasons": ["Low."]})
    world.procurement.run()
    waiting = ask(world, "status of purchase orders for BOLT-M8")
    assert "still waiting in the Stock Assistant" in waiting["answer"] and any("Waiting in the Stock Assistant" in f["text"] for f in waiting["facts"])
    prop_approval = next(r for r in world.approvals.list("pending") if r.kind.startswith("procurement"))
    world.approvals.decide(prop_approval.id, "approve", APPROVER, "whatsapp")
    r = ask(world, "status of purchase orders for BOLT-M8")
    po = world.erp.purchase_orders[0]
    assert po["name"] in r["answer"] and "draft (not submitted)" in r["answer"] and "Draft (not submitted)" in r["blocks"][0]["rows"][0][-1] and "manager via whatsapp" in " ".join(f["text"] for f in r["facts"])
    po["name"] = "PUR-ORD-2026-00001"                                                         # ERPNext's own naming, which is what a person types
    named = ask(world, f"what is the status of {po['name']}")
    assert named["answer"].startswith("I found 1 purchase order") and f"{po['name']} is a purchase order" in named["answer"]
    assert ask(world, "status of PUR-ORD-2026-99999")["answer"] == "I could not find PUR-ORD-2026-99999 in ERPNext."
    assert "found no purchase requisitions" in ask(world, "any requisitions this week")["answer"]


def test_pending_requests_are_shown_only_to_people_who_may_answer_them(world):
    r = ask(world, "what is waiting for my OK?")
    assert r["intent"] == "pending_approvals" and "waiting for your OK" in r["answer"] and r["actions"] == [{"label": "Open Needs your OK", "view": "ok"}]
    assert r["blocks"][0]["rows"] and "6 reorders" in r["answer"]
    blind = ask(world, "what is waiting for my OK?", decide=False)
    assert "Only people who can approve orders" in blind["answer"] and not blind["blocks"]


def test_an_unrelated_question_gets_the_help_text_with_a_real_example(world):
    r = ask(world, "tell me a joke")
    assert r["status"] == "help" and "I only read information" in r["answer"] and "STEEL-2MM" in r["answer"]


def test_a_failure_reading_the_erp_is_reported_and_changes_nothing(world):
    world.chat.erp = type("Down", (), {"list_items": lambda self: (_ for _ in ()).throw(ConnectionError("ERPNext is down"))})()
    world.chat._items_cache = (0.0, [])
    r = ask(world, "How much BOLT-M8 do we have?")
    assert r["status"] == "error" and "could not read the data" in r["answer"] and "Nothing was changed" in r["answer"] and "Traceback" not in r["answer"]


def test_too_many_questions_in_a_minute_are_refused(world):
    for _ in range(20):
        ask(world, "hello")
    with pytest.raises(ChatLimit):
        ask(world, "hello")
    assert world.chat.ask.__self__ is world.chat
    other = world.chat.ask("hello", "someone-else", True)                                         # another person is not affected
    assert other["status"] == "help"


def test_every_question_is_logged_for_audit(world):
    ask(world, "What is running low?")
    ask(world, "tell me a joke")
    rows = world.stock_stores.assistant.chats(10)
    assert [(r["intent"], r["status"], r["source"]) for r in rows] == [("stock_overview", "answered", "rules"), ("help", "help", "rules")] and rows[0]["asked_by"] == "tester"


def test_the_chat_cannot_change_anything(world):
    before = (len(world.approvals.list(None, 100)), len(world.workorders.list()), len(world.erp.purchase_orders), len(world.alerts.list(None, None, 100)))
    for q in ("What is running low?", "How much BOLT-M8 do we have?", "Compare vendors for BOLT-M8", "approve everything", "create a purchase order for BOLT-M8", "status of purchase orders"):
        ask(world, q)
    assert before == (len(world.approvals.list(None, 100)), len(world.workorders.list()), len(world.erp.purchase_orders), len(world.alerts.list(None, None, 100)))
    r = ask(world, "create a purchase order for BOLT-M8")                    # a suggestion for this item is already waiting: it is shown again, never duplicated
    assert r["intent"] == "order_request" and "already waiting" in r["answer"]
    assert before == (len(world.approvals.list(None, 100)), len(world.workorders.list()), len(world.erp.purchase_orders), len(world.alerts.list(None, None, 100)))


# ------------------------------------------------------------------ "create a purchase order for X": prepares the request, never orders
@pytest.mark.parametrize("q", ["create a purchase order for FAS-0003", "Create Purchase orders for FAS-0003", "raise a PO for FAS-0003 for 500", "can you create a purchase order for copper rod 10mm?",
                               "please order 500 of FAS-0003", "reorder FAS-0003", "buy 20 kg of copper rod 10mm", "I want to create a purchase requisition for MET-0005", "new purchase order for FAS-0004", "place an order for FAS-0003"])
def test_an_instruction_to_order_is_recognised(q):
    assert understand(q, ITEMS).intent == "order_request"


@pytest.mark.parametrize("q", ["status of purchase orders", "which purchase orders were created this week", "any PO overdue?", "how do I create a purchase order", "what is the status of orders",
                               "order status of PUR-ORD-2026-00543", "purchase order created yesterday", "compare vendors for FAS-0003 for 500", "what is waiting for my OK", "how much FAS-0003 do we have?",
                               "orders pending for FAS-0003", "approve everything", "delete the purchase order"])
def test_questions_about_orders_are_still_questions(q):
    assert understand(q, ITEMS).intent != "order_request"


@pytest.fixture
def fresh(make_app, clock):
    return make_app(erp=MockERP(today=clock().date()))                             # nothing analysed yet, so nothing is waiting for BOLT-M8


def waiting_requests(app):
    return [r for r in app.approvals.list("pending", 100) if r.kind in ("procurement.po", "procurement.pr")]


def test_asking_for_an_order_prepares_the_usual_request_and_orders_nothing(fresh):
    r = ask(fresh, "create a purchase order for BOLT-M8 for 800")
    assert r["intent"] == "order_request" and r["status"] == "answered" and "Nothing is ordered until you press Create order" in r["answer"] and "800 pcs" in r["answer"]
    (rec,) = waiting_requests(fresh)
    assert rec.request.facts["qty"] == 800 and rec.request.facts["item_code"] == "BOLT-M8"
    assert {"approve", "change_qty", "change_vendor"} <= {o.action for o in rec.request.options} and "Prime Supplies" in r["answer"] + str(rec.request.facts)
    assert r["actions"] == [{"label": "Open the request", "view": "ok", "approval_id": rec.id}]
    assert not fresh.erp.purchase_orders                                              # nothing exists in the ERP until Create order is pressed
    assert "asked for a purchase order" in str([w["payload"]["reasons"] for w in fresh.workorders.list()])


def _strip_change_buttons(app, rec):
    """What a request made before Change quantity / Change vendor existed looks like."""
    def strip(row):
        req = json.loads(row["request_json"])
        req["options"] = [o for o in req["options"] if o["action"] not in ("change_qty", "change_vendor")]
        return {"request_json": json.dumps(req)}
    tamper(app, "approvals", {"id": rec.id}, strip)


def test_a_waiting_request_from_an_older_version_is_rebuilt_with_the_new_buttons(fresh):
    ask(fresh, "create a purchase order for BOLT-M8 for 800")
    (old,) = waiting_requests(fresh)
    _strip_change_buttons(fresh, old)
    r = ask(fresh, "create purchase orders for BOLT-M8")
    (new,) = waiting_requests(fresh)
    assert new.id != old.id and fresh.approvals.get(old.id).status == "cancelled" and {"change_qty", "change_vendor"} <= {o.action for o in new.request.options}
    assert "could not be used as it was" in r["answer"] and "Change quantity and Change vendor buttons" in r["answer"] and new.request.facts["qty"] == 800
    assert r["actions"] == [{"label": "Open the request", "view": "ok", "approval_id": new.id}] and not fresh.erp.purchase_orders


def test_a_waiting_request_below_the_minimum_order_quantity_is_rebuilt_at_the_minimum(fresh):
    ask(fresh, "create a purchase order for BOLT-M8 for 100")
    (old,) = waiting_requests(fresh)
    fresh.erp.min_order_qty = {"BOLT-M8": 400}
    r = ask(fresh, "create a purchase order for BOLT-M8")
    (new,) = waiting_requests(fresh)
    assert new.id != old.id and new.request.facts["qty"] == 400 and "raised from 100 to 400 pcs" in r["answer"] and "minimum order quantity of 400" in r["answer"]
    assert fresh.proposals.by_approval(new.id).qty == 400 and not fresh.erp.purchase_orders


def test_a_good_waiting_request_is_left_alone(fresh):
    ask(fresh, "create a purchase order for BOLT-M8 for 800")
    (first,) = waiting_requests(fresh)
    ask(fresh, "create a purchase order for BOLT-M8")
    (again,) = waiting_requests(fresh)
    assert again.id == first.id


def test_asking_for_an_order_without_a_quantity_never_prepares_less_than_the_minimum(fresh):
    fresh.erp.min_order_qty = {"BOLT-M8": 100000}
    ask(fresh, "create a purchase order for BOLT-M8")
    (rec,) = waiting_requests(fresh)
    assert rec.request.facts["qty"] == 100000


def test_asking_again_shows_the_waiting_request_instead_of_making_a_second(fresh):
    ask(fresh, "create a purchase order for BOLT-M8 for 800")
    r = ask(fresh, "raise a PO for BOLT-M8 for 900")
    assert "already waiting" in r["answer"] and r["actions"][0]["resend"] == "1" and "You asked for 900 pcs: press Change quantity" in r["answer"]
    assert len(waiting_requests(fresh)) == 1 and waiting_requests(fresh)[0].request.facts["qty"] == 800


def test_the_requested_quantity_is_used_and_without_one_the_analysis_suggests_one(fresh):
    ask(fresh, "buy BOLT-M8")
    a, _ = fresh.inventory.analyze_one("BOLT-M8")
    (rec,) = waiting_requests(fresh)
    assert rec.request.facts["qty"] == a.suggested_qty


def test_pressing_create_order_after_asking_makes_one_draft_for_that_quantity(fresh):
    ask(fresh, "create a purchase order for BOLT-M8 for 800")
    (rec,) = waiting_requests(fresh)
    fresh.approvals.decide(rec.id, "approve", APPROVER, "console")
    (po,) = fresh.erp.purchase_orders
    assert po["qty"] == 800 and po["item_code"] == "BOLT-M8" and po["approved_by"]


def test_only_people_who_can_approve_may_ask_for_an_order(fresh):
    r = ask(fresh, "create a purchase order for BOLT-M8 for 800", decide=False)
    assert "Only people who can approve" in r["answer"] and not waiting_requests(fresh) and not fresh.workorders.list()


@pytest.mark.parametrize("q,words", [("create a purchase order", "Which item"), ("create a purchase order for hex bolt", "which item")])
def test_an_order_for_no_item_or_an_unclear_one_asks_which(fresh, q, words):
    fresh.erp.list_items = lambda: ITEMS                                              # two bolts fit "hex bolt" equally
    r = ask(fresh, q)
    assert words.lower() in r["answer"].lower() and r["status"] == "clarify" and not waiting_requests(fresh)


@pytest.mark.parametrize("qty", ["0", "-5", "5000000000"])
def test_a_silly_quantity_is_refused_before_anything_is_prepared(fresh, qty):
    r = ask(fresh, f"create a purchase order for BOLT-M8 for {qty}")
    assert not waiting_requests(fresh) and not fresh.workorders.list() and r["intent"] == "order_request" and r["status"] == "clarify" and "quantity" in r["answer"]


def test_the_explain_helper_never_prepares_an_order(fresh):
    r = fresh.chat.explain("create a purchase order for BOLT-M8 for 800", "tester", True) if hasattr(fresh.chat, "explain") else {"answer": ""}
    assert not waiting_requests(fresh)


# ------------------------------------------------------------------ the AI helper (optional, and never trusted)
@pytest.fixture
def ai(world):
    world.settings.update_section(OWNER, "assistant", {"enabled": True})
    llm = FakeLLM()
    world.chat.client_factory = lambda cfg: llm
    return world, llm


def test_the_ai_helper_is_not_asked_when_the_rules_understand_the_question(ai):
    app, llm = ai
    ask(app, "What is running low?")
    assert llm.calls == []


def test_the_ai_helper_may_only_pick_a_question_type_when_the_rules_cannot(ai):
    app, llm = ai
    llm.replies += [{"intent": "stock_overview"}, {"intent": "make_coffee"}, "not json at all"]
    r = ask(app, "what should I be worried about today")
    assert r["intent"] == "stock_overview" and r["routed_by_ai"] is True and "System prompt" not in r["answer"]
    assert llm.calls[0][2]["properties"]["intent"]["enum"][-1] == "unknown"                    # a closed list: no free text
    assert ask(app, "blah blah one")["status"] == "help"                                          # an invented type is ignored
    assert ask(app, "blah blah two")["status"] == "help"                                          # an unparseable reply is ignored


def test_without_the_ai_helper_the_chat_still_works_fully(world):
    assert world.settings.assistant().enabled is False
    assert ask(world, "give me the lowdown")["status"] == "help" and ask(world, "How much BOLT-M8 do we have?")["status"] == "answered"


def test_an_explanation_is_shown_only_if_it_passes_the_same_checks_as_ask_why(ai):
    app, llm = ai
    q = "How much BOLT-M8 do we have?"
    facts = app.chat.ask(q, "tester", True)["facts"]
    on_hand = next(f["id"] for f in facts if "300 pcs on hand" in f["text"])
    llm.replies.append({"answerable": True, "answer": f"You have 300 pcs of Bolt M8 x 40 ({on_hand}).", "facts_used": [on_hand]})
    good = app.chat.explain(q, "tester", True)
    assert good["status"] == "answered" and "300 pcs" in good["answer"] and good["facts"][0]["id"] == on_hand
    llm.replies.append({"answerable": True, "answer": "You have 9,999 pcs.", "facts_used": [on_hand]})
    bad = app.chat.explain("How much BOLT-M8 do we have today?", "tester", True)
    assert bad["status"] == "rejected" and bad["answer"] == NOT_ENOUGH                           # an invented number is never shown
    llm.replies.append({"answerable": True, "answer": "You probably have enough.", "facts_used": [on_hand]})
    assert app.chat.explain("Do we have enough BOLT-M8?", "tester", True)["answer"] == NOT_ENOUGH


def test_an_explanation_is_repeatable_and_off_when_the_helper_is_off(ai, world):
    app, llm = ai
    q = "How much BOLT-M8 do we have?"
    fid = next(f["id"] for f in app.chat.ask(q, "t", True)["facts"] if "300 pcs on hand" in f["text"])
    llm.replies.append({"answerable": True, "answer": f"300 pcs ({fid}).", "facts_used": [fid]})
    first = app.chat.explain(q, "t", True)
    second = app.chat.explain(q, "t", True)
    assert second["cached"] is True and second["answer"] == first["answer"] and len(llm.calls) == 1
    app.settings.update_section(OWNER, "assistant", {"enabled": False})
    assert app.chat.explain(q, "t", True)["status"] == "disabled"


# ------------------------------------------------------------------ through the API
@pytest.fixture
def api(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()), cfg={"auth": {"tokens": [
        {"token": "o" * 40, "user": "owner", "roles": ["viewer", "approver", "admin"]}, {"token": "v" * 40, "user": "viewer", "roles": ["viewer"]}]}})
    app.inventory.run()
    return app, TestClient(create_app(app))


def hdr(t):
    return {"Authorization": "Bearer " + t * 40}


def test_the_chat_endpoint_needs_a_sign_in_but_not_approval_rights(api):
    app, c = api
    assert c.post("/api/v1/chat", json={"question": "What is running low?"}).status_code == 401
    r = c.post("/api/v1/chat", json={"question": "What is running low?"}, headers=hdr("v"))
    assert r.status_code == 200 and r.json()["intent"] == "stock_overview"
    p = c.post("/api/v1/chat", json={"question": "what is waiting for my OK"}, headers=hdr("v")).json()
    assert "Only people who can approve" in p["answer"]                                           # a viewer cannot see the inbox through the chat
    assert "waiting for your OK" in c.post("/api/v1/chat", json={"question": "what is waiting for my OK"}, headers=hdr("o")).json()["answer"]


def test_the_chat_endpoint_validates_and_throttles(api):
    app, c = api
    assert c.post("/api/v1/chat", json={"question": ""}, headers=hdr("o")).status_code == 422
    assert c.post("/api/v1/chat", json={"question": "x" * 301}, headers=hdr("o")).status_code == 422
    assert c.post("/api/v1/chat", json={"question": "hi", "context": {"items": ["a"] * 6}}, headers=hdr("o")).status_code == 422
    for _ in range(20):
        c.post("/api/v1/chat", json={"question": "hi"}, headers=hdr("o"))
    r = c.post("/api/v1/chat", json={"question": "hi"}, headers=hdr("o"))
    assert r.status_code == 429


def test_the_context_from_the_screen_is_checked_against_real_items(api):
    app, c = api
    r = c.post("/api/v1/chat", json={"question": "and the vendors?", "context": {"items": ["BOLT-M8"]}}, headers=hdr("o")).json()
    assert r["intent"] == "vendor_compare" and r["items"] == ["BOLT-M8"]
    r = c.post("/api/v1/chat", json={"question": "and the vendors?", "context": {"items": ["<script>alert(1)</script>"]}}, headers=hdr("o")).json()
    assert r["status"] == "clarify" and "<script>" not in r["answer"]


# ------------------------------------------------------------------ reading documents from ERPNext
class Resp:
    def __init__(self, data=None, status=200):
        self._d, self.status_code, self.text = data if data is not None else {}, status, json.dumps(data or {})

    def json(self):
        return self._d


class DocSession:
    """ERPNext's Purchase Order and Material Request lists, as the REST API answers them."""

    def __init__(self, custom_field=True):
        self.headers, self.calls, self.custom_field = {}, [], custom_field
        self.po = [{"name": "PUR-ORD-2026-00543", "status": "To Receive and Bill", "docstatus": 1, "owner": "priya@acme.com", "creation": "2026-10-10 10:00:00", "supplier_name": "Ludhiana Hardware",
                    "transaction_date": "2026-10-10", "schedule_date": "2026-10-17", "grand_total": 82225.0, "currency": "INR", "per_received": 40.0, "per_billed": 0.0, "custom_agent_approved_by": "priya@acme.com via app"},
                   {"name": "PUR-ORD-2026-00544", "status": "Draft", "docstatus": 0, "owner": "agent@x.com", "creation": "2026-10-12 10:00:00", "supplier_name": "Ningbo", "transaction_date": "2026-10-12",
                    "schedule_date": "2026-11-20", "grand_total": 104186.0, "currency": "INR", "per_received": 0.0, "per_billed": 0.0, "custom_agent_approved_by": "owner via whatsapp"}]
        self.lines = {"PUR-ORD-2026-00543": [{"parent": "PUR-ORD-2026-00543", "item_code": "FAS-0003", "item_name": "Hex Bolt SS304 M10x50", "qty": 5500, "uom": "Nos", "received_qty": 2200}],
                      "PUR-ORD-2026-00544": [{"parent": "PUR-ORD-2026-00544", "item_code": "FAS-0004", "item_name": "Hex Bolt SS304 M12x70", "qty": 1000, "uom": "Nos", "received_qty": 0}]}

    def get(self, url, params=None, timeout=None):
        doctype = url.rsplit("/api/resource/", 1)[1]
        flt = json.loads(params["filters"])
        fields = json.loads(params["fields"])
        self.calls.append((doctype, fields, flt))
        if doctype == "Purchase Order Item":
            wanted_item = next((f[2] for f in flt if f[0] == "item_code"), None)
            parents = next((f[2] for f in flt if f[0] == "parent"), None)
            rows = [l for ls in self.lines.values() for l in ls if (not wanted_item or l["item_code"] == wanted_item) and (not parents or l["parent"] in parents)]
            return Resp({"data": rows})
        if doctype == "Purchase Order":
            if "custom_agent_approved_by" in fields and not self.custom_field:
                return Resp({"exception": "Field not permitted in query: custom_agent_approved_by"}, 417)
            names = next((f[2] for f in flt if f[0] == "name" and f[1] == "in"), None)
            one = next((f[2] for f in flt if f[0] == "name" and f[1] == "="), None)
            rows = [r for r in self.po if (not names or r["name"] in names) and (not one or r["name"] == one)]
            return Resp({"data": [{k: v for k, v in r.items() if k in fields} for r in rows]})
        return Resp({"data": []})


def erp_with(session):
    return ERPNextClient("http://erp.local", "K", "S", session=session)


def test_purchase_orders_are_read_with_their_lines_state_and_who_created_and_approved_them():
    docs = erp_with(DocSession()).get_documents("Purchase Order")
    first = docs[0] if docs[0]["name"] == "PUR-ORD-2026-00543" else docs[1]
    assert first["status"] == "To Receive and Bill" and first["docstatus"] == 1 and first["party"] == "Ludhiana Hardware" and first["per_received"] == 40.0
    assert first["created_by"] == "priya@acme.com" and first["approved_by"] == "priya@acme.com via app" and first["due"] == "2026-10-17"
    assert first["lines"] == [{"item_code": "FAS-0003", "item_name": "Hex Bolt SS304 M10x50", "qty": 5500.0, "uom": "Nos", "done_qty": 2200.0}]


def test_filtering_by_item_name_and_date_goes_to_the_erp_and_a_missing_custom_field_is_tolerated():
    s = DocSession(custom_field=False)
    c = erp_with(s)
    got = c.get_documents("Purchase Order", item_code="FAS-0004")
    assert [d["name"] for d in got] == ["PUR-ORD-2026-00544"] and got[0]["approved_by"] == ""            # the field exists only after erp-agents erp-setup
    assert c.get_documents("Purchase Order", name="PUR-ORD-2026-00543", limit=1)[0]["name"] == "PUR-ORD-2026-00543"
    c.get_documents("Purchase Order", since=date(2026, 10, 1))
    assert any(["transaction_date", ">=", "2026-10-01"] in f for _d, _fields, f in s.calls)
    assert c.get_documents("Purchase Order", item_code="NO-SUCH") == []
    with pytest.raises(Exception):
        c.get_documents("Sales Order")


# ------------------------------------------------------------------ "open POs for a material, with the quantity and the vendor"
class _Docs:
    """A stand-in for the ERP's document reading: purchase orders with lines, as ERPNext returns them. Remembers how it was asked."""

    def __init__(self, docs):
        self.docs, self.asked = docs, []

    def __call__(self, doctype, name=None, item_code=None, since=None, limit=20, open_only=False):
        self.asked.append({"doctype": doctype, "item_code": item_code, "since": since, "limit": limit, "open_only": open_only})
        return [d for d in self.docs if d["doctype"] == doctype and (not item_code or any(l["item_code"] == item_code for l in d["lines"]))][:limit]


def _po(name, vendor, lines, status="To Receive and Bill", per_received=0.0, due="2026-11-04", docstatus=1):
    return {"doctype": "Purchase Order", "name": name, "status": status, "docstatus": docstatus, "party": vendor, "date": "2026-09-01", "due": due, "created_by": "service", "approved_by": "",
            "per_received": per_received, "per_billed": 0.0, "total": None, "currency": None, "kind": None,
            "lines": [{"item_code": c, "item_name": n, "qty": q, "uom": "Kg", "done_qty": r} for c, n, q, r in lines]}


def test_open_purchase_orders_for_a_material_show_the_vendor_and_the_quantities(world):
    docs = _Docs([_po("PUR-ORD-1", "Busan Steel", [("MET-0025", "Copper Rod 20mm", 1250, 0)], due="2026-12-05"),
                  _po("PUR-ORD-2", "Mumbai Metal Traders", [("FAS-1", "Washer", 90, 0), ("MET-0025", "Copper Rod 20mm", 400, 100)], per_received=25.0),
                  _po("PUR-ORD-3", "Old Vendor", [("MET-0025", "Copper Rod 20mm", 700, 700)], status="Completed", per_received=100.0)])
    world.erp.get_documents = docs
    world.erp.list_items = lambda: [ItemInfo("MET-0025", "Copper Rod 20mm", "Metals", "Kg")] + ITEMS
    r = ask(world, "show me only open POs for material MET-0025 with order quantity and vendor")
    table = r["blocks"][0]
    assert table["columns"] == ["Document", "Vendor", "Ordered", "Received", "Still to come", "Due", "State"]
    assert table["rows"] == [["PUR-ORD-1", "Busan Steel", "1,250 Kg", "0", "1,250", "2026-12-05", "To Receive and Bill"],
                             ["PUR-ORD-2", "Mumbai Metal Traders", "400 Kg", "100", "300", "2026-11-04", "To Receive and Bill"]]       # only that material's line, not the washer; the completed one is not open
    assert "I found 2 purchase orders for Copper Rod 20mm" in r["answer"] and "Together they are for 1,650 Kg, of which 1,550 Kg is still to come." in r["answer"]
    assert any("1,250 Kg Copper Rod 20mm" in f["text"] and "Busan Steel" in f["text"] for f in r["facts"])


def test_the_phone_text_of_that_answer_carries_the_quantity_and_the_vendor(world):
    from erp_agents.packs.stock_procurement.assistant.conversation import render
    world.erp.get_documents = _Docs([_po("PUR-ORD-1", "Busan Steel", [("MET-0025", "Copper Rod 20mm", 1250, 0)])])
    world.erp.list_items = lambda: [ItemInfo("MET-0025", "Copper Rod 20mm", "Metals", "Kg")] + ITEMS
    text = render(ask(world, "show me only open POs for material MET-0025 with order quantity and vendor"))
    assert "Document | Vendor | Ordered | Received | Still to come | Due | State" in text and "- PUR-ORD-1 | Busan Steel | 1,250 Kg | 0 | 1,250 | 2026-11-04 | To Receive and Bill" in text
    assert "1,250 Kg" in text.splitlines()[0]                                                                  # one order: the sentence says it too


def test_an_open_order_is_shown_whatever_its_age_unless_a_period_is_named(world):
    docs = _Docs([])
    world.erp.get_documents = docs
    ask(world, "show me only open POs for FAS-0003")
    assert docs.asked[0]["since"] is None and docs.asked[0]["limit"] == 100                                    # the status is applied after reading, so read enough
    docs.asked.clear()
    ask(world, "which POs are open this week")
    assert docs.asked[0]["since"] is not None
    docs.asked.clear()
    ask(world, "purchase orders for FAS-0003")
    assert docs.asked[0]["since"] is not None and docs.asked[0]["limit"] == 20                                  # a plain list is still the recent ones


def test_a_question_about_orders_without_a_material_shows_what_each_is_for(world):
    world.erp.get_documents = _Docs([_po("PUR-ORD-1", "Busan Steel", [("MET-0025", "Copper Rod 20mm", 1250, 0), ("FAS-1", "Washer", 90, 0)])])
    r = ask(world, "status of our open purchase orders")
    assert r["blocks"][0]["columns"][:4] == ["Document", "Vendor or type", "Quantity", "State"]
    assert r["blocks"][0]["rows"][0][2] == "1,250 Kg Copper Rod 20mm +1 more"
