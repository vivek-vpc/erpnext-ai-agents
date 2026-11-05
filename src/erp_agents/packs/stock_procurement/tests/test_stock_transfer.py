"""Moving stock between warehouses: the request a person answers, the draft Stock Entry it makes, and the chat, the tool and the REST route that start it.
Nothing is created in the ERP until a person presses Create transfer, and even then it is only a draft."""
import pytest
from fastapi.testclient import TestClient

from conftest import APPROVER, OWNER, VIEWER
from erp_agents.core.api.app import create_app
from erp_agents.core.erp.mock import MockERP
from erp_agents.packs.stock_procurement.assistant.chat import find_warehouses, understand, wants_transfer

KNOWN = ["Main Warehouse", "Shop floor", "Stores - TTC", "Finished Goods - TTC", "Stores - ACD"]


@pytest.fixture
def world(make_app, clock):
    erp = MockERP(today=clock().date(), scenarios=False)
    erp.add_item("BOLT", "Bolt", lambda age: 10, 0, group="Fasteners", uom="pcs", warehouses={"Stores": {"on_hand": 100, "reserved": 60}, "Shop floor": {"on_hand": 40}})
    erp.add_item("NUT", "Nut", lambda age: 10, 500, group="Other")
    return make_app(erp=erp), erp


def request_of(app, res):
    return app.approvals.get(res["approval_id"])


def test_preparing_a_transfer_sends_a_request_and_creates_nothing(world):
    app, erp = world
    res = app.transfers.prepare("BOLT", 30, "Stores", "Shop floor", "anna", "The shop floor is running short.")
    assert res["status"] == "created" and (res["from_warehouse"], res["to_warehouse"], res["qty"]) == ("Stores", "Shop floor", 30)
    rec = request_of(app, res)
    assert rec.status == "pending" and rec.kind == "inventory.transfer" and rec.request.facts["qty"] == 30 and rec.request.facts["item_code"] == "BOLT"
    assert [o.label for o in rec.request.options] == ["Create transfer", "Don't move it", "Details"]
    text = " ".join(l for s in rec.request.sections for l in s.lines)
    assert "Why" in [s.heading for s in rec.request.sections] and "The shop floor is running short." in text
    assert "Stores: 100 pcs (60 promised to customers)" in text and "Shop floor: 40 pcs" in text
    assert "Stores would hold 70 pcs and Shop floor 70 pcs" in text and "only a draft" in text
    assert erp.stock_transfers == [] and app.transfers.get(res["transfer"])["status"] == "proposed"      # nothing in the ERP, and no stock moved


def test_taking_more_than_the_promised_stock_is_said_not_blocked(world):
    app, _ = world
    rec = request_of(app, app.transfers.prepare("BOLT", 50, "Stores", "Shop floor", "anna"))
    assert "Careful: Stores would then hold less than the 60 pcs promised to customers" in " ".join(l for s in rec.request.sections for l in s.lines)
    all_of_it = request_of(app, app.transfers.prepare("BOLT", 40, "Shop floor", "Stores", "anna"))
    assert "That is everything Shop floor has of it." in " ".join(l for s in all_of_it.request.sections for l in s.lines)


@pytest.mark.parametrize("args,why", [
    (("NOPE", 5, "Stores", "Shop floor"), "no item NOPE"),
    (("BOLT", 0, "Stores", "Shop floor"), "more than zero"),
    (("BOLT", -3, "Stores", "Shop floor"), "more than zero"),
    (("BOLT", float("nan"), "Stores", "Shop floor"), "more than zero"),
    (("BOLT", 5, "Stores", "Stores"), "the two warehouses are the same"),
    (("BOLT", 5, "Stor", "Shop floor"), "the warehouse to take it from: no warehouse 'Stor'; did you mean Stores"),
    (("BOLT", 5, "Stores", "Nowhere"), "the warehouse to move it to: no warehouse 'Nowhere'"),
    (("BOLT", 150, "Stores", "Shop floor"), "Stores holds only 100 pcs of BOLT, so 150 cannot be moved from it"),
    (("BOLT", 5, "Main Warehouse", "Shop floor"), "Main Warehouse holds none of BOLT"),
])
def test_a_transfer_that_cannot_work_is_refused_with_the_reason_and_nothing_is_sent(world, args, why):
    app, erp = world
    res = app.transfers.prepare(*args, "anna")
    assert res["status"] == "refused" and why in res["error"]
    assert app.approvals.list("pending", 50) == [] and erp.stock_transfers == [] and app.transfers.list() == []


