"""The chat (Telegram, WhatsApp, the console) answers the follow-up questions with the same tools the agents use, not with the general list of purchase documents.

Reported 25 Oct 2026: "Which goods receipts don't match their order?" listed every recent purchase order and receipt, cancelled and fully received ones included,
because the chat's rules only knew the document list."""
from datetime import timedelta

import pytest

from conftest import OWNER
from ai_munimji_engine.packs.stock_procurement.erp import CountLine, ReceiptLine
from erp_agents.core.erp.mock import MockERP
from erp_agents.packs.stock_procurement.assistant.chat import understand


@pytest.fixture
def app(make_app, clock):
    return make_app(erp=MockERP(today=clock().date()))


def ask(app, question):
    return app.chat.ask(question, "owner", True)


def receipt(app, name, received, rate=10.0, po_qty=100.0, before=0.0, rejected=0.0):
    t = app.inventory.today()
    app.erp.receipts.append(ReceiptLine(receipt=name, posting_date=t - timedelta(days=2), supplier="SUP-PRIME", supplier_name="Prime Supplies", item_code="BOLT-M8",
                                        item_name="Bolt M8 x 40", uom="pcs", received_qty=received, accepted_qty=received - rejected, rejected_qty=rejected, rate=rate,
                                        purchase_order="PO-0009", po_qty=po_qty, po_rate=10.0, po_due=t - timedelta(days=3), po_received_before=before))


@pytest.mark.parametrize("question,intent", [
    ("Which goods receipts don't match their order?", "receipt_mismatches"),
    ("Any GRN with rejected goods?", "receipt_mismatches"),
    ("Which deliveries are late?", "late_deliveries"),
    ("Which vendor is late?", "late_deliveries"),
    ("Show the vendor scorecard", "vendor_scorecard"),
    ("Which supplier is most reliable?", "vendor_scorecard"),
    ("What is dead stock?", "slow_stock"),
    ("Which items tie up cash?", "slow_stock"),
    ("Is there a substitute for OIL-5L?", "substitutes"),
    ("What should we count this week?", "stock_counts"),
    ("Explain the count differences", "stock_counts"),
    ("Forecast for GASKET-X", "demand_forecast"),
    ("Why did you order BOLT-M8?", "decision_history"),
    # the existing questions keep their answers
    ("What is the status of our open purchase orders?", "document_status"),
    ("Which purchase orders are late?", "document_status"),
    ("Which purchase receipts came this week?", "document_status"),
    ("What is running low?", "stock_overview"),
    ("Compare vendors for BOLT-M8", "vendor_compare"),
    ("Create a purchase order for BOLT-M8 for 500", "order_request"),
])
def test_each_question_reaches_the_answer_that_fits_it(question, intent):
    assert understand(question, []).intent == intent


def test_the_reported_question_lists_only_receipts_that_do_not_match(app):
    receipt(app, "MAT-PRE-0001", 120, rate=10.5)                          # 20 more than ordered, price +5 %
    receipt(app, "MAT-PRE-0002", 60, before=40)                           # the rest of an order: matches
    app.erp.add_open_order("PUR-ORD-0743", "SUP-PRIME", "BOLT-M8", 1000, app.inventory.today() + timedelta(days=30), received=1000)    # fully received: not a receipt problem
    out = ask(app, "Which goods receipts don't match their order?")
    assert out["intent"] == "receipt_mismatches"
    text = out["answer"]
    assert text.startswith("1 of 2 goods receipts") and "MAT-PRE-0001" in text and "20 pcs more than ordered" in text and "+5.0%" in text
    assert "MAT-PRE-0002" not in text and "PUR-ORD-0743" not in text and "Cancelled" not in text
    assert out["facts"] and out["source"] == "data"


def test_when_every_receipt_matches_it_says_so(app):
    receipt(app, "MAT-PRE-0003", 100)
    assert ask(app, "Which goods receipts don't match their order?")["answer"].startswith("All 1 goods receipts of the last 14 days match")


def test_late_deliveries_with_their_effect_on_stock(app):
    app.erp.add_open_order("PO-EXT-0001", "SUP-PRIME", "OIL-5L", 100, app.inventory.today() - timedelta(days=5), received=20, supplier_name="Prime Supplies")
    text = ask(app, "Which deliveries are late?")["answer"]
    assert "1 purchase order is late" in text and "PO-EXT-0001 from Prime Supplies, 5 days late" in text and "80 cans of OIL-5L still to come" in text and "days without it" in text
    assert app.erp.sent_emails == []                                       # a question never messages a vendor


def test_scorecard_slow_stock_counts_and_history_answer_from_their_tools(app):
    for i in range(3):
        receipt(app, f"PR-{i}", 100)
    assert "1. Prime Supplies: score" in ask(app, "Show the vendor scorecard")["answer"]
    assert "Obsolete Part Z (DEAD-Z), dead" in ask(app, "What is dead stock?")["answer"]
    assert ask(app, "What should we count this week?")["answer"].startswith("Count these")
    app.erp.counts.append(CountLine("SR-0001", app.inventory.today() - timedelta(days=1), "BOLT-M8", "Main Warehouse", 360, 300))
    assert "SR-0001, BOLT-M8" in ask(app, "Explain the count differences")["answer"]
    app.inventory.run()
    rec = next(r for r in app.approvals.list("pending", 50) if r.request.facts.get("item_code") == "BOLT-M8")
    app.approvals.decide(rec.id, "dismiss", OWNER, "test")
    history = ask(app, "Why did you suggest reordering BOLT-M8?")["answer"]
    assert "BOLT-M8" in history and "by a person" in history and "owner" not in history     # names stay out, as for the agents


def test_an_item_is_asked_for_when_the_question_needs_one(app):
    out = ask(app, "Is there a substitute?")
    assert out["intent"] == "substitutes" and out["status"] == "clarify"
    app.erp.add_item("OIL-5L-ALT", "Hydraulic Oil 5L (other brand)", lambda age: 1.0, 40)
    app.erp.alternatives["OIL-5L"] = ["OIL-5L-ALT"]
    assert "OIL-5L-ALT): 40 available" in ask(app, "Is there a substitute for OIL-5L?")["answer"]
    assert "reorder point" in ask(app, "Forecast for GASKET-X")["answer"]
