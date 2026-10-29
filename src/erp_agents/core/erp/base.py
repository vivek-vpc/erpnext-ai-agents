"""ERP adapter interface. Agents only ever talk to this, never to ERPNext directly,
so swapping ERPNext for Odoo (or a CSV file) means writing one new adapter."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional, Protocol, Sequence, runtime_checkable, Any


@dataclass(frozen=True)
class ItemInfo:
    code: str
    name: str
    group: Optional[str] = None
    uom: str = "units"
    lead_time_days: Optional[float] = None   # from the ERP item record, if maintained
    created: Optional[date] = None           # when the item's history begins (its creation, or its first stock movement if earlier): avoids diluting usage for brand-new items


@dataclass(frozen=True)
class WarehouseStock:
    """What one warehouse holds of an item (one ERPNext Bin row), so stock can be shown warehouse by warehouse."""
    warehouse: str
    on_hand: float
    reserved: float = 0.0                    # reserved for sales orders
    incoming: float = 0.0                    # on open purchase orders that deliver here
    in_transit: bool = False                 # a Transit-type warehouse: this stock is moving between your warehouses, it is not on a shelf


@dataclass(frozen=True)
class StockInfo:
    """Every stock figure the ERP keeps for an item (summed over the chosen warehouses). The owner decides in the
    policy which of them are added to or subtracted from on-hand stock (see STOCK_COLUMNS)."""
    on_hand: float
    reserved: float = 0.0                    # reserved for sales orders
    incoming: float = 0.0                    # quantity on open purchase orders
    planned: float = 0.0                     # quantity of open production work orders that will produce this item
    reserved_production: float = 0.0         # raw material reserved for production
    reserved_subcontract: float = 0.0        # reserved for subcontracting
    reserved_production_plan: float = 0.0    # reserved for production plans
    requested: float = 0.0                   # material requests not yet turned into orders
    in_transit: float = 0.0                  # stock in warehouses of type Transit (moving between warehouses). NOT part of on_hand: the policy decides what it counts as
    projected: Optional[float] = None        # the ERP's own projected quantity, for comparison only
    next_arrival: Optional[date] = None      # earliest expected receipt of the open purchase orders, if known
    by_warehouse: tuple[WarehouseStock, ...] = ()   # the same stock split by warehouse (largest first); empty when the ERP cannot say. The figures above are the sum of these


# Stock columns the owner can add or subtract: ERPNext Bin field -> (label, StockInfo attribute, how it reads in a sentence).
# "On hand" (actual_qty) is always the starting point. The order here is the order shown in the console.
STOCK_COLUMNS: dict[str, tuple[str, str, str]] = {
    "reserved_qty": ("Reserved for sales orders", "reserved", "reserved for sales orders"),
    "reserved_qty_for_production": ("Reserved for production", "reserved_production", "reserved for production"),
    "reserved_qty_for_sub_contract": ("Reserved for subcontracting", "reserved_subcontract", "reserved for subcontracting"),
    "reserved_qty_for_production_plan": ("Reserved for production plans", "reserved_production_plan", "reserved for production plans"),
    "ordered_qty": ("On order (purchase orders)", "incoming", "on open purchase orders"),
    "indented_qty": ("Requested (material requests)", "requested", "requested on material requests"),
    "planned_qty": ("Planned production (work orders)", "planned", "planned on production work orders"),
}
# Stock in transit is not an ERPNext Bin column: it is the stock held in Transit-type warehouses, and the adapter reports it apart from on-hand stock. The owner chooses
# what it counts as (policy.in_transit_stock): arriving = added to the stock position only; shelf = counted like stock on the shelf; ignore = not counted.
IN_TRANSIT_COLUMN = "in_transit"
IN_TRANSIT_LABEL = "In transit between warehouses"
IN_TRANSIT_PHRASE = "in transit between warehouses"
IN_TRANSIT_MODES = ("arriving", "shelf", "ignore")
DEFAULT_IN_TRANSIT = "arriving"
DEFAULT_STOCK_SUBTRACT = ["reserved_qty", "reserved_qty_for_production", "reserved_qty_for_sub_contract"]
DEFAULT_STOCK_ADD = ["ordered_qty", "planned_qty"]


def stock_column(stock: "StockInfo", name: str) -> float:
    if name == IN_TRANSIT_COLUMN:
        return float(stock.in_transit or 0.0)
    return float(getattr(stock, STOCK_COLUMNS[name][1]) or 0.0)


def column_phrase(name: str) -> str:
    """How a stock column reads in a sentence ("on open purchase orders", "in transit between warehouses")."""
    return IN_TRANSIT_PHRASE if name == IN_TRANSIT_COLUMN else STOCK_COLUMNS[name][2]


@dataclass(frozen=True)
class DailyUsage:
    day: date
    qty: float                               # positive number = quantity consumed/sold that day


@dataclass(frozen=True)
class VendorOffer:
    """What the ERP knows about buying one item from one supplier (from past orders and the item's supplier list)."""
    supplier: str                            # supplier code/id in the ERP
    name: str
    unit_price: Optional[float] = None       # rate on the most recent order
    avg_price: Optional[float] = None
    lead_time_days: Optional[float] = None   # observed: order date -> goods receipt, averaged
    on_time_rate: Optional[float] = None     # 0..1 share of receipts on or before the promised date
    orders: int = 0                          # past order lines for this item
    last_order: Optional[date] = None
    payment_days: Optional[float] = None     # credit days from the supplier's payment terms
    listed: bool = False                     # named as a supplier on the item record
    currency: Optional[str] = None
    country: Optional[str] = None            # the supplier's country (shown for information; the ERP decides which taxes apply)
    # What the supplier has QUOTED for this item (their latest valid Supplier Quotation). Filled in only when the owner asked for quotations to be read.
    quote_price: Optional[float] = None      # per unit, in company currency
    quote_ref: Optional[str] = None          # the quotation's number in the ERP
    quote_date: Optional[date] = None
    quote_valid_till: Optional[date] = None  # None = the quotation has no end date
    quote_qty: Optional[float] = None        # the quantity the price was quoted for
    quote_lead_days: Optional[float] = None  # the delivery time they quoted, if any


@dataclass(frozen=True)
class QuoteRule:
    """Ask for supplier quotations too. A quotation counts while it is valid on `today`; one with no end date counts for `max_age_days` after its date."""
    today: date
    max_age_days: int = 30


@dataclass(frozen=True)
class TaxRow:
    """One row of a purchase taxes template, as the ERP defines it (ERPNext: Purchase Taxes and Charges)."""
    description: str
    charge_type: str                         # Actual | On Net Total | On Previous Row Amount | On Previous Row Total | On Item Quantity
    rate: float = 0.0
    amount: float = 0.0                      # for charge_type "Actual"
    category: str = "Total"                  # Total (payable, not item cost) | Valuation | Valuation and Total (both)
    add_deduct: str = "Add"                  # Add | Deduct
    row_id: Optional[int] = None             # the 1-based row an "On Previous Row ..." charge refers to
    account: str = ""
    rate_from_item_template: bool = False    # the rate came from the item's own tax template, not the purchase template


@dataclass(frozen=True)
class TaxSetup:
    """Which duties and taxes the ERP applies when buying one item from one supplier, and why."""
    template: Optional[str]                  # the purchase taxes template the ERP chose (None = it applies none)
    source: str                              # "tax rule" | "default template" | "none"
    rows: tuple[TaxRow, ...] = ()
    notes: tuple[str, ...] = ()              # e.g. "no Tax Rule matched and no default template", or what could not be read
    tax_category: Optional[str] = None       # the supplier's tax category in the ERP
    item_template: Optional[str] = None      # the item tax template that adjusted some rates, if any


@dataclass(frozen=True)
class PurchaseOrderDraft:
    supplier: str
    item_code: str
    qty: float
    rate: float
    uom: str
    required_by: date
    remarks: str = ""
    vendor_note: str = ""                     # shown to the vendor on the purchase order (ERPNext: Terms and Conditions)
    taxes_template: Optional[str] = None      # name of the ERP's purchase taxes template to apply (owner-configured)
    reference: str = ""                       # stable id of the approved proposal: stored on the order so a retry finds it instead of creating a second one
    approved_by: str = ""                     # who approved it, e.g. "priya@acme.com via app": stored on the order, because ERPNext only sees the service account
    supplier_quotation: str = ""              # the Supplier Quotation the price comes from: linked on the order's item when ERPNext accepts the link (same supplier and currency)


@dataclass(frozen=True)
class RequisitionDraft:
    """A purchase requisition (ERPNext: a Material Request of type Purchase): asks for the goods; a buyer turns it into an order later."""
    item_code: str
    qty: float
    uom: str
    required_by: date
    rate: Optional[float] = None              # estimated price per unit in company currency (the recommended vendor's)
    note: str = ""                            # the recommendation, for the buyer: vendor, price, total, delivery
    reference: str = ""                       # stable id of the approved proposal (a retry finds the document instead of creating a second one)
    approved_by: str = ""


@dataclass(frozen=True)
class PurchaseOrderResult:
    ok: bool
    name: Optional[str] = None               # document id in the ERP, e.g. PO-00012
    error: Optional[str] = None
    note: str = ""                           # what was done, for cancel_purchase_order: "deleted the draft", "cancelled it", "already gone"
    total: Optional[float] = None            # update_purchase_order_qty: the order's grand total after the change, as the ERP worked it out
    submitted: bool = False                  # update_purchase_order_qty: the order is submitted (not a draft)


@dataclass(frozen=True)
class StockTransferDraft:
    """Moving stock from one of your warehouses to another (ERPNext: a Stock Entry of type Material Transfer). One item per transfer."""
    item_code: str
    qty: float
    uom: str
    from_warehouse: str
    to_warehouse: str
    remarks: str = ""                         # why, for whoever opens the draft in the ERP
    reference: str = ""                       # stable id of the approved request: stored on the document so a retry finds it instead of creating a second one
    approved_by: str = ""                     # who approved it, e.g. "priya@acme.com via app": stored on the document, because ERPNext only sees the service account


@dataclass(frozen=True)
class ReceiptLine:
    """One line of a goods receipt (ERPNext: Purchase Receipt Item), with what its purchase order line asked for and the invoice against it, if any.
    Prices are in company currency."""
    receipt: str
    posting_date: Optional[date]
    supplier: str
    supplier_name: str
    item_code: str
    item_name: str
    uom: str
    received_qty: float                       # everything that arrived (accepted + rejected)
    accepted_qty: float
    rejected_qty: float = 0.0
    rate: Optional[float] = None              # price per unit on the receipt
    purchase_order: Optional[str] = None
    po_qty: Optional[float] = None            # what the order line asked for
    po_rate: Optional[float] = None           # the order line's price per unit
    po_due: Optional[date] = None             # the order line's required-by date
    po_received_before: float = 0.0           # received against the order line by earlier receipts
    invoice: Optional[str] = None             # a purchase invoice against this receipt or its order, if any
    invoice_status: Optional[str] = None      # Draft | Unpaid | Partly Paid | Paid | Overdue | ...
    invoice_rate: Optional[float] = None      # the invoice line's price per unit, if it could be read
    submitted: bool = True                    # False = the receipt is still a draft


@dataclass(frozen=True)
class SalesDue:
    """Quantity still to deliver on an open sales order line, and when the customer wants it. No customer is named."""
    item_code: str
    due: Optional[date]
    qty: float


@dataclass(frozen=True)
class SupplierContact:
    """How to reach a supplier, from its contact in the ERP. PERSONAL DATA: used only to send a message a person approved, never shown to a language model,
    never logged in full and never stored (only a one-way hash of the number, to recognise a reply)."""
    supplier: str
    contact_name: str = ""
    email: str = ""
    mobile: str = ""                          # as written in the ERP; the sender cleans it into a WhatsApp number


@dataclass(frozen=True)
class CountLine:
    """One line of a stock count (ERPNext: Stock Reconciliation Item): what was counted against what the system said."""
    document: str
    posting_date: Optional[date]
    item_code: str
    warehouse: str
    counted_qty: float
    system_qty: float                         # the quantity the ERP held before the count (current_qty)
    submitted: bool = True


@dataclass(frozen=True)
class MessageResult:
    ok: bool
    reference: Optional[str] = None           # e.g. the ERP's Communication record
    error: Optional[str] = None


@runtime_checkable
class FollowUpERP(Protocol):
    """What the purchasing follow-up and stock intelligence features read from the ERP, in addition to `ERPClient`. Every method is optional: a feature whose method
    an ERP connection lacks says so instead of guessing (call them through `getattr`)."""

    def get_receipt_lines(self, since: date) -> list[ReceiptLine]: ...

    def get_open_sales_orders(self, item_codes: Sequence[str], until: date) -> dict[str, list[SalesDue]]: ...

    def get_supplier_contact(self, supplier: str) -> Optional[SupplierContact]: ...

    def get_item_alternatives(self, item_code: str) -> list[str]:
        """Item codes the ERP lists as approved alternatives (ERPNext: Item Alternative, both ways when the record says two-way)."""
        ...

    def get_stock_counts(self, since: date) -> list[CountLine]: ...

    def get_stock_values(self, item_codes: Sequence[str]) -> dict[str, float]:
        """Value of the stock on hand per item, in company currency (ERPNext: Bin stock_value)."""
        ...

    def last_purchase_dates(self, item_codes: Sequence[str]) -> dict[str, tuple[date, str]]:
        """When each item was last received, and from which supplier."""
        ...

    def send_email(self, recipients: Sequence[str], subject: str, body: str, reference_doctype: str, reference_name: str) -> MessageResult:
        """Send an e-mail through the ERP's own outgoing mail account, recorded on the document (ERPNext: a Communication)."""
        ...


@dataclass(frozen=True)
class FoundDocument:
    """A document this application made earlier, found again by the reference it was made with (see `RecoveryERP`)."""
    doctype: str
    name: str
    approved_by: str = ""                    # what the application wrote on it when it made it: "<person> via <channel>"


@runtime_checkable
class RecoveryERP(Protocol):
    """Finding a document by its reference. Optional, like `FollowUpERP`: a connection without it cannot finish an answer that was acted on but never written down (call it
    through `getattr`)."""

    def find_by_reference(self, doctype: str, reference: str) -> Optional[FoundDocument]:
        """The not-cancelled document of this type made for `reference`, or None. Raises when the ERP cannot be asked."""
        ...


@runtime_checkable
class TransferERP(Protocol):
    """Moving stock between warehouses. Optional, like `FollowUpERP`: a connection without it makes the feature say so (call it through `getattr`)."""

    def create_stock_transfer(self, draft: StockTransferDraft, acting: Optional[Any] = None) -> PurchaseOrderResult:
        """Create a DRAFT stock transfer (ERPNext: a Stock Entry of type Material Transfer); no stock moves until a person submits it in the ERP. Same rules as
        `create_purchase_order`: `acting` = the approver's own ERP session, the reference makes a retry safe, the approver is recorded on the document."""
        ...


@runtime_checkable
class ERPClient(Protocol):
    def list_items(self) -> list[ItemInfo]: ...

    def get_stock(self, item_codes: Sequence[str], warehouses: Optional[Sequence[str]] = None) -> dict[str, StockInfo]:
        """Stock figures summed over `warehouses` (None/empty = the ERP adapter's default, normally all)."""
        ...

    def get_consumption(self, item_codes: Sequence[str], start: date, end: date,
                        warehouses: Optional[Sequence[str]] = None) -> dict[str, list[DailyUsage]]:
        """Sparse list: only days with consumption need to be returned."""
        ...

    def list_warehouses(self) -> list[str]:
        """Names of the warehouses that can hold stock (for the warehouse filter)."""
        ...

    def get_vendor_offers(self, item_code: str, since: date, quotes: Optional[QuoteRule] = None) -> list[VendorOffer]:
        """Suppliers that can supply this item, with price/lead time/reliability from history since `since`. With `quotes`, also what each has quoted (and a supplier
        that has quoted but never been ordered from is included)."""
        ...

    def item_min_order_qty(self, item_code: str) -> Optional[float]:
        """The smallest quantity the ERP lets you order of this item (its minimum order quantity), or None if it sets none."""
        ...

    def get_purchase_taxes(self, supplier: str, item_code: str, posting_date: date) -> TaxSetup:
        """The duties and taxes the ERP itself would apply to a purchase of this item from this supplier."""
        ...

    def create_purchase_order(self, draft: PurchaseOrderDraft, acting: Optional[Any] = None) -> PurchaseOrderResult:
        """Create a DRAFT purchase order (a person reviews and submits it in the ERP). `acting` = the approver's own ERP session:
        when given, the ERP records THEM as the creator and applies their own permissions."""
        ...

    def cancel_purchase_order(self, name: str, reference: str, acting: Optional[Any] = None) -> PurchaseOrderResult:
        """Take back a purchase order THIS application created for the proposal `reference`: a draft is deleted, a submitted one is cancelled. Never touches an order that
        carries a different reference, one that has goods received or an invoice against it, or one that is already gone (that is a success: nothing is left to remove).
        `acting` = the person's own ERP session, so the ERP applies their permissions and records them."""
        ...

    def update_purchase_order_qty(self, name: str, reference: str, item_code: str, qty: float, acting: Optional[Any] = None) -> PurchaseOrderResult:
        """Change the quantity of `item_code` IN THE SAME purchase order (draft or submitted), keeping its vendor and price. Only an order THIS application created for
        `reference`; never below what has already been received or billed, never on a cancelled, closed or completed order."""
        ...

    def get_documents(self, doctype: str, name: Optional[str] = None, item_code: Optional[str] = None, since: Optional[date] = None, limit: int = 20,
                      open_only: bool = False) -> list[dict[str, Any]]:
        """Read-only. Purchase Orders, Material Requests or Purchase Receipts, newest first, each as
        {doctype, name, status, docstatus, party, party_id, date, due, created_by, approved_by, per_received, per_billed, total, currency, kind, lines:[{item_code, item_name, qty, done_qty, uom}]}.
        Filter by exact document name, by item on any line, and by earliest date. `open_only`: the ERP leaves out documents that are completed, closed, cancelled or delivered
        BEFORE the limit applies, so an old order that is still open is not lost behind newer finished ones."""
        ...

    def create_material_request(self, draft: RequisitionDraft, acting: Optional[Any] = None) -> PurchaseOrderResult:
        """Create a DRAFT purchase requisition (a Material Request of type Purchase), same rules as create_purchase_order."""
        ...

    def healthcheck(self) -> bool: ...