def test_a_warehouse_is_found_by_its_name_or_without_its_company_when_that_is_unique():
    from erp_agents.packs.stock_procurement.inventory.transfers import StockTransfers
    find = lambda text: StockTransfers.warehouse(None, text, KNOWN)             # the lookup uses nothing of the service itself
    assert find("Stores - TTC") == ("Stores - TTC", "") and find("stores - ttc")[0] == "Stores - TTC" and find("Finished Goods")[0] == "Finished Goods - TTC"
    assert find("Stores")[0] is None and "Stores - TTC" in find("Stores")[1] and "Stores - ACD" in find("Stores")[1]       # two companies have a Stores: ask, do not pick
    assert find("")[0] is None and find("   ")[1] == "no warehouse was given"


def test_a_second_request_for_the_same_move_is_not_made(world):
    app, _ = world
    first = app.transfers.prepare("BOLT", 30, "Stores", "Shop floor", "anna")
    again = app.transfers.prepare("BOLT", 10, "stores", "SHOP FLOOR", "ben")
    assert again["status"] == "waiting" and again["approval_id"] == first["approval_id"] and len(app.approvals.list("pending", 50)) == 1
    other_way = app.transfers.prepare("BOLT", 10, "Shop floor", "Stores", "ben")
    assert other_way["status"] == "created"                                       # the opposite move is a different request


def test_creating_the_transfer_makes_a_draft_with_the_approver_and_moves_no_stock(world):
    app, erp = world
    res = app.transfers.prepare("BOLT", 30, "Stores", "Shop floor", "anna", "short on the floor")
    done = app.approvals.decide(res["approval_id"], "approve", OWNER, "app")
    assert done.ok and done.status == "decided"
    (draft,) = erp.stock_transfers
    assert (draft["item_code"], draft["qty"], draft["from_warehouse"], draft["to_warehouse"], draft["docstatus"]) == ("BOLT", 30, "Stores", "Shop floor", 0)
    assert draft["reference"] == res["transfer"] and draft["approved_by"] == "owner via app" and "Requested by anna" in draft["remarks"] and "short on the floor" in draft["remarks"]
    row = app.transfers.get(res["transfer"])
    assert row["status"] == "created" and row["entry_name"] == draft["name"] and "owner via app" in row["status_note"]
    assert erp.get_stock(["BOLT"])["BOLT"].on_hand == 140                          # a draft: the stock is where it was
    assert draft["name"] in app.transfers._outcome_detail(app.approvals.get(res["approval_id"])) and "only a draft" in app.transfers._outcome_detail(app.approvals.get(res["approval_id"]))


def test_after_it_is_submitted_in_the_erp_the_stock_shows_in_the_new_place(world):
    app, erp = world
    res = app.transfers.prepare("BOLT", 30, "Stores", "Shop floor", "anna")
    app.approvals.decide(res["approval_id"], "approve", OWNER, "app")
    erp.submit_stock_transfer(erp.stock_transfers[0]["name"])
    a, _src = app.inventory.analyze_one("BOLT")
    assert [(w["warehouse"], w["on_hand"]) for w in a.warehouses] == [("Shop floor", 70), ("Stores", 70)] and a.on_hand == 140        # the total does not change


def test_saying_no_creates_nothing_and_closes_the_request(world):
    app, erp = world
    res = app.transfers.prepare("BOLT", 30, "Stores", "Shop floor", "anna")
    done = app.approvals.decide(res["approval_id"], "dismiss", OWNER, "app")
    assert done.ok and erp.stock_transfers == [] and app.transfers.get(res["transfer"])["status"] == "rejected"
    assert app.transfers.prepare("BOLT", 30, "Stores", "Shop floor", "anna")["status"] == "created"       # asking again later is fine


