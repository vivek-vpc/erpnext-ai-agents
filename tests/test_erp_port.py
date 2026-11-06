"""The ERP port is split: the core's `ERPClient` is generic and the stock pack's calls are in the pack's own port.

Nothing about stock, vendors or purchase orders may come back into the generic port (a sales or finance pack, or the Frappe app, would have to implement it), and both ERP
implementations that exist (the REST client and the demo ERP) must still offer everything the stock pack needs."""
import pytest

from ai_munimji_engine.core.erp import base as generic
from ai_munimji_engine.packs.stock_procurement import erp as stock
from erp_agents.core.erp.erpnext import ERPNextClient
from erp_agents.core.erp.mock import MockERP

STOCK_AND_PURCHASING_CALLS = {"list_items", "get_stock", "get_consumption", "list_warehouses", "get_vendor_offers", "item_min_order_qty", "get_purchase_taxes",
                              "create_purchase_order", "cancel_purchase_order", "update_purchase_order_qty", "get_documents", "create_material_request"}


def methods(protocol) -> set[str]:
    return {name for name in vars(protocol) if not name.startswith("_") and callable(getattr(protocol, name))}


def test_the_generic_port_only_says_whether_the_erp_is_reachable_and_finds_documents_by_reference():
    assert methods(generic.ERPClient) == {"healthcheck"}
    assert methods(generic.RecoveryERP) == {"find_by_reference"}


def test_the_stock_packs_calls_are_in_its_own_port_and_nowhere_else():
    assert methods(stock.StockERP) | methods(stock.PurchasingERP) == STOCK_AND_PURCHASING_CALLS
    assert not methods(stock.StockERP) & methods(stock.PurchasingERP)
    assert methods(generic.ERPClient).isdisjoint(STOCK_AND_PURCHASING_CALLS)
    whole = {m for base in stock.StockProcurementERP.__mro__ for m in methods(base)}
    assert STOCK_AND_PURCHASING_CALLS | {"healthcheck"} <= whole


def test_the_generic_module_holds_none_of_the_stock_types():
    assert not [n for n in vars(generic) if n in ("ItemInfo", "StockInfo", "VendorOffer", "PurchaseOrderDraft", "STOCK_COLUMNS", "TaxSetup")]
    assert hasattr(stock, "ItemInfo") and hasattr(stock, "STOCK_COLUMNS") and hasattr(generic, "FoundDocument")


@pytest.mark.parametrize("client", [lambda: MockERP(), lambda: ERPNextClient("http://erp.invalid", "k", "s")], ids=["the demo ERP", "the REST client"])
def test_both_implementations_still_offer_the_whole_port(client):
    erp = client()
    assert isinstance(erp, generic.ERPClient) and isinstance(erp, stock.StockProcurementERP)
    assert isinstance(erp, stock.StockERP) and isinstance(erp, stock.PurchasingERP)
    assert isinstance(erp, generic.RecoveryERP) and isinstance(erp, stock.FollowUpERP) and isinstance(erp, stock.TransferERP)


def test_the_core_does_not_import_the_stock_port():
    import ast
    from pathlib import Path
    core = Path(__file__).resolve().parent.parent / "src" / "erp_agents" / "core"
    offenders = []
    for path in core.rglob("*.py"):
        if "tests" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("ai_munimji_engine.packs"):
                if path.name not in ("mock.py", "erpnext.py", "world_erp.py"):      # the ERP implementations (the demo ERP, the REST client, the demo ERP over the shared test world) serve the stock pack and build its types
                    offenders.append(f"{path.relative_to(core)}:{node.lineno}")
    assert not offenders, "the core must reach the stock pack's types only through the ERP implementations: " + ", ".join(offenders)
