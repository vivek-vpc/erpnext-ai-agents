"""The stock pack's settings: thresholds with per-group and per-item overrides, the vendor selector, the demand calendar, and the stock fields of the system settings.

Admin-configurable policy for the inventory agent. Resolution order (later wins): code defaults -> settings.yaml `policy:` -> admin override on *global* -> on *item group*
-> on *item*. Every field has a description so an admin UI can be generated from the schema. `attach()` registers all of this with the core's `SettingsStore`.
"""
from __future__ import annotations

from datetime import date as date_type
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from erp_agents.core.erp.base import DEFAULT_IN_TRANSIT, DEFAULT_STOCK_ADD, DEFAULT_STOCK_SUBTRACT, IN_TRANSIT_COLUMN, IN_TRANSIT_LABEL, STOCK_COLUMNS
from erp_agents.core.policy import SystemSettings
from .templating import TemplateError, check_template


def _checked(setting: str, template: str, names: tuple[str, ...]) -> str:
    """A message template as a setting: only {name} for the names it documents (templating.py), so the error names the setting and what is allowed."""
    try:
        check_template(template, names)
    except TemplateError as exc:
        raise ValueError(f"{setting}: {exc}") from None
    return template


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    enabled: bool = Field(True, description="Monitor this item/group at all.")

    # ---- thresholds (the knobs the owner/admin cares about most) -------------
    min_qty: Optional[float] = Field(
        None, ge=0, description="Alert when stock position (on hand + on order) is at or below this quantity. Empty = rule off."
    )
    min_days_cover: Optional[float] = Field(
        7, gt=0, description="Alert when stock would last this many days or fewer at the planned usage rate. Empty = rule off."
    )
    use_lead_time_rule: bool = Field(
        True, description="Also alert when days of cover <= supplier lead time + safety days (i.e. it is time to order)."
    )
    safety_stock_days: float = Field(3, ge=0, description="Extra days of buffer added to lead time.")
    lead_time_days: Optional[float] = Field(
        None, gt=0, description="Override replenishment lead time. Empty = use the ERP item's lead time, else default."
    )
    default_lead_time_days: float = Field(7, gt=0, description="Used when neither override nor ERP lead time exists.")
    # ---- what counts as stock: the owner picks the ERP columns (see erp.base.STOCK_COLUMNS) and the warehouses
    stock_subtract: list[str] = Field(
        default_factory=lambda: list(DEFAULT_STOCK_SUBTRACT),
        description="ERP stock columns subtracted from on-hand stock to get AVAILABLE stock (e.g. reserved for sales orders). Empty = ignore reservations.",
    )
    stock_add: list[str] = Field(
        default_factory=lambda: list(DEFAULT_STOCK_ADD),
        description="ERP stock columns added to available stock to get the stock POSITION (e.g. on order, planned production). Empty = on-hand only.",
    )
    in_transit_stock: Literal["arriving", "shelf", "ignore"] = Field(
        DEFAULT_IN_TRANSIT,
        description="What stock in transit warehouses (moving between your warehouses) counts as: arriving = added to the stock position only, so it is not sold from yet; "
                    "shelf = counted like stock on the shelf; ignore = not counted at all.",
    )
    warehouses: list[str] = Field(
        default_factory=list, description="Only count stock (and usage) in these warehouses. Empty = all warehouses (or the ERP connection's own filter).",
    )

    # ---- how usage is estimated ----------------------------------------------
    usage_method: Literal["base", "recent", "blend"] = Field(
        "blend", description="base = long-window average, recent = short-window average, blend = weighted mix."
    )
    recent_window_days: int = Field(14, ge=3, le=120, description="Short window for 'recent' usage.")
    base_window_days: int = Field(90, ge=14, le=400, description="Long window for 'base' usage.")
    recent_weight: float = Field(0.4, ge=0, le=1, description="Weight of recent usage when usage_method = blend.")
    history_days: int = Field(
        480, ge=30, le=1100,
        description="How much consumption history to load from the ERP. Seasonal comparison needs about 15+ months (455+ days).",
    )
    trend_notable_pct: float = Field(25, ge=1, description="Usage change (%) vs base that is called out as a trend.")

    # ---- predictive early warnings (history based) -----------------------------
    predictive_enabled: bool = Field(True, description="Warn early when history says a threshold will be crossed soon.")
    lookahead_days: int = Field(14, ge=1, le=120, description="How far ahead the agent looks for a predicted crossing.")
    min_history_days: int = Field(30, ge=7, description="Minimum days of data needed before making predictions.")
    seasonal_adjust: bool = Field(True, description="Use last year's same-period demand uplift (needs >1 year of history).")
    seasonal_max_factor: float = Field(2.0, ge=1, le=5, description="Cap on the seasonal uplift applied to forecasts.")
    forecast_reorder_point: bool = Field(
        True, description="Also decide when to order from the demand forecast: last year's seasonal pattern for the lead-time days and the open sales orders due in them, "
                          "not only average daily use. It can only make an order come sooner, never later.",
    )
    sales_orders_in_forecast: bool = Field(
        True, description="Count what is still to deliver on open sales orders due within the lead time as demand. Only used when 'reserved for sales orders' is not "
                          "already subtracted from available stock (so a sales order is never counted twice).",
    )

    # ---- order suggestion handed to the procurement step -------------------------
    target_cover_days: float = Field(30, gt=0, description="Suggested order brings stock to lead + safety + this many days.")
    order_multiple: float = Field(1, gt=0, description="Round suggested quantity up to a multiple of this (pack size).")
    min_order_qty: float = Field(0, ge=0, description="Never suggest less than this.")

    # ---- alert behaviour -------------------------------------------------------------
    snooze_days: int = Field(3, ge=1, le=60, description="'Remind me later' length.")
    dismiss_days: int = Field(7, ge=1, le=180, description="'Ignore' length before the same condition may alert again.")
    reorder_cooldown_days: int = Field(3, ge=0, le=60, description="After approval, wait this long for a PO to appear before re-alerting.")

    @field_validator("stock_subtract", "stock_add", mode="after")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @field_validator("warehouses", mode="after")
    @classmethod
    def _clean_warehouses(cls, value: list[str]) -> list[str]:
        return [w for w in dict.fromkeys(w.strip() for w in value) if w]

    @model_validator(mode="after")
    def _check(self) -> "Policy":
        for field_name in ("stock_subtract", "stock_add"):
            unknown = [c for c in getattr(self, field_name) if c not in STOCK_COLUMNS]
            if unknown:
                raise ValueError(f"{field_name}: unknown stock column(s) {', '.join(unknown)} (choose from {', '.join(STOCK_COLUMNS)})")
        both = sorted(set(self.stock_subtract) & set(self.stock_add))
        if both:
            raise ValueError(f"stock column(s) {', '.join(both)} cannot be both added and subtracted")
        if self.recent_window_days >= self.base_window_days:
            raise ValueError("recent_window_days must be smaller than base_window_days")
        if self.history_days < self.base_window_days:
            raise ValueError("history_days must be at least base_window_days")
        return self