def test_when_the_erp_refuses_the_decision_is_rolled_back_so_the_button_can_be_pressed_again(world):
    app, erp = world
    res = app.transfers.prepare("BOLT", 30, "Stores", "Shop floor", "anna")
    erp.fail_purchase_orders = True
    failed = app.approvals.decide(res["approval_id"], "approve", OWNER, "app")
    assert not failed.ok and "did not accept the stock transfer" in failed.message
    assert app.approvals.get(res["approval_id"]).status == "pending" and app.transfers.get(res["transfer"])["status"] == "proposed" and erp.stock_transfers == []
    erp.fail_purchase_orders = False
    assert app.approvals.decide(res["approval_id"], "approve", OWNER, "app").ok and len(erp.stock_transfers) == 1


def test_a_retry_never_creates_a_second_draft(world):
    app, erp = world
    res = app.transfers.prepare("BOLT", 30, "Stores", "Shop floor", "anna")
    app.approvals.decide(res["approval_id"], "approve", OWNER, "app")
    again = erp.create_stock_transfer(__import__("ai_munimji_engine.packs.stock_procurement.erp", fromlist=["StockTransferDraft"]).StockTransferDraft(
        "BOLT", 30, "pcs", "Stores", "Shop floor", reference=res["transfer"]))
    assert again.ok and again.name == erp.stock_transfers[0]["name"] and len(erp.stock_transfers) == 1


def test_an_unanswered_or_cancelled_request_closes_the_transfer(world, clock):
    app, erp = world
    one = app.transfers.prepare("BOLT", 30, "Stores", "Shop floor", "anna")
    app.approvals.cancel(one["approval_id"], "no longer needed")
    assert app.transfers.get(one["transfer"])["status"] == "cancelled"
    two = app.transfers.prepare("BOLT", 10, "Stores", "Shop floor", "anna")
    clock.advance(days=30)
    app.approvals.expire_due()
    assert app.transfers.get(two["transfer"])["status"] == "expired" and erp.stock_transfers == []


def test_when_only_the_personal_login_may_create_documents_a_whatsapp_answer_is_refused(world):
    app, erp = world
    app.settings.update_procurement(OWNER, {"require_personal_login": True}, [])
    res = app.transfers.prepare("BOLT", 30, "Stores", "Shop floor", "anna")
    out = app.approvals.decide(res["approval_id"], "approve", OWNER, "whatsapp")
    assert not out.ok and "approve it in the console" in out.message and erp.stock_transfers == [] and app.approvals.get(res["approval_id"]).status == "pending"


def test_an_erp_connection_that_cannot_create_transfers_says_so(world):
    app, erp = world
    class Limited:
        def __getattr__(self, name):
            if name == "create_stock_transfer":
                raise AttributeError(name)
            return getattr(erp, name)
    app.transfers.app.erp = Limited()
    res = app.transfers.prepare("BOLT", 30, "Stores", "Shop floor", "anna")
    assert res["status"] == "refused" and "cannot create stock transfers" in res["error"]


# ------------------------------------------------------------------ the tool
def test_the_tool_prepares_the_request_for_an_approver_only(world):
    app, erp = world
    args = {"item_code": "BOLT", "qty": 30, "from_warehouse": "Stores", "to_warehouse": "Shop floor", "reason": "asked by the shop"}
    assert "not allowed" in app.toolkit.call("prepare_stock_transfer", args, VIEWER)["error"] and app.approvals.list("pending", 50) == []
    r = app.toolkit.call("prepare_stock_transfer", args, APPROVER)
    assert r["status"] == "created" and r["qty"] == 30 and (r["from_warehouse"], r["to_warehouse"]) == ("Stores", "Shop floor") and "Nothing is created in ERPNext" in r["note"]
    assert app.approvals.get(r["approval_id"]).status == "pending" and erp.stock_transfers == []
    again = app.toolkit.call("prepare_stock_transfer", {**args, "qty": 10}, APPROVER)
    assert again["status"] == "waiting" and again["qty"] == 30 and "It is for 30 pcs" in again["note"]           # says the waiting request is for a different quantity
    bad = app.toolkit.call("prepare_stock_transfer", {**args, "qty": 5000}, APPROVER)
    assert bad["status"] == "refused" and "holds only 100" in bad["error"] and bad["note"] == "nothing was prepared"      # an impossible quantity is refused, not "already waiting"
    assert "did_you_mean" in app.toolkit.call("prepare_stock_transfer", {**args, "item_code": "BOLTT"}, APPROVER)
    waiting = app.toolkit.call("waiting_requests", {"item_code": "BOLT"}, VIEWER)
    assert waiting["requests"][0]["type"] == "stock transfer request" and waiting["requests"][0]["qty"] == 30


