"""ERPNext / Frappe REST adapter.

Uses only the standard resource API (`/api/resource/<DocType>`) with an API key + secret, so it
works against a local Docker site or Frappe Cloud. Give the agent a dedicated low-privilege user
with *read* access to Item, Bin and Stock Ledger Entry.

NOTE: written from Frappe's documented REST conventions and unit-tested with a fake HTTP session.
It has NOT been exercised against a live ERPNext in this build - run `erp-agents erp-check`
against your instance first and compare a few numbers with Stock Balance in the UI.

Mapping
  items        Item                (is_stock_item=1, disabled=0)   -> code, name, group, uom, lead_time_days, creation
               Stock Ledger Entry  the first movement date per item: history begins at the earlier of that and `creation`
  stock        Bin                 actual_qty (sum over warehouses); reserved = reserved_qty + reserved_qty_for_production
                                   + reserved_qty_for_sub_contract; ordered_qty (open PO qty); planned_qty (open work orders)
  consumption  Stock Ledger Entry  outgoing rows (actual_qty < 0) for configurable voucher types
Limitations
  * Expected arrival = earliest `schedule_date` of open Purchase Order lines not yet fully received
    (PO status "To Receive" / "To Receive and Bill"). Needs read access to Purchase Order; without it
    the date is unknown and the agent says so.
  * Consumption defaults to Delivery Note + Sales Invoice vouchers. Add e.g. "Stock Entry" via
    `consumption_voucher_types` if you consume raw material that way (note: stock transfers are also
    Stock Entries and would be counted as consumption).
"""
from __future__ import annotations

import json
import re
import logging
import time
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Optional, Sequence

import requests
from requests.adapters import HTTPAdapter

try:  # urllib3 is a dependency of requests
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None  # type: ignore

from urllib.parse import quote

from .. import obs
from ..netguard import no_redirects
from .base import (STOCK_COLUMNS, CountLine, DailyUsage, ItemInfo, MessageResult, PurchaseOrderDraft, PurchaseOrderResult, QuoteRule, ReceiptLine, RequisitionDraft, SalesDue,
                   StockInfo, StockTransferDraft, SupplierContact, TaxRow, TaxSetup, VendorOffer, WarehouseStock)

log = logging.getLogger(__name__)

PAGE = 1000
BIN_FIELDS = ["item_code", "warehouse", "actual_qty", *STOCK_COLUMNS, "projected_qty"]
BIN_FIELDS_MID = ["item_code", "warehouse", "actual_qty", "reserved_qty", "reserved_qty_for_production", "reserved_qty_for_sub_contract",
                  "ordered_qty", "indented_qty", "planned_qty", "projected_qty"]       # without the newest column(s)
BIN_FIELDS_BASIC = ["item_code", "warehouse", "actual_qty", "reserved_qty", "ordered_qty"]
TRANSIT_CACHE_SECONDS = 300
# Tax Rule criteria that need the order's addresses or an item, which the agent does not have when it only compares vendors.
UNJUDGEABLE_RULE_FIELDS = ["billing_city", "billing_county", "billing_state", "billing_zipcode", "billing_country",
                           "shipping_city", "shipping_county", "shipping_state", "shipping_zipcode", "shipping_country", "item", "item_group"]


# A read-only field on the Purchase Order that carries the id of the approved proposal. It makes order creation idempotent:
# if ERPNext accepted an order but the answer was lost, the retry finds the order by this reference instead of creating a second one.
PO_REF_FIELD = "custom_agent_ref"
PO_REF_LABEL = "Agent proposal reference"
PO_APPROVER_FIELD = "custom_agent_approved_by"
PO_APPROVER_LABEL = "Approved by (Stock Assistant)"
MR_NOTE_FIELD = "custom_agent_note"
MR_NOTE_LABEL = "Stock Assistant note"


class ERPNextError(RuntimeError):
    pass


class ERPNextClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
        warehouses: Optional[list[str]] = None,
        consumption_voucher_types: Optional[list[str]] = None,
        item_groups: Optional[list[str]] = None,
        timeout: float = 30.0,
        session: Optional[Any] = None,
        company: Optional[str] = None,
    ) -> None:
        self.company = company or None
        self.base_url = base_url.rstrip("/")
        self.warehouses = warehouses or []
        self.voucher_types = consumption_voucher_types or ["Delivery Note", "Sales Invoice"]
        self.item_groups = item_groups or []
        self.timeout = timeout
        if session is None:
            session = requests.Session()
            if Retry is not None:
                retry = Retry(total=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))
                session.mount("http://", HTTPAdapter(max_retries=retry))
                session.mount("https://", HTTPAdapter(max_retries=retry))
            no_redirects(session)                          # the API key and secret travel in every request's header
            obs.observe_session(session, "erpnext")        # one line per request (what, status, milliseconds) with the task's id, never the query string
        self.session = session
        self.session.headers.update({"Authorization": f"token {api_key}:{api_secret}", "Accept": "application/json"})

    # ---------------------------------------------------------------- low level
    def _list(self, doctype: str, fields: list[Any], filters: list, order_by: Optional[str] = None,
              parent: Optional[str] = None, group_by: Optional[str] = None) -> list[dict]:
        rows: list[dict] = []
        start = 0
        while True:
            params = {
                "fields": json.dumps(fields),
                "filters": json.dumps(filters),
                "limit_start": start,
                "limit_page_length": PAGE,
            }
            if order_by:
                params["order_by"] = order_by
            if parent:                       # child tables (e.g. Purchase Order Item) are listed through their parent doctype
                params["parent"] = parent
            if group_by:
                params["group_by"] = group_by
            resp = self.session.get(f"{self.base_url}/api/resource/{doctype}", params=params, timeout=self.timeout)
            if resp.status_code >= 400:
                raise ERPNextError(f"{doctype}: HTTP {resp.status_code} {resp.text[:200]}")
            batch = resp.json().get("data", [])
            rows.extend(batch)
            if len(batch) < PAGE:
                return rows
            start += PAGE

    # ---------------------------------------------------------------- interface
    def healthcheck(self) -> bool:
        try:
            resp = self.session.get(f"{self.base_url}/api/method/frappe.auth.get_logged_user", timeout=self.timeout)
            return resp.status_code == 200
        except Exception as exc:  # noqa: BLE001
            log.warning("ERPNext healthcheck failed: %s", exc)
            return False

    def list_items(self) -> list[ItemInfo]:
        filters: list = [["is_stock_item", "=", 1], ["disabled", "=", 0]]
        if self.item_groups:
            filters.append(["item_group", "in", self.item_groups])
        rows = self._list("Item", ["name", "item_name", "item_group", "stock_uom", "lead_time_days", "creation"], filters)
        first = self._first_movements()
        out = []
        for r in rows:
            lt = r.get("lead_time_days")
            starts = [d for d in (_to_date(r.get("creation")), first.get(r["name"])) if d]
            out.append(
                ItemInfo(
                    code=r["name"],
                    name=r.get("item_name") or r["name"],
                    group=r.get("item_group"),
                    uom=r.get("stock_uom") or "units",
                    lead_time_days=float(lt) if lt else None,
                    created=min(starts) if starts else None,            # when this item's history begins (see _first_movements)
                )
            )
        return out

    def _first_movements(self) -> dict[str, date]:
        """The date of each item's first stock ledger entry. An item's history starts there if that is earlier than the item record's creation date: data
        loaded from an old system (or entered late) carries years of movements on an item that was only created in ERPNext recently, and measuring usage
        from the creation date would make a year of sales invisible. Items with no movement keep their creation date."""
        # Frappe 16 wants a SQL function written as a dict; older versions want it as text.
        for field in ({"MIN": "posting_date", "as": "first_date"}, "min(posting_date) as first_date"):
            try:
                rows = self._list("Stock Ledger Entry", ["item_code", field], [["is_cancelled", "=", 0]], group_by="item_code")
                return {r["item_code"]: d for r in rows if r.get("item_code") and (d := _to_date(r.get("first_date")))}
            except Exception as exc:  # noqa: BLE001 - without it the old behaviour (creation date) applies, which is only less informed
                last = exc
        log.warning("could not read when each item's stock history begins (%s): usage will be measured from each item's creation date", last)
        return {}

    def list_warehouses(self) -> list[str]:
        """Warehouses that can hold stock (group warehouses are only folders), for the warehouse filter."""
        rows = self._list("Warehouse", ["name", "is_group", "disabled"], [["disabled", "=", 0]])
        return sorted(r["name"] for r in rows if not r.get("is_group"))

    def get_stock(self, item_codes: Sequence[str], warehouses: Optional[Sequence[str]] = None) -> dict[str, StockInfo]:
        wh = list(warehouses or self.warehouses)
        totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        projected: dict[str, float] = defaultdict(float)
        has_projected: set[str] = set()
        places: dict[str, list[WarehouseStock]] = defaultdict(list)
        for chunk in _chunks(item_codes, 100):
            filters: list = [["item_code", "in", list(chunk)]]
            if wh:
                filters.append(["warehouse", "in", wh])
            rows = self._bin_rows(filters)
            transit = self._transit_warehouses()
            for r in rows:
                code = r["item_code"]
                totals[code]["in_transit" if r.get("warehouse") in transit else "on_hand"] += float(r.get("actual_qty") or 0)      # reported apart: the owner decides what it counts as
                for field, (_label, attr, _phrase) in STOCK_COLUMNS.items():
                    totals[code][attr] += float(r.get(field) or 0)
                place = WarehouseStock(str(r.get("warehouse") or "(no warehouse)"), float(r.get("actual_qty") or 0), float(r.get("reserved_qty") or 0), float(r.get("ordered_qty") or 0),
                                       r.get("warehouse") in transit)
                if place.on_hand or place.reserved or place.incoming:          # a Bin row stays after the last movement: empty ones would only clutter the list
                    places[code].append(place)
                if r.get("projected_qty") is not None:
                    projected[code] += float(r["projected_qty"])
                    has_projected.add(code)
        on_order = [code for code in item_codes if totals.get(code, {}).get("incoming", 0) > 0]
        arrivals = self._next_arrivals(on_order, wh) if on_order else {}
        # Items without a Bin row simply have zero stock.
        return {
            code: StockInfo(**{k: v for k, v in totals[code].items()}, projected=projected[code] if code in has_projected else None,
                            next_arrival=arrivals.get(code), by_warehouse=tuple(sorted(places[code], key=lambda p: (-p.on_hand, p.warehouse)))) if code in totals else StockInfo(on_hand=0.0)
            for code in item_codes
        }

    def _transit_warehouses(self) -> set[str]:
        """Names of the warehouses of type Transit (stock moving between your warehouses). Cached for a few minutes. If the ERP cannot say, none: stock then counts as on hand."""
        now = time.monotonic()
        cached = getattr(self, "_transit_cache", None)
        if cached and now - cached[0] < TRANSIT_CACHE_SECONDS:
            return cached[1]
        try:
            rows = self._list("Warehouse", ["name", "warehouse_type", "is_group", "disabled"], [["warehouse_type", "=", "Transit"]])
            names = {r["name"] for r in rows if r.get("warehouse_type") == "Transit" and not r.get("is_group") and not r.get("disabled")}
        except Exception as exc:  # noqa: BLE001 - never fail a stock read because the warehouse types could not be read
            log.warning("could not read the transit warehouses (%s); stock in transit will count as on hand", exc)
            return set()
        self._transit_cache = (now, names)
        return names

    def _bin_rows(self, filters: list) -> list[dict]:
        """Bin rows with every stock column. ERPNext versions differ in which columns exist, so try the full list,
        then without the newest ones, then the basics: a missing column means 0, never a failure."""
        last: Optional[ERPNextError] = None
        for fields in (BIN_FIELDS, BIN_FIELDS_MID, BIN_FIELDS_BASIC):
            try:
                return self._list("Bin", fields, filters)
            except ERPNextError as exc:
                last = exc
                log.warning("Bin query with %d fields failed (%s); trying fewer", len(fields), exc)
        raise last  # type: ignore[misc]

    # ---------------------------------------------------------------- vendors
    def get_vendor_offers(self, item_code: str, since: date, quotes: Optional[QuoteRule] = None) -> list[VendorOffer]:
        """Suppliers for one item, from the item's supplier list and its submitted purchase orders (and, when `quotes` is given, their valid supplier quotations).

        price        rate on the most recent order line (and the average over the period), always in the COMPANY currency
                     (the order's base_rate), so vendors billing in different currencies compare fairly
        lead time    goods-receipt date minus order date, averaged (needs Purchase Receipts)
        reliability  share of receipts posted on or before the order line's required-by date
        payment      longest credit_days in the supplier's payment terms template
        Everything except the order lines is best effort: when the agent user cannot read a doctype, that
        figure is simply unknown and the scoring says so."""
        listed: set[str] = set()
        try:
            listed = {r["supplier"] for r in self._list("Item Supplier", ["supplier"], [["parent", "=", item_code]], parent="Item") if r.get("supplier")}
        except (ERPNextError, requests.RequestException) as exc:
            log.warning("Could not read the supplier list of %s: %s", item_code, exc)

        lines = self._list("Purchase Order Item", ["parent", "qty", "rate", "base_rate", "schedule_date"],
                           [["item_code", "=", item_code], ["docstatus", "=", 1]], parent="Purchase Order")
        headers: dict[str, dict] = {}
        for chunk in _chunks(sorted({r["parent"] for r in lines}), 100):
            for h in self._list("Purchase Order", ["name", "supplier", "supplier_name", "transaction_date", "currency", "conversion_rate"],
                                [["name", "in", list(chunk)], ["docstatus", "=", 1], ["transaction_date", ">=", since.isoformat()]]):
                headers[h["name"]] = h

        receipts: dict[str, date] = {}          # purchase order -> first receipt date
        po_names = sorted(headers)
        try:
            for chunk in _chunks(po_names, 100):
                items = self._list("Purchase Receipt Item", ["parent", "purchase_order"],
                                   [["item_code", "=", item_code], ["docstatus", "=", 1], ["purchase_order", "in", list(chunk)]], parent="Purchase Receipt")
                prs = {r["parent"]: r["purchase_order"] for r in items if r.get("purchase_order")}
                for chunk2 in _chunks(sorted(prs), 100):
                    for pr in self._list("Purchase Receipt", ["name", "posting_date"], [["name", "in", list(chunk2)], ["docstatus", "=", 1]]):
                        d, po = _to_date(pr.get("posting_date")), prs[pr["name"]]
                        if d is not None and (po not in receipts or d < receipts[po]):
                            receipts[po] = d
        except (ERPNextError, requests.RequestException) as exc:
            log.warning("Could not read purchase receipts for %s (lead time and reliability unknown): %s", item_code, exc)

        by_supplier: dict[str, list[tuple[date, dict, dict]]] = defaultdict(list)
        for line in lines:
            h = headers.get(line["parent"])
            d = _to_date(h.get("transaction_date")) if h else None
            if h and d and h.get("supplier"):
                by_supplier[h["supplier"]].append((d, line, h))

        quoted = self._quotations(item_code, quotes) if quotes is not None else {}
        suppliers = sorted(set(by_supplier) | listed | set(quoted))
        info: dict[str, dict] = {}
        terms_days: dict[str, float] = {}
        try:
            for chunk in _chunks(suppliers, 100):
                flt = [["name", "in", list(chunk)]]
                try:
                    rows = self._list("Supplier", ["name", "supplier_name", "payment_terms", "disabled", "country"], flt)
                except ERPNextError:                      # no country field: still read the rest
                    rows = self._list("Supplier", ["name", "supplier_name", "payment_terms", "disabled"], flt)
                for s in rows:
                    info[s["name"]] = s
            templates = sorted({s["payment_terms"] for s in info.values() if s.get("payment_terms")})
            for chunk in _chunks(templates, 100):
                for t in self._list("Payment Terms Template Detail", ["parent", "credit_days"], [["parent", "in", list(chunk)]], parent="Payment Terms Template"):
                    terms_days[t["parent"]] = max(terms_days.get(t["parent"], 0.0), float(t.get("credit_days") or 0))
        except (ERPNextError, requests.RequestException) as exc:
            log.warning("Could not read supplier details (names and payment terms): %s", exc)

        offers: list[VendorOffer] = []
        for code in suppliers:
            if info.get(code, {}).get("disabled"):
                continue
            history = sorted(by_supplier.get(code, []), key=lambda t: t[0])
            rates = [r for r in (_base_rate(l, h) for _, l, h in history) if r]
            leads, on_time = [], []
            for order_date, line, h in history:
                got = receipts.get(h["name"])
                if got is None:
                    continue
                leads.append(float((got - order_date).days))
                promised = _to_date(line.get("schedule_date"))
                if promised is not None:
                    on_time.append(1.0 if got <= promised else 0.0)
            template = info.get(code, {}).get("payment_terms")
            qt = quoted.get(code)
            offers.append(VendorOffer(
                supplier=code,
                name=info.get(code, {}).get("supplier_name") or (history[-1][2].get("supplier_name") if history else None) or (qt or {}).get("supplier_name") or code,
                unit_price=_base_rate(history[-1][1], history[-1][2]) if history else None,
                avg_price=sum(rates) / len(rates) if rates else None,
                lead_time_days=sum(leads) / len(leads) if leads else None,
                on_time_rate=sum(on_time) / len(on_time) if on_time else None,
                orders=len(history),
                last_order=history[-1][0] if history else None,
                payment_days=terms_days.get(template) if template else None,
                listed=code in listed,
                currency=history[-1][2].get("currency") if history else (qt or {}).get("currency"),
                country=(info.get(code, {}).get("country") or None),
                quote_price=qt["price"] if qt else None, quote_ref=qt["name"] if qt else None, quote_date=qt["date"] if qt else None,
                quote_valid_till=qt["valid_till"] if qt else None, quote_qty=qt["qty"] if qt else None, quote_lead_days=qt["lead_days"] if qt else None,
            ))
        return offers

    def _quotations(self, item_code: str, rule: QuoteRule) -> dict[str, dict[str, Any]]:
        """Each supplier's latest VALID supplier quotation for this item: submitted, not stopped, cancelled or expired, and either not past its end date or (with no
        end date) not older than the owner's limit. The price is in company currency (ERPNext's own base rate). If the line quotes several prices for the item
        the lowest is used. Whatever cannot be read means 'no quotation', never a guess."""
        try:
            try:
                lines = self._list("Supplier Quotation Item", ["parent", "qty", "rate", "base_rate", "lead_time_days"], [["item_code", "=", item_code], ["docstatus", "=", 1]], parent="Supplier Quotation")
            except ERPNextError:
                lines = self._list("Supplier Quotation Item", ["parent", "qty", "rate", "base_rate"], [["item_code", "=", item_code], ["docstatus", "=", 1]], parent="Supplier Quotation")
            heads: dict[str, dict] = {}
            for chunk in _chunks(sorted({r["parent"] for r in lines}), 100):
                for h in self._list("Supplier Quotation", ["name", "supplier", "supplier_name", "transaction_date", "valid_till", "currency", "conversion_rate", "status"],
                                    [["name", "in", list(chunk)], ["docstatus", "=", 1]]):
                    heads[h["name"]] = h
        except (ERPNextError, requests.RequestException) as exc:
            log.warning("Could not read the supplier quotations for %s (they are left out): %s", item_code, exc)
            return {}
        best: dict[str, dict[str, Any]] = {}
        for line in lines:
            h = heads.get(line["parent"])
            if not h or not h.get("supplier") or (h.get("status") or "Submitted") in ("Expired", "Stopped", "Cancelled", "Draft"):
                continue
            made, until = _to_date(h.get("transaction_date")), _to_date(h.get("valid_till"))
            if until is not None:
                if until < rule.today:
                    continue
            elif made is None or (rule.today - made).days > rule.max_age_days:
                continue
            price = _base_rate(line, h)
            if not price:
                continue
            cand = {"name": h["name"], "price": price, "date": made, "valid_till": until, "qty": float(line.get("qty") or 0) or None, "currency": h.get("currency"),
                    "lead_days": float(line["lead_time_days"]) if line.get("lead_time_days") else None, "supplier_name": h.get("supplier_name")}
            cur = best.get(h["supplier"])
            if cur is None or (made or date.min, h["name"]) > (cur["date"] or date.min, cur["name"]) or (cand["name"] == cur["name"] and price < cur["price"]):
                best[h["supplier"]] = cand
        return best

    # ---------------------------------------------------------------- duties and taxes, as ERPNext itself sets them up
    def _doc(self, doctype: str, name: str) -> dict:
        resp = self.session.get(f"{self.base_url}/api/resource/{doctype}/{quote(name, safe='')}", timeout=self.timeout)
        if resp.status_code >= 400:
            raise ERPNextError(f"{doctype} {name}: HTTP {resp.status_code} {resp.text[:160]}")
        return resp.json().get("data") or {}

    def get_purchase_taxes(self, supplier: str, item_code: str, posting_date: date) -> TaxSetup:
        """The duties and taxes ERPNext would put on a purchase of this item from this supplier.

        1. ERPNext's own Tax Rule logic picks the purchase taxes template for the supplier (by supplier, supplier group,
           tax category, company ...): we call its method, so the choice is exactly ERPNext's.
        2. No rule matched: the default purchase taxes template of the company, as ERPNext does for a new order.
        3. The template's rows are read. If the item has an Item Tax Template that fits the supplier's tax category, its
           rates replace the matching rows' rates, as in ERPNext.
        Whatever cannot be read is returned as a note, never filled in."""
        notes: list[str] = []
        try:
            sup = next(iter(self._list("Supplier", ["name", "supplier_group", "tax_category", "country"], [["name", "=", supplier]])), {})
        except ERPNextError:
            sup = {}
            notes.append("could not read the supplier's tax category")
        tax_category = sup.get("tax_category") or None
        company = self.company

        template, source = None, "none"
        try:
            template = self._tax_rule_template(supplier, sup.get("supplier_group"), tax_category, company, posting_date, notes)
            if template:
                source = "tax rule"
        except (ERPNextError, requests.RequestException) as exc:
            notes.append(f"could not read the ERP's Tax Rules: {exc}")
        if not template:
            try:
                flt: list = [["is_default", "=", 1], ["disabled", "=", 0]] + ([["company", "=", company]] if company else [])
                defaults = self._list("Purchase Taxes and Charges Template", ["name", "company"], flt)
                if len(defaults) == 1:
                    template, source = defaults[0]["name"], "default template"
                elif len(defaults) > 1:
                    notes.append("several companies have a default purchase taxes template and no company is set for this agent")
                else:
                    notes.append("there is no default purchase taxes template")
            except (ERPNextError, requests.RequestException) as exc:
                notes.append(f"could not read the default purchase taxes template: {exc}")
        if not template:
            return TaxSetup(None, "none", (), tuple(notes), tax_category)

        try:
            doc = self._doc("Purchase Taxes and Charges Template", template)
        except (ERPNextError, requests.RequestException) as exc:
            return TaxSetup(template, source, (), (*notes, f"could not read template '{template}': {exc}"), tax_category)
        rows = [dict(r) for r in doc.get("taxes", [])]
        if any(r.get("included_in_print_rate") for r in rows):
            notes.append("some template rows are included in the price; this agent treats vendor prices as excluding tax")

        item_template, overrides = self._item_tax_overrides(item_code, tax_category, posting_date, notes)
        out: list[TaxRow] = []
        for r in rows:
            rate, from_item = float(r.get("rate") or 0), False
            account = r.get("account_head") or ""
            if account in overrides and r.get("charge_type") in ("On Net Total", "On Previous Row Amount", "On Previous Row Total", "On Item Quantity"):
                rate, from_item = overrides[account], True
            out.append(TaxRow(
                description=r.get("description") or account, charge_type=r.get("charge_type") or "", rate=rate,
                amount=float(r.get("tax_amount") or 0), category=r.get("category") or "Total", add_deduct=r.get("add_deduct_tax") or "Add",
                row_id=int(r["row_id"]) if r.get("row_id") else None, account=account, rate_from_item_template=from_item,
            ))
        return TaxSetup(template, source, tuple(out), tuple(notes), tax_category, item_template)

    def _tax_rule_template(self, supplier: str, supplier_group: Optional[str], tax_category: Optional[str], company: Optional[str],
                           posting_date: date, notes: list[str]) -> Optional[str]:
        """ERPNext's own Tax Rule selection for a purchase, done on the rules read through the normal API (this ERPNext does not
        expose its function). Same logic as ERPNext's `get_tax_template`: a rule must be in date, have exactly the supplier's
        tax category (blank = blank), and each of supplier / supplier group / company must be blank or equal; the rule that sets
        the most of those wins, then the higher priority.
        Rules that also depend on addresses, item or item group cannot be judged without the order's addresses: they are
        skipped and reported, not guessed."""
        fields = ["name", "tax_type", "purchase_tax_template", "supplier", "supplier_group", "tax_category", "company", "priority", "from_date", "to_date",
                  *UNJUDGEABLE_RULE_FIELDS]
        rules = self._list("Tax Rule", fields, [["tax_type", "=", "Purchase"]])
        today = posting_date
        candidates, skipped = [], []
        for r in rules:
            tpl = r.get("purchase_tax_template")
            f, t = _to_date(r.get("from_date")), _to_date(r.get("to_date"))
            if not tpl or (f and f > today) or (t and t < today):
                continue
            if (r.get("tax_category") or "") != (tax_category or ""):
                continue
            if (r.get("supplier") or supplier) != supplier or (r.get("supplier_group") or supplier_group or "") != (supplier_group or ""):
                continue
            if r.get("company") and r["company"] != company:
                if not company:
                    skipped.append(f"{r['name']} (for company {r['company']}, but no company is set for this agent)")
                continue
            if any(r.get(k) for k in UNJUDGEABLE_RULE_FIELDS):
                skipped.append(f"{r['name']} (depends on addresses or items)")
                continue
            matched = sum(1 for k in ("supplier", "supplier_group", "company") if r.get(k)) + 1          # +1: tax_type, as in ERPNext
            candidates.append((matched, int(r.get("priority") or 0), r["name"], tpl))
        if skipped:
            notes.append("Tax Rules not considered because they cannot be judged from here: " + "; ".join(skipped))
        if not candidates:
            notes.append("no Tax Rule matched this vendor")
            return None
        candidates.sort()
        best = candidates[-1]
        tied = {c[3] for c in candidates if c[:2] == best[:2]}
        if len(tied) > 1:
            notes.append(f"several Tax Rules tie for this vendor ({', '.join(sorted(tied))}); ERPNext picks among them arbitrarily, template '{best[3]}' was used")
        return best[3]

    def _item_tax_overrides(self, item_code: str, tax_category: Optional[str], posting_date: date, notes: list[str]) -> tuple[Optional[str], dict[str, float]]:
        """The item's own tax template (if one fits the supplier's tax category and date): {account: rate}, 0 where not applicable."""
        try:
            item = self._doc("Item", item_code)
            fits = []
            for t in item.get("taxes", []):
                valid_from = _to_date(t.get("valid_from"))
                if t.get("item_tax_template") and (not t.get("tax_category") or t.get("tax_category") == tax_category) and (not valid_from or valid_from <= posting_date):
                    fits.append((1 if t.get("tax_category") else 0, valid_from or date.min, t["item_tax_template"]))
            if not fits:
                return None, {}
            name = max(fits, key=lambda f: (f[0], f[1]))[2]          # an exact tax-category match, then the most recent
            tpl = self._doc("Item Tax Template", name)
            return name, {d["tax_type"]: (0.0 if d.get("not_applicable") else float(d.get("tax_rate") or 0)) for d in tpl.get("taxes", []) if d.get("tax_type")}
        except (ERPNextError, requests.RequestException) as exc:
            notes.append(f"could not read the item's tax template: {exc}")
            return None, {}

    def create_purchase_order(self, draft: PurchaseOrderDraft, acting: Optional[Any] = None) -> PurchaseOrderResult:
        """POST a Purchase Order. Not submitted: it stays a draft that a person reviews in ERPNext.
        With `acting` (the approver's own ERPNext session) ERPNext records that person as the creator and checks their permissions;
        without it the service account creates it and the approver is written in a field."""
        body: dict[str, Any] = {
            "supplier": draft.supplier,
            "transaction_date": date.today().isoformat(),
            "schedule_date": draft.required_by.isoformat(),
            "items": [{"item_code": draft.item_code, "qty": draft.qty, "rate": draft.rate, "uom": draft.uom,
                       "schedule_date": draft.required_by.isoformat()}],
        }
        company, problem = self._company_for_new_document()
        if problem:
            return PurchaseOrderResult(ok=False, error=problem)
        if company:
            body["company"] = company
        found = self._reference_guard("Purchase Order", draft.reference, draft.approved_by, body)
        if found is not None:
            return found
        try:
            self._in_supplier_currency(body, draft)
        except (ERPNextError, requests.RequestException) as exc:
            return PurchaseOrderResult(ok=False, error=f"could not work out the supplier's currency, so no order was created (the price is held in company currency): {exc}")
        if draft.supplier_quotation:
            self._link_quotation(body, draft)
        if draft.vendor_note:
            body["terms"] = draft.vendor_note            # the "Terms and Conditions" text printed on the order the vendor receives
        if draft.taxes_template:
            body["taxes_and_charges"] = draft.taxes_template     # the owner's purchase taxes template for this kind of purchase
            self._apply_taxes(body, draft)
        return self._post_document("Purchase Order", body, acting, "purchase order")

    def _apply_taxes(self, body: dict[str, Any], draft: PurchaseOrderDraft) -> None:
        """Put the template's rows on the order and the item's own tax template on its line. Naming the template is not enough: ERPNext fills the tax table of a new
        order only in its screen, so an order created through the API would otherwise have NO tax rows and a total without GST or duty. Best effort: if something cannot
        be read the order is still created with the template named, and the tax table can be filled in ERPNext (choose the template again)."""
        try:
            tpl = self._doc("Purchase Taxes and Charges Template", draft.taxes_template)
            rate = float(body.get("conversion_rate") or 1.0) or 1.0
            rows = []
            for r in tpl.get("taxes", []):
                row = {"charge_type": r["charge_type"], "account_head": r["account_head"], "description": r.get("description") or r["account_head"], "rate": r.get("rate") or 0,
                       "category": r.get("category") or "Total", "add_deduct_tax": r.get("add_deduct_tax") or "Add"}
                for key in ("row_id", "cost_center", "included_in_print_rate"):
                    if r.get(key):
                        row[key] = r[key]
                if r["charge_type"] == "Actual":
                    row["tax_amount"] = round(float(r.get("tax_amount") or 0) / rate, 2)          # a fixed amount in company currency, shown in the order's currency
                rows.append(row)
            if rows:
                body["taxes"] = rows
            sup = self._doc("Supplier", draft.supplier)
            notes: list[str] = []
            name, overrides = self._item_tax_overrides(draft.item_code, sup.get("tax_category") or None, date.today(), notes)
            if name:
                body["items"][0]["item_tax_template"] = name
                body["items"][0]["item_tax_rate"] = json.dumps(overrides)
        except Exception as exc:  # noqa: BLE001 - never lose the order over its tax rows
            log.warning("could not put the tax rows of '%s' on the order: %s", draft.taxes_template, exc)

    def _company_for_new_document(self) -> tuple[Optional[str], Optional[str]]:
        """(company, problem) for a document the assistant is about to create. The configured company, else the only company ERPNext has. With several companies and none
        configured it refuses: ERPNext would silently use the service account's default company, which may not be the one the stock and vendors belong to."""
        if self.company:
            return self.company, None
        try:
            names = sorted(r["name"] for r in self._list("Company", ["name"], []))
        except Exception as exc:  # noqa: BLE001 - if the companies cannot be read, behave as before and let ERPNext choose
            log.warning("could not list the companies (%s); ERPNext will choose the company for new documents", exc)
            return None, None
        if len(names) == 1:
            return names[0], None
        if len(names) > 1:
            return None, (f"ERPNext has several companies ({', '.join(names)}) and no company is set for the assistant, so nothing was created: it would land in whichever company "
                          "the assistant's ERPNext user has as its default. Set erp.company in the configuration file, restart, and press the button again.")
        return None, None

    def item_min_order_qty(self, item_code: str) -> Optional[float]:
        try:
            moq = self._doc("Item", item_code).get("min_order_qty")
        except (ERPNextError, requests.RequestException):
            return None
        return float(moq) if moq else None

    def _link_quotation(self, body: dict[str, Any], draft: PurchaseOrderDraft) -> None:
        """Tie the order's item to the Supplier Quotation its price came from, so ERPNext shows the connection, and take the price exactly as the vendor quoted it
        (in the vendor's own currency, not converted back from company currency at today's rate). ERPNext only accepts the link when the quotation is submitted and
        is for the same supplier and currency; if it would not, leave the link out (the order is still created, and the vendor note names the quotation)."""
        try:
            sq = self._doc("Supplier Quotation", draft.supplier_quotation)
            currency = body.get("currency") or self._company_currency()
        except (ERPNextError, requests.RequestException) as exc:
            log.info("quotation %s not linked on the order: %s", draft.supplier_quotation, exc)
            return
        lines = [l for l in sq.get("items", []) if l.get("item_code") == draft.item_code and float(l.get("rate") or 0) > 0]
        if not (sq.get("docstatus") == 1 and sq.get("supplier") == draft.supplier and sq.get("currency") == currency and lines):
            log.info("quotation %s not linked on the order: it is not a submitted quotation of %s for %s in %s", draft.supplier_quotation, draft.supplier, draft.item_code, currency)
            return
        line = min(lines, key=lambda l: float(l.get("base_rate") or l.get("rate") or 0))        # the lowest price they quoted for the item, as the comparison used
        item = body["items"][0]
        item["supplier_quotation"] = draft.supplier_quotation
        if line.get("name"):
            item["supplier_quotation_item"] = line["name"]
        item["rate"] = float(line["rate"])

    def create_material_request(self, draft: RequisitionDraft, acting: Optional[Any] = None) -> PurchaseOrderResult:
        """POST a Material Request of type Purchase (a purchase requisition). Draft, never submitted."""
        line: dict[str, Any] = {"item_code": draft.item_code, "qty": draft.qty, "uom": draft.uom, "schedule_date": draft.required_by.isoformat()}
        if draft.rate:
            line["rate"] = draft.rate
        body: dict[str, Any] = {"material_request_type": "Purchase", "transaction_date": date.today().isoformat(),
                                "schedule_date": draft.required_by.isoformat(), "items": [line]}
        company, problem = self._company_for_new_document()
        if problem:
            return PurchaseOrderResult(ok=False, error=problem)
        if company:
            body["company"] = company
        found = self._reference_guard("Material Request", draft.reference, draft.approved_by, body)
        if found is not None:
            return found
        if draft.note and draft.reference:
            body[MR_NOTE_FIELD] = draft.note[:900]
        return self._post_document("Material Request", body, acting, "purchase requisition")

    def create_stock_transfer(self, draft: StockTransferDraft, acting: Optional[Any] = None) -> PurchaseOrderResult:
        """POST a Stock Entry of type Material Transfer. Draft, never submitted: the stock moves when a person submits it in ERPNext. The line carries its unit and conversion
        factor (1: the quantity is in the item's stock unit), because ERPNext only fills them in its screen, not for a document created through the API."""
        if draft.from_warehouse == draft.to_warehouse:
            return PurchaseOrderResult(ok=False, error="the two warehouses are the same")
        line = {"item_code": draft.item_code, "qty": draft.qty, "uom": draft.uom, "stock_uom": draft.uom, "conversion_factor": 1, "transfer_qty": draft.qty,
                "s_warehouse": draft.from_warehouse, "t_warehouse": draft.to_warehouse}
        body: dict[str, Any] = {"stock_entry_type": "Material Transfer", "purpose": "Material Transfer", "posting_date": date.today().isoformat(),
                                "from_warehouse": draft.from_warehouse, "to_warehouse": draft.to_warehouse, "items": [line]}
        if draft.remarks:
            body["remarks"] = draft.remarks[:900]
        company, problem = self._company_for_new_document()
        if problem:
            return PurchaseOrderResult(ok=False, error=problem)
        if company:
            body["company"] = company
        found = self._reference_guard("Stock Entry", draft.reference, draft.approved_by, body)
        if found is not None:
            return found
        return self._post_document("Stock Entry", body, acting, "stock transfer")

    # ---------------------------------------------------------------- reading documents (for the chat and status questions)
    _DOC_SPECS = {
        "Purchase Order": dict(child="Purchase Order Item", party="supplier_name", party_id="supplier", date="transaction_date", due="schedule_date", total="grand_total", currency="currency",
                               done="received_qty", per_received="per_received", per_billed="per_billed", kind=None),
        "Material Request": dict(child="Material Request Item", party=None, party_id=None, date="transaction_date", due="schedule_date", total=None, currency=None,
                                 done="ordered_qty", per_received="per_received", per_billed=None, kind="material_request_type"),
        "Purchase Receipt": dict(child="Purchase Receipt Item", party="supplier_name", party_id="supplier", date="posting_date", due=None, total="grand_total", currency="currency",
                                 done="qty", per_received=None, per_billed="per_billed", kind=None),
    }

    FINISHED = ["Completed", "Closed", "Cancelled", "Delivered"]

    def get_documents(self, doctype: str, name: Optional[str] = None, item_code: Optional[str] = None, since: Optional[date] = None, limit: int = 20,
                      open_only: bool = False) -> list[dict[str, Any]]:
        spec = self._DOC_SPECS.get(doctype)
        if spec is None:
            raise ERPNextError(f"reading {doctype} is not supported")
        flt: list = [["docstatus", "<", 3]]
        if open_only:
            flt.append(["status", "not in", self.FINISHED])
        if name:
            flt.append(["name", "=", name])
        if since:
            flt.append([spec["date"], ">=", since.isoformat()])
        if item_code:
            lines = self._list(spec["child"], ["parent"], [["item_code", "=", item_code]], parent=doctype)
            parents = sorted({r["parent"] for r in lines})
            if not parents:
                return []
            flt.append(["name", "in", parents[:500]])
        base = ["name", "status", "docstatus", "owner", "creation"] + [spec[k] for k in ("party", "party_id", "date", "due", "total", "currency", "per_received", "per_billed", "kind") if spec[k]]
        tiers = [base + ["custom_agent_approved_by"], base]                       # the field exists only after `erp-agents erp-setup`
        rows = None
        for fields in tiers:
            try:
                rows = self._list(doctype, fields, flt, order_by="creation desc")
                break
            except ERPNextError as exc:
                last = exc
        if rows is None:
            raise last                                                            # type: ignore[possibly-undefined]
        rows = rows[:limit]
        names = [r["name"] for r in rows]
        lines_by: dict[str, list[dict]] = defaultdict(list)
        for chunk in _chunks(names, 100):
            for ln in self._list(spec["child"], ["parent", "item_code", "item_name", "qty", "uom", spec["done"]], [["parent", "in", list(chunk)]], parent=doctype):
                lines_by[ln["parent"]].append({"item_code": ln.get("item_code"), "item_name": ln.get("item_name"), "qty": float(ln.get("qty") or 0), "uom": ln.get("uom"),
                                               "done_qty": float(ln.get(spec["done"]) or 0)})
        out = []
        for r in rows:
            out.append({
                "doctype": doctype, "name": r["name"], "status": r.get("status") or "", "docstatus": int(r.get("docstatus") or 0), "party": r.get(spec["party"]) if spec["party"] else None,
                "party_id": r.get(spec["party_id"]) if spec["party_id"] else None,
                "date": r.get(spec["date"]), "due": r.get(spec["due"]) if spec["due"] else None, "created_by": r.get("owner"), "approved_by": r.get("custom_agent_approved_by") or "",
                "per_received": float(r.get(spec["per_received"]) or 0) if spec["per_received"] else None, "per_billed": float(r.get(spec["per_billed"]) or 0) if spec["per_billed"] else None,
                "total": float(r.get(spec["total"]) or 0) if spec["total"] else None, "currency": r.get(spec["currency"]) if spec["currency"] else None,
                "kind": r.get(spec["kind"]) if spec["kind"] else None, "lines": lines_by.get(r["name"], []),
            })
        return out

    def _reference_guard(self, doctype: str, reference: str, approved_by: str, body: dict[str, Any]) -> Optional[PurchaseOrderResult]:
        """Make creation safe to retry and record the approver. Returns a result when the document already exists (or cannot be checked)."""
        if not reference:
            return None
        try:
            existing = self._existing_doc(doctype, reference)
        except ERPNextError as exc:
            return PurchaseOrderResult(ok=False, error=str(exc))
        except requests.RequestException as exc:
            return PurchaseOrderResult(ok=False, error=f"could not reach ERPNext: {exc}")
        if existing:                                      # an earlier attempt got through even though its answer was lost
            return PurchaseOrderResult(ok=True, name=existing)
        body[PO_REF_FIELD] = reference
        if approved_by:
            body[PO_APPROVER_FIELD] = approved_by[:140]
        return None

    def _post_document(self, doctype: str, body: dict[str, Any], acting: Optional[Any], label: str) -> PurchaseOrderResult:
        session = acting.session if acting is not None else self.session
        extra: dict[str, Any] = {"headers": {"X-Frappe-CSRF-Token": acting.csrf}} if acting is not None and acting.csrf else {}
        try:
            resp = session.post(f"{self.base_url}/api/resource/{doctype}", json=body, timeout=self.timeout, **extra)
        except requests.RequestException as exc:
            return PurchaseOrderResult(ok=False, error=f"could not reach ERPNext: {exc}")
        if resp.status_code >= 400:
            if acting is not None and "CSRFTokenError" in resp.text:
                return PurchaseOrderResult(ok=False, error="your ERPNext sign-in needs refreshing: sign out of the Stock Assistant and sign in again")
            if acting is not None and resp.status_code in (401, 403):
                return PurchaseOrderResult(ok=False, error=f"ERPNext does not allow {acting.user} to create a {label}: {_erp_message(resp)}")
            return PurchaseOrderResult(ok=False, error=f"ERPNext refused the {label} (HTTP {resp.status_code}): {_erp_message(resp)}")
        name = (resp.json().get("data") or {}).get("name")
        return PurchaseOrderResult(ok=bool(name), name=name, error=None if name else "ERPNext did not return a document name")

    def update_purchase_order_qty(self, name: str, reference: str, item_code: str, qty: float, acting: Optional[Any] = None) -> PurchaseOrderResult:
        """The same order, a new quantity: a draft is saved with the new quantity; a submitted order is changed with ERPNext's own "Update Items"
        (erpnext.controllers.accounts_controller.update_child_qty_rate), which keeps it submitted and re-checks received and billed amounts itself."""
        path = f"{self.base_url}/api/resource/Purchase Order/{quote(name, safe='')}"
        try:
            resp = self.session.get(path, timeout=self.timeout)
        except requests.RequestException as exc:
            return PurchaseOrderResult(ok=False, error=f"could not reach ERPNext: {exc}")
        if resp.status_code == 404:
            return PurchaseOrderResult(ok=False, error="it is no longer in ERPNext")
        if resp.status_code >= 400:
            return PurchaseOrderResult(ok=False, error=f"ERPNext would not show the order (HTTP {resp.status_code}): {_erp_message(resp)}")
        doc = resp.json().get("data") or {}
        found = str(doc.get(PO_REF_FIELD) or "")
        if not reference or not (found == reference or found.startswith(reference + "-")):
            return PurchaseOrderResult(ok=False, error="it was not created by the Stock Assistant for this request, so it is left alone")
        docstatus = int(doc.get("docstatus") or 0)
        if docstatus == 2:
            return PurchaseOrderResult(ok=False, error="it has been cancelled in ERPNext")
        if doc.get("status") in ("Closed", "Completed", "On Hold", "Delivered"):
            return PurchaseOrderResult(ok=False, error=f"it is {doc['status']} in ERPNext; reopen it there first")
        rows = [r for r in doc.get("items") or [] if r.get("item_code") == item_code]
        if len(rows) != 1:
            return PurchaseOrderResult(ok=False, error=f"it has {len(rows)} lines for {item_code}, so change it in ERPNext")
        row = rows[0]
        received = float(row.get("received_qty") or 0)
        if qty < received:
            return PurchaseOrderResult(ok=False, error=f"{received:g} have already been received against it, so it cannot be for less than that")
        billed, rate = float(row.get("billed_amt") or 0), float(row.get("rate") or 0)
        if billed > 0 and qty * rate < billed - 0.005:
            return PurchaseOrderResult(ok=False, error=f"{billed:,.2f} has already been invoiced against it, so it cannot be for less than that")
        session = acting.session if acting is not None else self.session
        headers = {"X-Frappe-CSRF-Token": acting.csrf} if acting is not None and acting.csrf else {}
        who = acting.user if acting is not None else "the service account"
        try:
            if docstatus == 0:
                items = [{**r, "qty": qty} if r.get("name") == row.get("name") else r for r in doc.get("items") or []]
                done = session.put(path, json={"items": items}, timeout=self.timeout, headers=headers)
            else:
                # "Update Items" DELETES every line that is not listed, and blanks the description of a line sent without one: so every line is sent, as it is,
                # and only the quantity of ours differs (an unchanged line is skipped by ERPNext).
                trans = [{"docname": r.get("name"), "item_code": r.get("item_code"), "qty": qty if r.get("name") == row.get("name") else r.get("qty"), "rate": r.get("rate"),
                          "conversion_factor": r.get("conversion_factor") or 1, "uom": r.get("uom"), "schedule_date": r.get("schedule_date"), "description": r.get("description"),
                          **({"fg_item_qty": r["fg_item_qty"]} if r.get("fg_item_qty") is not None else {})} for r in doc.get("items") or []]
                done = session.post(f"{self.base_url}/api/method/erpnext.controllers.accounts_controller.update_child_qty_rate", timeout=self.timeout, headers=headers,
                                    json={"parent_doctype": "Purchase Order", "trans_items": json.dumps(trans), "parent_doctype_name": name, "child_docname": "items"})
        except requests.RequestException as exc:
            return PurchaseOrderResult(ok=False, error=f"could not reach ERPNext: {exc}")
        if done.status_code >= 400:
            if acting is not None and "CSRFTokenError" in done.text:
                return PurchaseOrderResult(ok=False, error="your ERPNext sign-in needs refreshing: sign out of the Stock Assistant and sign in again")
            if done.status_code in (401, 403):
                return PurchaseOrderResult(ok=False, error=f"ERPNext does not allow {who} to change a purchase order ({_erp_message(done)}). Give that role the Write permission on Purchase Order, "
                                                           "or change it in ERPNext")
            return PurchaseOrderResult(ok=False, error=f"ERPNext refused the change (HTTP {done.status_code}): {_erp_message(done)}")
        total = None
        try:
            after = self.session.get(path, timeout=self.timeout)
            if after.status_code < 400:
                total = (after.json().get("data") or {}).get("grand_total")
        except requests.RequestException:
            pass
        return PurchaseOrderResult(ok=True, name=name, total=float(total) if total is not None else None, submitted=docstatus == 1, note="changed the quantity")

    def cancel_purchase_order(self, name: str, reference: str, acting: Optional[Any] = None) -> PurchaseOrderResult:
        """Delete a DRAFT purchase order, or cancel a submitted one, that this application created for the proposal `reference`. Anything else is left alone, with the reason."""
        try:
            resp = self.session.get(f"{self.base_url}/api/resource/Purchase Order/{quote(name, safe='')}", timeout=self.timeout)
        except requests.RequestException as exc:
            return PurchaseOrderResult(ok=False, error=f"could not reach ERPNext: {exc}")
        if resp.status_code == 404:
            return PurchaseOrderResult(ok=True, name=name, note="already gone")
        if resp.status_code >= 400:
            return PurchaseOrderResult(ok=False, error=f"ERPNext would not show the order (HTTP {resp.status_code}): {_erp_message(resp)}")
        doc = resp.json().get("data") or {}
        found = str(doc.get(PO_REF_FIELD) or "")
        if not reference or not (found == reference or found.startswith(reference + "-")):
            return PurchaseOrderResult(ok=False, error="it was not created by the Stock Assistant for this request, so it is left alone")
        docstatus = int(doc.get("docstatus") or 0)
        if docstatus == 2:
            return PurchaseOrderResult(ok=True, name=name, note="already cancelled")
        got, billed = float(doc.get("per_received") or 0), float(doc.get("per_billed") or 0)
        if got > 0 or billed > 0:
            return PurchaseOrderResult(ok=False, error=f"{got:g}% of it has been received and {billed:g}% billed, so it has to be dealt with in ERPNext (cancel the receipt or invoice first)")
        session = acting.session if acting is not None else self.session
        extra: dict[str, Any] = {"headers": {"X-Frappe-CSRF-Token": acting.csrf}} if acting is not None and acting.csrf else {}
        who = acting.user if acting is not None else "the service account"
        try:
            if docstatus == 0:
                done = session.delete(f"{self.base_url}/api/resource/Purchase Order/{quote(name, safe='')}", timeout=self.timeout, **extra)
            else:
                done = session.post(f"{self.base_url}/api/method/frappe.client.cancel", json={"doctype": "Purchase Order", "name": name}, timeout=self.timeout, **extra)
        except requests.RequestException as exc:
            return PurchaseOrderResult(ok=False, error=f"could not reach ERPNext: {exc}")
        if done.status_code >= 400:
            if acting is not None and "CSRFTokenError" in done.text:
                return PurchaseOrderResult(ok=False, error="your ERPNext sign-in needs refreshing: sign out of the Stock Assistant and sign in again")
            verb = "delete" if docstatus == 0 else "cancel"
            if done.status_code in (401, 403):
                return PurchaseOrderResult(ok=False, error=f"ERPNext does not allow {who} to {verb} a purchase order ({_erp_message(done)}). Give that role the {verb.capitalize()} permission on Purchase Order, or {verb} it in ERPNext")
            return PurchaseOrderResult(ok=False, error=f"ERPNext refused to {verb} it (HTTP {done.status_code}): {_erp_message(done)}")
        return PurchaseOrderResult(ok=True, name=name, note="deleted the draft" if docstatus == 0 else "cancelled it")

    # The read-only fields the Stock Assistant writes on the documents it creates (made once by `erp-agents erp-setup`).
    CUSTOM_FIELDS: dict[str, tuple] = {
        "Purchase Order": (
            (PO_REF_FIELD, PO_REF_LABEL, "Data", "supplier_name", 1, "Set by the Stock Assistant when an approved proposal creates this order. Do not edit."),
            (PO_APPROVER_FIELD, PO_APPROVER_LABEL, "Data", PO_REF_FIELD, 0, "Who approved this in the Stock Assistant, and where from. Do not edit."),
        ),
        "Material Request": (
            (PO_REF_FIELD, PO_REF_LABEL, "Data", "company", 1, "Set by the Stock Assistant when an approved proposal creates this requisition. Do not edit."),
            (PO_APPROVER_FIELD, PO_APPROVER_LABEL, "Data", PO_REF_FIELD, 0, "Who approved this in the Stock Assistant, and where from. Do not edit."),
            (MR_NOTE_FIELD, MR_NOTE_LABEL, "Small Text", PO_APPROVER_FIELD, 0, "The Stock Assistant's recommendation (vendor, price, delivery) for the buyer. Do not edit."),
        ),
        "Stock Entry": (
            (PO_REF_FIELD, PO_REF_LABEL, "Data", "company", 1, "Set by the Stock Assistant when an approved request creates this stock transfer. Do not edit."),
            (PO_APPROVER_FIELD, PO_APPROVER_LABEL, "Data", PO_REF_FIELD, 0, "Who approved this in the Stock Assistant, and where from. Do not edit."),
        ),
    }

    # ---------------------------------------------------------------- purchasing follow-up and stock intelligence (FollowUpERP in base.py)
    def get_receipt_lines(self, since: date) -> list[ReceiptLine]:
        """Goods receipts posted since `since` (drafts too, returns left out), each line with its purchase order line and any purchase invoice against it.
        Prices are compared in the order's own currency (the receipt and the order of one supplier share it)."""
        heads = {h["name"]: h for h in self._list("Purchase Receipt", ["name", "supplier", "supplier_name", "posting_date", "docstatus", "is_return"],
                                                  [["posting_date", ">=", since.isoformat()], ["docstatus", "<", 2]]) if not h.get("is_return")}
        if not heads:
            return []
        lines: list[dict] = []
        for chunk in _chunks(sorted(heads), 100):
            lines += self._list("Purchase Receipt Item", ["parent", "item_code", "item_name", "uom", "received_qty", "qty", "rejected_qty", "rate", "purchase_order", "purchase_order_item"],
                                [["parent", "in", list(chunk)]], parent="Purchase Receipt")
        po_lines: dict[str, dict] = {}
        try:
            for chunk in _chunks(sorted({l["purchase_order_item"] for l in lines if l.get("purchase_order_item")}), 100):
                for r in self._list("Purchase Order Item", ["name", "parent", "qty", "rate", "schedule_date", "received_qty"], [["name", "in", list(chunk)]], parent="Purchase Order"):
                    po_lines[r["name"]] = r
        except (ERPNextError, requests.RequestException) as exc:
            log.warning("could not read the purchase order lines behind the receipts (nothing to compare against): %s", exc)
        invoices = self._invoices_for(sorted(heads), sorted({l["purchase_order"] for l in lines if l.get("purchase_order")}))
        out = []
        for l in lines:
            h = heads.get(l["parent"])
            if not h or not l.get("item_code"):
                continue
            po = po_lines.get(l.get("purchase_order_item") or "")
            received = float(l.get("received_qty") or 0) or float(l.get("qty") or 0) + float(l.get("rejected_qty") or 0)
            submitted = int(h.get("docstatus") or 0) == 1
            before = float(po.get("received_qty") or 0) - (float(l.get("qty") or 0) if submitted else 0.0) if po else 0.0
            inv = invoices.get((l["parent"], l["item_code"])) or invoices.get((l.get("purchase_order") or "", l["item_code"])) or {}
            out.append(ReceiptLine(
                receipt=l["parent"], posting_date=_to_date(h.get("posting_date")), supplier=h.get("supplier") or "", supplier_name=h.get("supplier_name") or h.get("supplier") or "",
                item_code=l["item_code"], item_name=l.get("item_name") or l["item_code"], uom=l.get("uom") or "units", received_qty=received, accepted_qty=float(l.get("qty") or 0),
                rejected_qty=float(l.get("rejected_qty") or 0), rate=float(l["rate"]) if l.get("rate") is not None else None, purchase_order=l.get("purchase_order") or None,
                po_qty=float(po["qty"]) if po and po.get("qty") is not None else None, po_rate=float(po["rate"]) if po and po.get("rate") is not None else None,
                po_due=_to_date(po.get("schedule_date")) if po else None, po_received_before=max(before, 0.0),
                invoice=inv.get("name"), invoice_status=inv.get("status"), invoice_rate=inv.get("rate"), submitted=submitted))
        return out

    def _invoices_for(self, receipts: list[str], orders: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
        """(receipt or order, item) -> the purchase invoice against it: its name, status and price. Unknown when the ERP cannot say (never a guess)."""
        found: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            rows: list[dict] = []
            for field, names in (("purchase_receipt", receipts), ("purchase_order", orders)):
                for chunk in _chunks(names, 100):
                    rows += self._list("Purchase Invoice Item", ["parent", "item_code", "rate", "purchase_receipt", "purchase_order"], [[field, "in", list(chunk)]], parent="Purchase Invoice")
            heads: dict[str, dict] = {}
            for chunk in _chunks(sorted({r["parent"] for r in rows}), 100):
                for h in self._list("Purchase Invoice", ["name", "status", "docstatus"], [["name", "in", list(chunk)], ["docstatus", "<", 2]]):
                    heads[h["name"]] = h
        except (ERPNextError, requests.RequestException) as exc:
            log.warning("could not read the purchase invoices against the receipts (payment state unknown): %s", exc)
            return {}
        for r in rows:
            h = heads.get(r["parent"])
            if not h:
                continue
            info = {"name": h["name"], "status": "Draft" if int(h.get("docstatus") or 0) == 0 else (h.get("status") or "Submitted"),
                    "rate": float(r["rate"]) if r.get("rate") is not None else None}
            for key in (r.get("purchase_receipt"), r.get("purchase_order")):
                if key:
                    found.setdefault((key, r.get("item_code") or ""), info)
        return found

    def get_open_sales_orders(self, item_codes: Sequence[str], until: date) -> dict[str, list[SalesDue]]:
        """Quantity still to deliver on submitted, open sales orders due by `until` (overdue lines included), in the stock unit. No customer is read."""
        out: dict[str, list[SalesDue]] = defaultdict(list)
        lines: list[dict] = []
        for chunk in _chunks(list(item_codes), 100):
            lines += self._list("Sales Order Item", ["parent", "item_code", "qty", "delivered_qty", "conversion_factor", "delivery_date"],
                                [["item_code", "in", list(chunk)], ["docstatus", "=", 1], ["delivery_date", "<=", until.isoformat()]], parent="Sales Order")
        open_orders: set[str] = set()
        for chunk in _chunks(sorted({l["parent"] for l in lines}), 100):
            for h in self._list("Sales Order", ["name", "status"], [["name", "in", list(chunk)], ["docstatus", "=", 1]]):
                if (h.get("status") or "") not in ("Closed", "Completed", "Cancelled", "On Hold", "To Bill"):
                    open_orders.add(h["name"])
        for l in lines:
            left = (float(l.get("qty") or 0) - float(l.get("delivered_qty") or 0)) * float(l.get("conversion_factor") or 1)
            if l["parent"] in open_orders and left > 0 and l.get("item_code"):
                out[l["item_code"]].append(SalesDue(l["item_code"], _to_date(l.get("delivery_date")), left))
        return dict(out)

    def get_supplier_contact(self, supplier: str) -> Optional[SupplierContact]:
        """The supplier's e-mail and mobile number: the Supplier's own fields (from its primary contact) first, then its linked Contacts, primary first.
        Nothing about the contact is logged."""
        email = mobile = name = ""
        try:
            s = self._doc("Supplier", supplier)
            email, mobile, name = (s.get("email_id") or "").strip(), (s.get("mobile_no") or "").strip(), (s.get("supplier_primary_contact") or "").strip()
        except (ERPNextError, requests.RequestException) as exc:
            log.warning("could not read supplier %s: %s", supplier, type(exc).__name__)
        if not (email and mobile):
            try:
                rows = self._list("Contact", ["name", "first_name", "email_id", "mobile_no", "phone", "is_primary_contact"],
                                  [["Dynamic Link", "link_doctype", "=", "Supplier"], ["Dynamic Link", "link_name", "=", supplier]])
            except (ERPNextError, requests.RequestException) as exc:
                log.warning("could not read the contacts of supplier %s: %s", supplier, type(exc).__name__)
                rows = []
            for r in sorted(rows, key=lambda r: not r.get("is_primary_contact")):
                email = email or (r.get("email_id") or "").strip()
                mobile = mobile or (r.get("mobile_no") or r.get("phone") or "").strip()
                name = name or (r.get("first_name") or r.get("name") or "")
        if not (email or mobile):
            return None
        return SupplierContact(supplier=supplier, contact_name=name, email=email, mobile=mobile)

    def get_item_alternatives(self, item_code: str) -> list[str]:
        rows = self._list("Item Alternative", ["item_code", "alternative_item_code", "two_way"], [["item_code", "=", item_code]])
        both = self._list("Item Alternative", ["item_code", "alternative_item_code", "two_way"], [["alternative_item_code", "=", item_code], ["two_way", "=", 1]])
        codes = [r["alternative_item_code"] for r in rows if r.get("item_code") == item_code and r.get("alternative_item_code")]
        codes += [r["item_code"] for r in both if r.get("alternative_item_code") == item_code and r.get("two_way") and r.get("item_code")]
        return [c for c in dict.fromkeys(codes) if c != item_code]

    def get_stock_counts(self, since: date) -> list[CountLine]:
        heads = {h["name"]: h for h in self._list("Stock Reconciliation", ["name", "posting_date", "docstatus", "purpose"],
                                                  [["posting_date", ">=", since.isoformat()], ["docstatus", "<", 2]]) if (h.get("purpose") or "Stock Reconciliation") != "Opening Stock"}
        out = []
        for chunk in _chunks(sorted(heads), 100):
            for l in self._list("Stock Reconciliation Item", ["parent", "item_code", "warehouse", "qty", "current_qty"], [["parent", "in", list(chunk)]], parent="Stock Reconciliation"):
                h = heads.get(l["parent"])
                if h and l.get("item_code"):
                    out.append(CountLine(document=l["parent"], posting_date=_to_date(h.get("posting_date")), item_code=l["item_code"], warehouse=l.get("warehouse") or "",
                                         counted_qty=float(l.get("qty") or 0), system_qty=float(l.get("current_qty") or 0), submitted=int(h.get("docstatus") or 0) == 1))
        return out

    def get_stock_values(self, item_codes: Sequence[str]) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        for chunk in _chunks(list(item_codes), 100):
            flt: list = [["item_code", "in", list(chunk)]]
            if self.warehouses:
                flt.append(["warehouse", "in", self.warehouses])
            for r in self._list("Bin", ["item_code", "stock_value"], flt):
                if r.get("item_code"):
                    out[r["item_code"]] += float(r.get("stock_value") or 0)
        return dict(out)

    def last_purchase_dates(self, item_codes: Sequence[str]) -> dict[str, tuple[date, str]]:
        lines: list[dict] = []
        for chunk in _chunks(list(item_codes), 100):
            lines += self._list("Purchase Receipt Item", ["parent", "item_code"], [["item_code", "in", list(chunk)], ["docstatus", "=", 1]], parent="Purchase Receipt")
        heads: dict[str, dict] = {}
        for chunk in _chunks(sorted({l["parent"] for l in lines}), 100):
            for h in self._list("Purchase Receipt", ["name", "posting_date", "supplier", "is_return"], [["name", "in", list(chunk)], ["docstatus", "=", 1]]):
                heads[h["name"]] = h
        out: dict[str, tuple[date, str]] = {}
        for l in lines:
            h = heads.get(l["parent"])
            d = _to_date(h.get("posting_date")) if h and not h.get("is_return") else None
            if d and (l["item_code"] not in out or d > out[l["item_code"]][0]):
                out[l["item_code"]] = (d, h.get("supplier") or "")
        return out

    def send_email(self, recipients: Sequence[str], subject: str, body: str, reference_doctype: str, reference_name: str) -> MessageResult:
        """Through ERPNext's own outgoing e-mail account (frappe.core.doctype.communication.email.make): the message is recorded as a Communication on the document,
        so it shows in the ERP's timeline. Fails when no outgoing e-mail account is set up in ERPNext."""
        payload = {"recipients": ", ".join(recipients), "subject": subject, "content": body, "doctype": reference_doctype, "name": reference_name,
                   "send_email": 1, "communication_medium": "Email"}
        try:
            resp = self.session.post(f"{self.base_url}/api/method/frappe.core.doctype.communication.email.make", json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            return MessageResult(ok=False, error=f"could not reach ERPNext: {type(exc).__name__}")
        if resp.status_code >= 400:
            return MessageResult(ok=False, error=f"ERPNext did not send the e-mail (HTTP {resp.status_code}): {_erp_message(resp)}")
        message = (resp.json() or {}).get("message")
        ref = message.get("name") if isinstance(message, dict) else (str(message) if message else None)
        return MessageResult(ok=True, reference=ref)


    def ensure_po_reference_field(self) -> str:
        """Create the fields the Stock Assistant writes on Purchase Order, Material Request and Stock Entry if missing.
        Returns 'exists' or 'created'. Needs the Custom Field permission (a one-time job for an administrator)."""
        made = False
        for doctype, fields in self.CUSTOM_FIELDS.items():
            for fieldname, label, ftype, after, indexed, note in fields:
                if self._field_present(doctype, fieldname):
                    continue
                body = {"dt": doctype, "fieldname": fieldname, "label": label, "fieldtype": ftype, "insert_after": after,
                        "read_only": 1, "no_copy": 1, "print_hide": 1, "search_index": indexed, "description": note}
                resp = self.session.post(f"{self.base_url}/api/resource/Custom Field", json=body, timeout=self.timeout)
                if resp.status_code >= 400:
                    raise ERPNextError(f"could not create the {doctype} field '{label}': HTTP {resp.status_code} {resp.text[:200]}")
                made = True
        return "created" if made else "exists"

    def _field_present(self, doctype: str, fieldname: str) -> bool:
        resp = self.session.get(f"{self.base_url}/api/resource/Custom Field/{quote(doctype + '-' + fieldname, safe='')}", timeout=self.timeout)
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        if resp.status_code == 403:       # the service account may not read Custom Field (it should not need to): the lookup fails loudly if the column is missing
            return True
        raise ERPNextError(f"could not check for the {doctype} field {fieldname}: HTTP {resp.status_code} {resp.text[:120]}")

    def _existing_doc(self, doctype: str, reference: str) -> Optional[str]:
        """The not-cancelled document already created for this proposal, if any. Refuses (ERPNextError) when its fields have not been set up."""
        ok = getattr(self, "_fields_ok", None)
        if ok is None:
            ok = self._fields_ok = set()
        if doctype not in ok:
            if not all(self._field_present(doctype, f[0]) for f in self.CUSTOM_FIELDS[doctype]):
                raise ERPNextError(f"the {doctype} fields '{PO_REF_LABEL}' and '{PO_APPROVER_LABEL}' do not exist in ERPNext yet, so duplicates could not "
                                   "be prevented and the approver could not be recorded. Run: erp-agents erp-setup")
            ok.add(doctype)
        rows = self._list(doctype, ["name"], [[PO_REF_FIELD, "=", reference], ["docstatus", "<", 2]])
        return rows[0]["name"] if rows else None

    def _company_currency(self) -> str:
        if not hasattr(self, "_ccy_cache"):
            if self.company:
                self._ccy_cache = self._doc("Company", self.company).get("default_currency") or ""
            else:
                rows = self._list("Company", ["name", "default_currency"], [])
                self._ccy_cache = (rows[0].get("default_currency") if len(rows) == 1 else "") or ""
        return self._ccy_cache

    def _in_supplier_currency(self, body: dict[str, Any], draft: PurchaseOrderDraft) -> None:
        """Vendor prices are compared and stored in company currency; a supplier billing in another currency needs the order in THAT
        currency (price converted at ERPNext's latest exchange rate), otherwise a base-currency number would be read as foreign money."""
        company_ccy = self._company_currency()
        if not company_ccy:
            return
        sup_ccy = self._doc("Supplier", draft.supplier).get("default_currency") or company_ccy
        if sup_ccy == company_ccy:
            return
        rows = self._list("Currency Exchange", ["exchange_rate", "date"], [["from_currency", "=", sup_ccy], ["to_currency", "=", company_ccy], ["date", "<=", date.today().isoformat()]],
                          order_by="date desc")
        if not rows:
            raise ERPNextError(f"no exchange rate from {sup_ccy} to {company_ccy} is set up in ERPNext")
        rate = float(rows[0]["exchange_rate"])
        body["currency"], body["conversion_rate"] = sup_ccy, rate
        for line in body["items"]:
            line["rate"] = round(float(line["rate"]) / rate, 4)

    def _next_arrivals(self, item_codes: Sequence[str], warehouses: Optional[Sequence[str]] = None) -> dict[str, date]:
        """Earliest expected receipt date per item from open Purchase Orders (lines not yet fully received).
        Best effort: if the agent user cannot read purchase orders, arrival dates are simply unknown."""
        try:
            lines: list[dict] = []
            for chunk in _chunks(item_codes, 100):
                filters: list = [["item_code", "in", list(chunk)], ["docstatus", "=", 1]]
                if warehouses:
                    filters.append(["warehouse", "in", list(warehouses)])
                lines += self._list("Purchase Order Item", ["parent", "item_code", "qty", "received_qty", "schedule_date"],
                                    filters, order_by="schedule_date asc", parent="Purchase Order")
            lines = [r for r in lines if r.get("schedule_date") and float(r.get("qty") or 0) - float(r.get("received_qty") or 0) > 0]
            parents = sorted({r["parent"] for r in lines})
            receiving: set[str] = set()
            for chunk in _chunks(parents, 100):
                rows = self._list("Purchase Order", ["name", "status"],
                                  [["name", "in", list(chunk)], ["status", "in", ["To Receive and Bill", "To Receive"]]])
                receiving.update(r["name"] for r in rows)
        except (ERPNextError, requests.RequestException) as exc:
            log.warning("Could not read open purchase orders (arrival dates unknown): %s", exc)
            return {}
        out: dict[str, date] = {}
        for r in lines:
            if r["parent"] not in receiving:
                continue
            d = _to_date(r["schedule_date"])
            if d is not None and (r["item_code"] not in out or d < out[r["item_code"]]):
                out[r["item_code"]] = d
        return out

    def get_consumption(self, item_codes: Sequence[str], start: date, end: date,
                        warehouses: Optional[Sequence[str]] = None) -> dict[str, list[DailyUsage]]:
        wh = list(warehouses or self.warehouses)
        per_day: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
        for chunk in _chunks(item_codes, 50):
            filters: list = [
                ["item_code", "in", list(chunk)],
                ["actual_qty", "<", 0],
                ["is_cancelled", "=", 0],
                ["posting_date", ">=", start.isoformat()],
                ["posting_date", "<=", end.isoformat()],
                ["voucher_type", "in", self.voucher_types],
            ]
            if wh:
                filters.append(["warehouse", "in", wh])
            rows = self._list("Stock Ledger Entry", ["item_code", "posting_date", "actual_qty"], filters, order_by="posting_date asc")
            for r in rows:
                d = _to_date(r["posting_date"])
                if d is not None:
                    per_day[r["item_code"]][d] += -float(r["actual_qty"])
        return {code: [DailyUsage(d, q) for d, q in sorted(days.items())] for code, days in per_day.items()}


def _erp_message(resp: Any) -> str:
    """The readable part of an ERPNext refusal."""
    try:
        data = resp.json()
        msgs = data.get("_server_messages")
        if msgs:
            parts = [json.loads(m).get("message", "") for m in json.loads(msgs)]
            text = re.sub(r"<[^>]+>", "", " ".join(p for p in parts if p))
            if text.strip():
                return text.strip()[:300]
        return str(data.get("exception") or data.get("message") or data)[:300]
    except Exception:  # noqa: BLE001
        return (getattr(resp, "text", "") or "")[:300]


def _base_rate(line: dict, header: dict) -> Optional[float]:
    """The line's price in company currency: ERPNext's own base_rate, else rate x the order's conversion rate (a foreign price is never used as is)."""
    base = line.get("base_rate")
    if base:
        return float(base)
    rate = line.get("rate")
    if not rate:
        return None
    return float(rate) * float(header.get("conversion_rate") or 1.0)


def _chunks(seq: Sequence[str], n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _to_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value)[:19].replace(" ", "T")).date()