# Optional columns of the Stock Monitor table, in the order they appear. None is shown unless the owner adds it.
MONITOR_COLUMNS: dict[str, str] = {
    "available": "Available stock",
    **{c: label for c, (label, _attr, _phrase) in STOCK_COLUMNS.items()},
    IN_TRANSIT_COLUMN: IN_TRANSIT_LABEL,
    "position": "Stock position",
    "arrival_date": "Arrival date of open orders",
    "projected_qty": "ERPNext projected quantity",
}


class StockSystemSettings(SystemSettings):
    """The system settings of the stock pack: the platform's fields plus the schedule, the alert limits and the stock monitor's columns."""

    monitor_columns: list[str] = Field(
        default_factory=list,
        description="Extra columns shown in the Stock Monitor table (e.g. ordered quantity, arrival date). Empty = only the essentials.",
    )

    @field_validator("monitor_columns", mode="after")
    @classmethod
    def _known_columns(cls, value: list[str]) -> list[str]:
        unknown = [c for c in value if c not in MONITOR_COLUMNS]
        if unknown:
            raise ValueError(f"unknown Stock Monitor column(s) {', '.join(unknown)} (choose from {', '.join(MONITOR_COLUMNS)})")
        return [c for c in MONITOR_COLUMNS if c in value]          # canonical order, no duplicates

    check_cron: str = Field("0 6 * * *", description="When the inventory agent runs (cron, server time zone).")
    max_alerts_per_run: int = Field(20, ge=1, le=500, description="Most new approval requests sent per run (worst first).")
    stock_alerts: bool = Field(
        True, description="Send an information-only stock alert (low stock, or needed urgently) BEFORE any request to reorder. An early warning then stays an alert and "
                          "does not ask you to reorder. Off = the old behaviour: the first message is the request itself.",
    )
    stock_alert_repeat_days: int = Field(7, ge=1, le=60, description="Repeat an alert for an item that is still low after this many days. A worse level is always sent at once.")
    max_notices_per_run: int = Field(20, ge=1, le=500, description="Most stock alerts sent per check (worst first), so one bad day does not flood your phone.")
    monitor_failures_before_alert: int = Field(3, ge=1, le=48, description="Tell the owner when the stock check has failed (for example ERPNext unreachable) this many runs in a row.")

    @model_validator(mode="after")
    def _check(self) -> "SystemSettings":
        parts = self.check_cron.split()
        if len(parts) != 5:
            raise ValueError("check_cron must have 5 fields, e.g. '0 6 * * *'")
        return self