# ------------------------------------------------------------------ the REST route
@pytest.fixture
def api(make_app, clock):
    cfg = {"auth": {"tokens": [{"token": "owner", "user": "owner", "roles": ["admin", "approver"]}, {"token": "view", "user": "acct", "roles": ["viewer"]}]}}
    erp = MockERP(today=clock().date(), scenarios=False)
    erp.add_item("BOLT", "Bolt", lambda age: 10, 0, uom="pcs", warehouses={"Stores": {"on_hand": 100}, "Shop floor": {"on_hand": 40}})
    app = make_app(erp=erp, cfg=cfg)
    return app, erp, TestClient(create_app(app))


def H(t):
    return {"Authorization": f"Bearer {t}"}


def test_the_route_prepares_lists_and_refuses_like_the_tool(api):
    app, erp, c = api
    body = {"item_code": "BOLT", "qty": 25, "from_warehouse": "Stores", "to_warehouse": "Shop floor", "reason": "restock"}
    assert c.post("/api/v1/inventory/transfers", json=body).status_code == 401
    assert c.post("/api/v1/inventory/transfers", json=body, headers=H("view")).status_code == 403
    ok = c.post("/api/v1/inventory/transfers", json=body, headers=H("owner"))
    assert ok.status_code == 200 and ok.json()["status"] == "created" and erp.stock_transfers == []
    assert c.post("/api/v1/inventory/transfers", json={**body, "qty": 5000}, headers=H("owner")).status_code == 422
    assert c.post("/api/v1/inventory/transfers", json={**body, "qty": 0}, headers=H("owner")).status_code == 422
    assert c.post("/api/v1/inventory/transfers", json={**body, "item_code": "NOPE"}, headers=H("owner")).status_code == 404
    rows = c.get("/api/v1/inventory/transfers", headers=H("view")).json()
    assert [(r["item_code"], r["qty"], r["from_warehouse"], r["to_warehouse"], r["status"]) for r in rows] == [("BOLT", 25, "Stores", "Shop floor", "proposed")]
    assert "requested_by" not in rows[0]
    done = c.post(f"/api/v1/approvals/{ok.json()['approval_id']}/decision", json={"action": "approve", "params": {}}, headers=H("owner"))
    assert done.status_code == 200 and len(erp.stock_transfers) == 1
    assert c.get("/api/v1/inventory/transfers", headers=H("view")).json()[0]["entry_name"] == erp.stock_transfers[0]["name"]


# ------------------------------------------------------------------ the chat
def test_the_words_that_start_a_transfer():
    for q in ("move 50 BOLT from Stores to Shop floor", "Transfer 20 of BOLT from stores to shop floor", "please shift BOLT from Stores to Shop floor", "can you relocate 5 BOLT to Stores"):
        assert wants_transfer(" ".join(q.lower().split())), q
    for q in ("what happens if I move BOLT", "why did we move BOLT to Stores", "how much BOLT is in Stores", "status of the transfer", "I will move on"):
        assert not wants_transfer(q.lower()), q


@pytest.mark.parametrize("q,expected", [
    ("move 50 bolt from stores - ttc to finished goods - ttc", ("Stores - TTC", "Finished Goods - TTC", False)),
    ("move 50 bolt from finished goods to shop floor", ("Finished Goods - TTC", "Shop floor", False)),
    ("move 50 bolt to shop floor from finished goods", ("Finished Goods - TTC", "Shop floor", False)),            # the words from / to decide, not the order
    ("move 50 bolt from shop floor to main warehouse", ("Shop floor", "Main Warehouse", False)),
    ("move 50 bolt shop floor main warehouse", ("Shop floor", "Main Warehouse", False)),                          # two names and no words: in the order written
    ("move 50 bolt from stores to shop floor", (None, "Shop floor", True)),                                       # Stores exists in two companies: not guessed
    ("move 50 bolt to shop floor", (None, "Shop floor", True)),
    ("move 50 bolt", (None, None, False)),
])
def test_the_warehouses_in_a_sentence_are_read_by_their_from_and_to_words(q, expected):
    assert find_warehouses(q, KNOWN) == expected