# Placeholders the vendor note template may use (filled from the proposal; see procurement/messages.py).
VENDOR_NOTE_FIELDS = ("item", "item_code", "qty", "uom", "required_by", "reference", "vendor", "unit_price", "total")


class ProcurementSettings(BaseModel):
    """How the vendor selector ranks suppliers and what happens when the owner approves a purchase order."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    weight_cost: float = Field(40, ge=0, le=100, description="How much total cost matters when ranking vendors (relative weight).")
    weight_delivery: float = Field(30, ge=0, le=100, description="How much delivery time matters, judged against how soon you run out.")
    weight_reliability: float = Field(20, ge=0, le=100, description="How much on-time delivery history matters.")
    weight_terms: float = Field(10, ge=0, le=100, description="How much payment terms (credit days) matter.")
    prefer_listed_suppliers: bool = Field(True, description="Give a small bonus to suppliers named on the item record in the ERP.")
    flag_price_jump_pct: Optional[float] = Field(10, gt=0, description="Warn when a quote is this % above that supplier's average price. Empty = never warn.")
    exclude_new_vendors: bool = Field(False, description="Never recommend a supplier with no past orders for this item (they are still listed).")
    min_vendors: int = Field(2, ge=1, le=10, description="Compare at least this many vendors; the request says so when fewer qualify.")
    history_days: int = Field(365, ge=30, le=1825, description="How far back to look at past orders for prices, lead times and reliability.")
    use_quotations: Literal["off", "prefer", "only"] = Field(
        "off", description="Prices from supplier quotations (the ERP's Supplier Quotation): off = compare on the prices of past orders only; prefer = a vendor's valid quotation "
                           "replaces its last order price, and a vendor that has quoted but was never ordered from can be compared; only = compare only the vendors that have a valid quotation.",
    )
    quotation_max_age_days: int = Field(
        30, ge=1, le=365, description="A quotation with no end date counts for this many days after its date. A quotation with an end date counts until then.",
    )
    fallback_qty: Optional[float] = Field(
        None, gt=0, description="Order this quantity when the inventory agent could not size an order (no usage history). Empty = ask the owner to decide.",
    )
    po_mode: Literal["off", "draft"] = Field(
        "draft", description="draft = on approval, create a DRAFT purchase order in the ERP for you to review and submit; off = only record the decision.",
    )
    snooze_days: int = Field(2, ge=1, le=30, description="'Remind me later' length for a purchase order request.")
    requisition_first_groups: list[str] = Field(
        default_factory=list, max_length=100,
        description="Item groups that get a purchase REQUISITION first (a Material Request your buyer turns into an order), not a purchase order. Empty = none.",
    )
    requisition_first_items: list[str] = Field(
        default_factory=list, max_length=500,
        description="Individual items (item codes) that get a purchase requisition first, whatever their group. Empty = none.",
    )
    require_personal_login: bool = Field(
        False, description="Only create orders and requisitions under the approver's own ERPNext login. An approval that does not come from a signed-in console session "
                           "(for example from WhatsApp) then waits until it is approved in the console. Off = those are created by the service account, with the approver's name in a field.",
    )
    include_taxes: bool = Field(
        True, description="Compare vendors on the total cost: goods plus the duties and taxes your ERP applies to a purchase from that vendor "
                          "(read from the ERP's Tax Rules and purchase taxes templates). Off = compare on price only.",
    )
    include_recoverable_taxes_in_cost: bool = Field(
        False, description="Count taxes the ERP books as payable-only, not as item cost (e.g. input tax credit) as cost. Off = they are shown but do not make a vendor look dearer.",
    )
    no_tax_setup: Literal["flag", "exclude"] = Field(
        "flag", description="When the ERP has no purchase tax setup that applies to a vendor, so duties cannot be checked: flag = still compare but warn; exclude = never recommend them.",
    )
    include_vendor_note: bool = Field(True, description="Put a note for the vendor on the purchase order (shown to you before you approve).")
    vendor_note_template: str = Field(
        "Please confirm delivery by {required_by} for {qty} {uom} of {item}. Our reference: {reference}.",
        max_length=500,
        description="The vendor note. You can use: " + ", ".join("{" + k + "}" for k in VENDOR_NOTE_FIELDS) + ".",
    )
    vendor_note_urgent: str = Field(
        "This order is urgent: please expedite if you can and confirm the earliest delivery date.",
        max_length=300,
        description="Added to the note when even this vendor's usual delivery time is after you would run out of stock. Empty = never added.",
    )

    @model_validator(mode="after")
    def _check(self) -> "ProcurementSettings":
        if self.weight_cost + self.weight_delivery + self.weight_reliability + self.weight_terms <= 0:
            raise ValueError("at least one weight must be above zero")
        return self

    @field_validator("vendor_note_template")
    @classmethod
    def _note_template(cls, value: str) -> str:
        return _checked("vendor_note_template", value, VENDOR_NOTE_FIELDS)


class CalendarEvent(BaseModel):
    """A festival, promotion or other known period when demand differs from the usual."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str = Field(min_length=1, max_length=80, description="What it is, e.g. 'Diwali sale'.")
    kind: Literal["festival", "promotion", "other"] = Field("promotion", description="Only a label.")
    start: date_type = Field(description="First day of the period.")
    end: date_type = Field(description="Last day of the period (same as start for one day).")
    uplift_pct: float = Field(description="Expected change in daily demand during the period, in %. +40 = 40% more; -20 = 20% less.", gt=-100, le=1000)
    item_groups: list[str] = Field(default_factory=list, description="Only these item groups. Empty groups and items = every item.")
    items: list[str] = Field(default_factory=list, description="Only these item codes. Empty groups and items = every item.")
    note: str = Field("", max_length=300, description="Free text for you; not used in any calculation.")

    @model_validator(mode="after")
    def _order(self) -> "CalendarEvent":
        if self.end < self.start:
            raise ValueError(f"{self.name!r}: the end date is before the start date")
        return self


class CalendarSettings(BaseModel):
    """Known demand events. Used to adjust days of cover, early warnings and suggested order quantities."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    enabled: bool = Field(True, description="Use the calendar when working out demand. Off = ignore every event.")
    events: list[CalendarEvent] = Field(
        default_factory=list, max_length=300,
        description="Festivals and promotions. History-based seasonality may already include recurring festivals; "
                    "add events for what history cannot show (a new promotion, a one-off festival).",
    )


# Placeholders the vendor messages may use (see followup/messages.py).
CHASE_FIELDS = ("vendor", "order", "item", "item_code", "qty", "still", "uom", "due", "days_late", "signature")
QUOTE_FIELDS = ("vendor", "item", "item_code", "qty", "uom", "reply_by", "reference", "signature")


class FollowUpSettings(BaseModel):
    """Purchasing follow-up (chasing late deliveries, checking goods receipts, asking vendors for prices, the vendor scorecard) and stock intelligence (slow and dead
    stock, cycle counts). A message to a vendor is only ever sent after a person presses Send; it goes to the e-mail address or mobile number on the supplier's
    contact in the ERP."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ---- how vendors are reached
    vendor_channel: Literal["email_first", "whatsapp_first", "email_only", "whatsapp_only"] = Field(
        "email_first", description="How a message to a vendor is sent: by e-mail through your ERP's outgoing mail account, or by WhatsApp to the mobile number on the "
                                   "supplier's contact. '..._first' falls back to the other when the first is missing or fails.",
    )
    whatsapp_country_code: str = Field(
        "", pattern=r"^[0-9]{0,4}$", description="Country calling code put in front of a supplier's mobile number that is written without one (for example 91). "
                                                 "Empty = only numbers written with their country code (+91..., 0091...) are used.",
    )
    whatsapp_vendor_template: str = Field(
        "", max_length=100, description="An approved WhatsApp template with one body variable, used when a vendor has not written to you in the last 24 hours (WhatsApp "
                                        "drops other messages then). Empty = WhatsApp reaches a vendor only inside that window; e-mail has no such limit.",
    )
    signature: str = Field("", max_length=200, description="Signed at the end of every message to a vendor, for example 'Purchasing, Test Trading Co'.")

    # ---- a vendor's WhatsApp replies
    vendor_reply_window_days: int = Field(
        30, ge=1, le=120, description="How many days after a message to a vendor their WhatsApp reply is still matched to it. It ends sooner when the order is received "
                                      "or the price round has been compared; after that the reply is not taken.",
    )
    vendor_reply_relay_per_day: int = Field(
        3, ge=1, le=20, description="The most replies from one vendor that are passed on to you in a day. Later ones are kept with the request, and you get one line saying there are more.",
    )
    vendor_reply_keep_per_day: int = Field(
        30, ge=5, le=200, description="The most replies from one vendor that are kept in a day. Beyond this they are dropped, so one number cannot fill the database.",
    )

    # ---- chasing late deliveries
    chase_enabled: bool = Field(True, description="Watch open purchase orders and ask you whether to remind the vendor when a delivery is late.")
    chase_grace_days: int = Field(1, ge=0, le=30, description="Days after the due date before an order counts as late.")
    chase_every_days: int = Field(3, ge=1, le=30, description="Ask again about the same late order after this many days.")
    chase_max: int = Field(3, ge=1, le=10, description="Most reminders sent to a vendor about one order; after that only you are told.")
    chase_message: str = Field(
        "Dear {vendor}, our purchase order {order} for {qty} {uom} of {item} was due on {due} and has not arrived in full ({still} {uom} still to come, "
        "{days_late} days late). Please reply with the date you will deliver. {signature}",
        max_length=800, description="The reminder sent to the vendor. You can use: " + ", ".join("{" + k + "}" for k in CHASE_FIELDS) + ".",
    )

    # ---- checking goods receipts
    receipt_check_enabled: bool = Field(True, description="Compare each goods receipt with its purchase order and tell you about differences before the invoice is paid.")
    receipt_lookback_days: int = Field(14, ge=1, le=120, description="Check goods receipts posted in the last this-many days.")
    price_tolerance_pct: float = Field(1, ge=0, le=50, description="A price on the receipt or invoice this % or more away from the order is a mismatch.")
    qty_tolerance_pct: float = Field(0, ge=0, le=50, description="Receiving this % more than was still to come on the order is a mismatch (0 = any extra).")

    # ---- asking vendors for prices
    quote_max_vendors: int = Field(3, ge=2, le=5, description="How many vendors are asked for a price at once.")
    quote_reply_days: int = Field(3, ge=1, le=30, description="Days the vendors have to reply before the comparison is sent to you.")
    quote_message: str = Field(
        "Dear {vendor}, please send your best price and delivery time for {qty} {uom} of {item} ({item_code}) by {reply_by}. Our reference: {reference}. {signature}",
        max_length=800, description="The request for a price. You can use: " + ", ".join("{" + k + "}" for k in QUOTE_FIELDS) + ".",
    )

    # ---- vendor scorecard
    scorecard_days: int = Field(180, ge=30, le=730, description="How far back the vendor scorecard looks (orders, receipts and prices).")

    # ---- slow and dead stock
    slow_stock_enabled: bool = Field(True, description="Once a week, list items that tie up cash: no use for a long time, or far more stock than you use.")
    slow_cover_days: int = Field(180, ge=30, le=1100, description="Stock that lasts longer than this many days at the current use is slow.")
    dead_after_days: int = Field(120, ge=30, le=1100, description="An item with stock and no use for this many days is dead stock.")
    return_window_days: int = Field(60, ge=0, le=365, description="Suggest returning to the vendor when the stock was bought within this many days.")
    slow_min_value: float = Field(0, ge=0, description="Leave out items whose stock is worth less than this (company currency).")

    # ---- cycle counts
    counts_enabled: bool = Field(True, description="Plan cycle counts each week and explain the differences found when a count is posted.")
    count_a_days: int = Field(30, ge=7, le=365, description="Count A items (the 80% of usage value) this often.")
    count_b_days: int = Field(90, ge=7, le=730, description="Count B items (the next 15%) this often.")
    count_c_days: int = Field(180, ge=7, le=1100, description="Count C items (the rest) this often.")
    counts_per_week: int = Field(15, ge=1, le=500, description="Most items on one week's count list.")
    variance_notable_pct: float = Field(2, ge=0, le=100, description="A count that differs from the system by this % or more (and by at least one unit) is explained to you.")

    # ---- when it runs
    daily_cron: str = Field("30 7 * * *", description="When late deliveries, goods receipts and new stock counts are checked (cron, your time zone).")
    weekly_cron: str = Field("0 8 * * 1", description="When the slow-stock list and the week's count list are sent (cron, your time zone).")

    @field_validator("chase_message")
    @classmethod
    def _chase_template(cls, value: str) -> str:
        return _checked("chase_message", value, CHASE_FIELDS)

    @field_validator("quote_message")
    @classmethod
    def _quote_template(cls, value: str) -> str:
        return _checked("quote_message", value, QUOTE_FIELDS)

    @model_validator(mode="after")
    def _check(self) -> "FollowUpSettings":
        for name in ("daily_cron", "weekly_cron"):
            if len(getattr(self, name).split()) != 5:
                raise ValueError(f"{name} must have 5 fields, e.g. '30 7 * * *'")
        if not (self.count_a_days <= self.count_b_days <= self.count_c_days):
            raise ValueError("count_a_days <= count_b_days <= count_c_days: A items are counted most often")
        if self.vendor_reply_relay_per_day > self.vendor_reply_keep_per_day:
            raise ValueError("vendor_reply_relay_per_day cannot be more than vendor_reply_keep_per_day")
        return self