def test_the_plan_for_a_move_reads_the_quantity_and_the_warehouses():
    from ai_munimji_engine.packs.stock_procurement.erp import ItemInfo
    items = [ItemInfo("BOLT", "Bolt", "Fasteners", "pcs")]
    p = understand("Move 1,250 of BOLT from Shop floor to Main Warehouse", items, None, KNOWN)
    assert (p.intent, p.qty, p.from_warehouse, p.to_warehouse, [i.code for i in p.items]) == ("stock_transfer", 1250, "Shop floor", "Main Warehouse", ["BOLT"])
    zero = understand("move 0 of BOLT from Shop floor to Main Warehouse", items, None, KNOWN)
    assert zero.qty is None and zero.bad_qty
    assert understand("move -5 of BOLT from Shop floor to Main Warehouse", items, None, KNOWN).bad_qty
    assert understand("what is the status of PUR-ORD-2026-00012", items, None, KNOWN).intent == "document_status"       # other questions are untouched


def ask(app, q, decide=True):
    return app.chat.ask(q, "tester", decide)


def test_the_chat_prepares_the_request_and_asks_about_anything_missing(world):
    app, erp = world
    r = ask(app, "Move 30 BOLT from Stores to Shop floor")
    assert r["intent"] == "stock_transfer" and r["status"] == "answered" and "I prepared a request to move 30 pcs of Bolt from Stores to Shop floor" in r["answer"]
    assert "Nothing is created in ERPNext" in r["answer"] and r["actions"][0]["approval_id"] and erp.stock_transfers == []
    assert app.approvals.get(r["actions"][0]["approval_id"]).request.facts["qty"] == 30
    again = ask(app, "transfer 10 of BOLT from stores to shop floor")
    assert "already waiting for your OK" in again["answer"] and len(app.approvals.list("pending", 50)) == 1
    no_qty = ask(app, "move BOLT from Stores to Shop floor")
    assert no_qty["status"] == "clarify" and "How many pcs of Bolt" in no_qty["answer"] and "Stores 100 pcs" in no_qty["answer"]          # says where it is held
    no_wh = ask(app, "move 5 BOLT")
    assert no_wh["status"] == "clarify" and "Which warehouses?" in no_wh["answer"] and "move 5 BOLT from <warehouse> to <warehouse>" in no_wh["answer"]
    no_item = ask(app, "move 5 from Stores to Shop floor")
    assert no_item["status"] == "clarify" and "Which item" in no_item["answer"]
    zero = ask(app, "move -5 BOLT from Stores to Shop floor")
    assert zero["status"] == "clarify" and "plain number above zero" in zero["answer"]
    too_many = ask(app, "move 900 BOLT from Stores to Shop floor")
    assert "Stores holds only 100 pcs" in too_many["answer"] and "Nothing was changed" in too_many["answer"]
    assert len(app.approvals.list("pending", 50)) == 1


def test_only_someone_who_can_approve_may_ask_the_chat_to_move_stock(world):
    app, _ = world
    r = ask(app, "Move 30 BOLT from Stores to Shop floor", decide=False)
    assert "Only people who can approve" in r["answer"] and app.approvals.list("pending", 50) == [] and app.transfers.list() == []


def test_explaining_a_transfer_instruction_never_prepares_a_second_request(world):
    app, _ = world
    app.settings.update_section(OWNER, "assistant", {"enabled": True})
    out = app.chat.explain("Move 30 BOLT from Stores to Shop floor", "tester", True)
    assert out["status"] == "not_enough_information" and "nothing to explain" in out["answer"].lower() and app.approvals.list("pending", 50) == []


def test_the_decision_history_shows_the_transfer_behind_a_request(world):
    app, erp = world
    res = app.transfers.prepare("BOLT", 30, "Stores", "Shop floor", "anna")
    app.approvals.decide(res["approval_id"], "approve", OWNER, "app")
    entry = app.history.entry(res["approval_id"])
    (move,) = [c for c in entry["context"] if c["heading"] == "Stock transfer"]
    assert "30 pcs of BOLT from Stores to Shop floor; status created, draft STE-MOCK-0001" in move["lines"][0]
    assert entry["status"] == "decided" and "stock transfer" in entry["what"].lower() or "transfer" in str(entry["what"]).lower()