RETIRED_PROCUREMENT_FIELDS = ("tax_rules", "home_country", "unknown_origin", "po_tax_template_import", "po_tax_template_domestic")


def attach(store: Any, base_policy: Optional[dict] = None, base_procurement: Optional[dict] = None, base_calendar: Optional[dict] = None,
           base_followup: Optional[dict] = None) -> Any:
    """Register this pack's settings with the core's store, and give the store the pack's convenience accessors (`effective_policy`, `procurement()`, `update_procurement()`...),
    so the rest of the pack reads `settings.procurement()` as it always did. The base values are validated at once: a bad settings file fails at start-up."""
    store.extend_system(StockSystemSettings)
    store.register_scoped("policy", Policy, base_policy or {})
    store.register_section("procurement", ProcurementSettings, base_procurement or {}, retired=RETIRED_PROCUREMENT_FIELDS)
    store.register_section("calendar", CalendarSettings, base_calendar or {})
    store.register_section("followup", FollowUpSettings, base_followup or {})
    store.effective_policy = lambda item_code=None, item_group=None: store.effective_scoped("policy", item_code, item_group)
    store.policy_with_sources = lambda item_code=None, item_group=None: store.scoped_with_sources("policy", item_code, item_group)
    store.update_policy = lambda actor, scope_type, scope_key, set_values=None, clear=None: store.update_scoped(actor, "policy", scope_type, scope_key, set_values, clear)
    store.reset_scope = lambda actor, scope_type, scope_key: store.reset_scoped(actor, "policy", scope_type, scope_key)
    store.procurement = lambda: store.section("procurement")
    store.calendar = lambda: store.section("calendar")
    store.followup = lambda: store.section("followup")
    store.update_procurement = lambda actor, set_values, clear=None: store.update_section(actor, "procurement", set_values, clear)
    return store
