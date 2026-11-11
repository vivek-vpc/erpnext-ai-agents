"""The inventory monitoring agent.

One run =  load items/stock/history from the ERP  ->  analyse each item with the admin's policy
->  reconcile with earlier alerts (no duplicates, respect snooze/ignore/cool-down, escalate when it
gets worse, close when stock recovers)  ->  send approval requests, worst first.

Approving hands a REORDER_NEEDED work order to the procurement step. The agent never creates
purchase orders itself.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, ContextManager, Optional
from zoneinfo import ZoneInfo

from ai_munimji_engine.storage import dumps
from ai_munimji_engine.stores.stock import StockStores

from ai_munimji_engine.core.approvals.models import ApprovalRecord, Decision
from ai_munimji_engine.core.approvals.render import render_plain
from ai_munimji_engine.core.approvals.service import ApprovalService, KindHandler
from ai_munimji_engine.core.common import Batches, Checkpoint, Clock, iso, short_id, slices, utcnow
from ..settings import Policy
from ai_munimji_engine.core.settings_store import SettingsError, SettingsStore
from ai_munimji_engine.core.workorders import REORDER_NEEDED, WorkOrderQueue
from ai_munimji_engine.packs.stock_procurement.erp import ItemInfo, StockProcurementERP
from .alerts import Alert, AlertRepo
from .events import Window, active_windows
from .analysis import SEV_CRITICAL, SEV_NAMES, STATUS_BREACH, STATUS_EARLY, STATUS_OK, Analysis, _n, analyze_item
from .messages import KIND, KIND_ALERT, KIND_MONITOR_DOWN, alert_level, build_alert_request, build_monitor_down_request, build_request
from .notices import LEVELS, NoticeRepo

log = logging.getLogger(__name__)
AGENT_NAME = "inventory"
SALES_HORIZON_DAYS = 120         # open sales orders due within this many days are read for the forecast (a lead time is never longer)


@dataclass
class RunReport:
    run_id: str
    dry_run: bool
    started_at: str
    finished_at: str = ""
    checked: int = 0
    ok: int = 0
    early_warnings: int = 0
    breaches: int = 0
    new_alerts: int = 0
    skipped: int = 0
    resolved: int = 0
    errors: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    failed: bool = False                     # the run could not read the ERP at all (nothing was checked)
    new_notices: int = 0                     # information-only stock alerts sent before any request

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _Fine:
    """What a run keeps of an item whose stock is fine, from its analysis until the loop that reconciles it with earlier alerts: the four things that loop and the sort before it read. The
    whole analysis (the usage series, the reasons, the rules, the policy) is some tens of kilobytes, and on a site of tens of thousands of items nearly every item is fine."""
    item_code: str
    status: str
    severity: int
    cover_days_on_hand: Optional[float]


def _short_error(exc: Exception) -> str:
    """One readable line for the owner and the run history: no traceback, no long response bodies."""
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text[:200]}" if text else type(exc).__name__


class InventoryAgent:
    def __init__(
        self,
        erp: StockProcurementERP,
        settings: SettingsStore,
        alerts: AlertRepo,
        approvals: ApprovalService,
        workorders: WorkOrderQueue,
        stock: StockStores,
        transaction: Callable[[], ContextManager[Any]],
        clock: Clock = utcnow,
    ) -> None:
        self.erp, self.settings, self.alerts = erp, settings, alerts
        self.approvals, self.workorders, self.runs, self.transaction, self.clock = approvals, workorders, stock.runs, transaction, clock
        self.notices = NoticeRepo(stock.notices, clock)
        self.order_settled: Optional[Callable[[str, datetime], bool]] = None        # (item, since) -> its order was made and none of it is still on the way; set by bootstrap
        self.extra_sections: list[Callable[[Analysis], list[Any]]] = []             # more sections for an alert or a reorder request (approved alternatives when short)
        approvals.register_handler(KIND, KindHandler(
            on_decision=self._on_decision, on_expired=self._on_expired, on_cancelled=self._on_cancelled,
        ))
        approvals.register_handler(KIND_ALERT, KindHandler(on_decision=self._on_notice_decision))

    def _with_extras(self, req: Any, a: Analysis) -> Any:
        """Add what the extra-section providers have to say about this item (they must not fail the alert)."""
        for provide in self.extra_sections:
            try:
                req.sections.extend(provide(a))
            except Exception:  # noqa: BLE001 - a courtesy section never stops an alert
                log.exception("an extra section for %s failed", a.item_code)
        return req

    # ================================================================== time
    def today(self) -> date:
        tz = ZoneInfo(self.settings.system().timezone)
        return self.clock().astimezone(tz).date()

    # ================================================================== main run
    def run(self, dry_run: bool = False, item_codes: Optional[list[str]] = None, batch_size: Optional[int] = None, checkpoint: Optional[Checkpoint] = None,
            batch_seconds: Optional[float] = None) -> RunReport:
        """`batch_size` / `checkpoint`: a long run works through the items in batches and calls `checkpoint(stage, done, total)` at the end of each one ("planning": working out each item's settings;
        "checking": reading and analysing, which writes nothing; "raising": alerts and requests, worst first). The caller commits there. "raising" is also cut by the clock: `batch_seconds` is the longest it goes
        between two checkpoints, so that what it has written is not locked for longer than that (a person answering a request that the run has just replaced waits that long). The result is the same as without batches: the same items are
        analysed with the same figures and acted on in the same order, so a run that stops half way and is run again acts on each item once (the alerts it already raised are
        found by the duplicate checks of `_reconcile` and `_notify`)."""
        run_id = short_id(8)
        now = self.clock()
        report = RunReport(run_id=run_id, dry_run=dry_run, started_at=iso(now))
        system = self.settings.system()
        today = self.today()

        try:
            items = self._items(item_codes)
        except Exception as exc:  # noqa: BLE001 - the ERP is unreachable or refused us: record the failed run instead of crashing
            return self._failed_run(report, exc, dry_run)
        planned: list[tuple[ItemInfo, Policy, dict[str, str]]] = []
        planning = Batches("planning", len(items), batch_size, checkpoint)         # working out each item's settings reads the settings store: minutes on a large site, and the caller's lease must be kept
        for it in items:
            try:
                try:
                    policy, sources = self.settings.policy_with_sources(it.code, it.group)
                except SettingsError as exc:
                    report.errors.append(f"{it.code}: {exc}")
                    continue
                if policy.enabled:
                    planned.append((it, policy, sources))
            finally:
                planning.one()

        analyses: list[tuple[Analysis | _Fine, Optional[Policy], Optional[dict[str, str]]]] = []
        if planned:
            history = self._history_by_warehouses(planned)
            reading = Batches("checking", len(planned), batch_size, checkpoint)
            for group in slices(planned, batch_size):
                try:
                    stock, usage = self._load(group, today, history)
                except Exception as exc:  # noqa: BLE001
                    return self._failed_run(report, exc, dry_run)
                sales = self._sales(group, today)
                for it, policy, sources in group:
                    try:
                        st = stock.get(it.code)
                        if st is None:
                            report.errors.append(f"{it.code}: no stock data returned by the ERP")
                            continue
                        try:
                            a = analyze_item(it, st, usage.get(it.code, []), policy, today, self._windows(it, today), sales.get(it.code, ()))
                            analyses.append((_Fine(a.item_code, a.status, a.severity, a.cover_days_on_hand), None, None) if a.status == STATUS_OK else (a, policy, sources))
                        except Exception as exc:  # noqa: BLE001 - one bad item must not stop the run
                            log.exception("analysis failed for %s", it.code)
                            report.errors.append(f"{it.code}: analysis failed ({exc})")
                    finally:
                        reading.one()

        report.checked = len(analyses)
        # Worst first so the per-run cap never hides a critical item behind routine ones.
        analyses.sort(key=lambda t: (-t[0].severity, t[0].cover_days_on_hand if t[0].cover_days_on_hand is not None else 1e9))

        budget = system.max_alerts_per_run
        notice_budget = system.max_notices_per_run
        acting = Batches("raising", len(analyses), batch_size, checkpoint, batch_seconds)
        for a, policy, sources in analyses:
            try:
                if a.status == STATUS_OK:
                    report.ok += 1
                elif a.status == STATUS_EARLY:
                    report.early_warnings += 1
                else:
                    report.breaches += 1
                try:
                    if a.status == STATUS_OK:
                        if not dry_run:
                            self._clear_notices(a.item_code)
                    else:
                        ended = self._end_quiet_period_if_order_done(a, dry_run)
                        if ended:
                            report.actions.append(ended)
                        # the information-only alert goes first; the request to reorder (if any) follows it
                        note = self._notify(a, policy, run_id, dry_run, notice_budget, request_follows=a.status != STATUS_EARLY or not system.stock_alerts)
                        if note:
                            report.actions.append(note)
                            if note["action"] in ("new_notice", "would_notify"):
                                notice_budget -= 1
                                report.new_notices += 1
                    if a.status == STATUS_EARLY and system.stock_alerts:
                        action = {"item_code": a.item_code, "status": a.status, "severity": SEV_NAMES[a.severity], "action": "skipped:alert_only"}     # an early warning only alerts
                    else:
                        action = self._reconcile(a, sources, run_id, dry_run, budget)
                except Exception as exc:  # noqa: BLE001
                    log.exception("reconcile failed for %s", a.item_code)
                    report.errors.append(f"{a.item_code}: {exc}")
                    continue
                if action["action"] in ("new_alert", "would_alert"):
                    budget -= 1
                    report.new_alerts += 1
                elif action["action"] == "resolved":
                    report.resolved += 1
                elif action["action"].startswith("skipped"):
                    report.skipped += 1
                report.actions.append(action)
            finally:
                acting.one()

        report.finished_at = iso(self.clock())
        self.runs.add(run_id, AGENT_NAME, report.started_at, report.finished_at, dry_run, dumps(report.to_dict()))
        if not dry_run and not item_codes:
            self._monitor_recovered()
        return report

    # ================================================================== a run that could not read the ERP
    def _failed_run(self, report: RunReport, exc: Exception, dry_run: bool) -> RunReport:
        """Keep a record of the failure (it shows in the run history), change nothing else, and tell the owner if it keeps happening."""
        message = _short_error(exc)
        log.warning("inventory run %s could not read the ERP: %s", report.run_id, message)
        report.failed = True
        report.errors.append(f"ERP could not be read: {message}")
        report.finished_at = iso(self.clock())
        self.runs.add(report.run_id, AGENT_NAME, report.started_at, report.finished_at, dry_run, dumps(report.to_dict()))
        if not dry_run:
            self._warn_monitor_down()
        return report

    def _recent_real_runs(self, limit: int) -> list[dict[str, Any]]:
        return [{"started_at": r["started_at"], **json.loads(r["summary_json"] or "{}")} for r in self.runs.recent(AGENT_NAME, limit)]

    def _warn_monitor_down(self) -> None:
        """After N failed runs in a row, send ONE 'the monitor is down' notice (not again while it is open or within a day of the last one)."""
        n = self.settings.system().monitor_failures_before_alert
        recent = self._recent_real_runs(n)
        if len(recent) < n or not all(r.get("failed") for r in recent):
            return
        existing = self.approvals.latest_of_kind(KIND_MONITOR_DOWN)
        if existing:
            last = datetime.fromisoformat(existing["created_at"].replace("Z", "+00:00"))
            if existing["status"] == "pending" or self.clock() - last < timedelta(hours=24):
                return
        errors = [f"{r['started_at'][:16].replace('T', ' ')} UTC: {(r.get('errors') or ['?'])[0]}" for r in recent]
        req = build_monitor_down_request(n, recent[-1]["started_at"][:16].replace("T", " ") + " UTC", (recent[0].get("errors") or ["unknown error"])[0], errors)
        try:
            self.approvals.submit(req)
        except Exception:  # noqa: BLE001 - telling the owner must never break the scheduler
            log.exception("could not send the 'stock monitor is down' notice")

    def _monitor_recovered(self) -> None:
        """A good run: close any open 'monitor is down' notice so nobody acts on a stale warning."""
        for rec in self.approvals.list("pending", 50):
            if rec.kind == KIND_MONITOR_DOWN:
                self.approvals.cancel(rec.id, "the stock check is working again")

    # ================================================================== on-demand analysis
    def _items(self, item_codes: Optional[list[str]] = None) -> list[ItemInfo]:
        """The ERP's items, or only `item_codes`. An ERP that can read some items without listing every one (the optional `get_items(codes)`, like `get_open_sales_orders` optional
        for the forecast) is asked for just those, so that looking at one item does not read them all."""
        read = getattr(self.erp, "get_items", None)
        if item_codes and read is not None:
            return list(read(list(item_codes)))
        return [i for i in self.erp.list_items() if not item_codes or i.code in item_codes]

    def analyze_one(self, item_code: str) -> tuple[Analysis, dict[str, str]]:
        item = next((i for i in self._items([item_code]) if i.code == item_code), None)
        if item is None:
            raise KeyError(f"Unknown item {item_code!r}")
        policy, sources = self.settings.policy_with_sources(item.code, item.group)
        today = self.today()
        stocks, usages = self._load([(item, policy, sources)], today)
        stock = stocks.get(item.code)
        if stock is None:
            raise KeyError(f"No stock data for {item_code!r}")
        sales = self._sales([(item, policy, sources)], today)
        return analyze_item(item, stock, usages.get(item.code, []), policy, today, self._windows(item, today), sales.get(item.code, ())), sources

    def _windows(self, item: ItemInfo, today: date) -> list[Window]:
        """Calendar events (festivals, promotions) that apply to this item. Empty when the calendar is off or has none."""
        cal = self.settings.calendar()
        return active_windows(cal.events, item.code, item.group, today) if cal.enabled and cal.events else []

    @staticmethod
    def _history_by_warehouses(planned: list[tuple[ItemInfo, Policy, dict[str, str]]]) -> dict[tuple[str, ...], int]:
        """The longest history any planned item asks for, per set of warehouses: the window every item of that set is read for, whichever batch it is in."""
        longest: dict[tuple[str, ...], int] = {}
        for _, p, _ in planned:
            key = tuple(sorted(p.warehouses))
            longest[key] = max(longest.get(key, 0), p.history_days)
        return longest

    def _load(self, planned: list[tuple[ItemInfo, Policy, dict[str, str]]], today: date,
              history_by: Optional[dict[tuple[str, ...], int]] = None) -> tuple[dict[str, Any], dict[str, Any]]:
        """Stock and usage for these items. Items whose policy names different warehouses are fetched separately,
        so a warehouse filter set on one group or item only affects that group or item. `history_by` (from `_history_by_warehouses` over the whole run) keeps the window of
        an item the same however the run is cut into batches; without it the window is the longest among `planned`."""
        by_warehouses: dict[tuple[str, ...], list[tuple[ItemInfo, Policy, dict[str, str]]]] = {}
        for entry in planned:
            by_warehouses.setdefault(tuple(sorted(entry[1].warehouses)), []).append(entry)
        stock: dict[str, Any] = {}
        usage: dict[str, Any] = {}
        for warehouses, entries in by_warehouses.items():
            codes = [it.code for it, _, _ in entries]
            history = (history_by or {}).get(warehouses) or max(p.history_days for _, p, _ in entries)
            wh = list(warehouses) or None
            stock.update(self.erp.get_stock(codes, wh))
            usage.update(self.erp.get_consumption(codes, today - timedelta(days=history), today, wh))
        return stock, usage

    def _sales(self, planned: list[tuple[ItemInfo, Policy, dict[str, str]]], today: date) -> dict[str, Any]:
        """What is still to deliver on open sales orders, per item, for the items whose forecast counts them; nothing when the ERP connection cannot say."""
        read = getattr(self.erp, "get_open_sales_orders", None)
        codes = [it.code for it, p, _ in planned if p.forecast_reorder_point and p.sales_orders_in_forecast]
        if read is None or not codes:
            return {}
        try:
            return read(codes, today + timedelta(days=SALES_HORIZON_DAYS)) or {}
        except Exception as exc:  # noqa: BLE001 - without them the forecast uses usage and seasonality only, which is what it did before
            log.warning("could not read the open sales orders (the forecast uses usage only): %s", _short_error(exc))
            return {}

    def overview(self) -> list[dict[str, Any]]:
        """One row per ERP item for the admin screen: live stock/cover plus the thresholds in force.
        Read-only: nothing is stored or sent."""
        today = self.today()
        rows: list[dict[str, Any]] = []
        planned: list[tuple[ItemInfo, Policy, dict[str, str]]] = []
        for it in self.erp.list_items():
            try:
                policy, sources = self.settings.policy_with_sources(it.code, it.group)
            except SettingsError as exc:
                rows.append(self._overview_row(it, None, None, {}, error=str(exc)))
                continue
            if policy.enabled:
                planned.append((it, policy, sources))
            else:
                rows.append(self._overview_row(it, None, policy, sources))
        if planned:
            stock, usage = self._load(planned, today)
            sales = self._sales(planned, today)
            for it, policy, sources in planned:
                st = stock.get(it.code)
                if st is None:
                    rows.append(self._overview_row(it, None, policy, sources, error="no stock data returned by the ERP"))
                    continue
                try:
                    a = analyze_item(it, st, usage.get(it.code, []), policy, today, self._windows(it, today), sales.get(it.code, ()))
                except Exception as exc:  # noqa: BLE001 - one bad item must not hide the others
                    log.exception("overview analysis failed for %s", it.code)
                    rows.append(self._overview_row(it, None, policy, sources, error=f"analysis failed ({exc})"))
                    continue
                rows.append(self._overview_row(it, a, policy, sources))
        rows.sort(key=lambda r: (-r["severity_rank"], r["cover_days"] if r["cover_days"] is not None else 1e9, r["item_code"]))
        return rows

    def _overview_row(self, it: ItemInfo, a: Optional[Analysis], policy: Optional[Policy], sources: dict[str, str],
                      error: Optional[str] = None) -> dict[str, Any]:
        return {
            "item_code": it.code, "item_name": it.name, "group": it.group, "uom": it.uom,
            "enabled": policy.enabled if policy else None,
            "min_qty": policy.min_qty if policy else None,
            "min_days_cover": policy.min_days_cover if policy else None,
            "sources": sources,
            "item_overrides": self.settings.get_overrides("item", it.code),
            "on_hand": a.on_hand if a else None,
            "reserved": a.reserved if a else None,                        # the part actually subtracted by the policy
            "available": a.available if a else None,
            "position": a.position if a else None,
            "projected": a.projected if a else None,
            "stock_columns": a.breakdown if a else [],
            "by_warehouse": a.warehouses if a else [],                    # where the stock sits: warehouse, on_hand, reserved, incoming, in_transit
            "calendar": [w.to_dict() for w in a.windows] if a else [],
            "stock_subtract": policy.stock_subtract if policy else None,
            "stock_add": policy.stock_add if policy else None,
            "in_transit_stock": policy.in_transit_stock if policy else None,
            "in_transit": a.in_transit if a else None,
            "warehouses": policy.warehouses if policy else None,
            "incoming": a.incoming if a else None,
            "planned": a.planned if a else None,
            "next_arrival": a.next_arrival if a else None,
            "daily_use": a.usage.planning_rate if a and a.usage else None,
            "cover_days": a.cover_days_on_hand if a else None,
            "lead_time_days": a.lead_time_days if a else None,
            "stockout_date": a.stockout_date if a else None,
            "status": a.status if a else ("ERROR" if error else "DISABLED"),
            "severity": SEV_NAMES[a.severity] if a else None,
            "severity_rank": a.severity if a else -1,
            "headline": (a.reasons[0].text if a and a.reasons else ""),
            "error": error,
        }

    # ================================================================== reconcile with history of alerts
    def _reconcile(self, a: Analysis, sources: dict[str, str], run_id: str, dry_run: bool, budget: int) -> dict[str, Any]:
        now = self.clock()
        base = {"item_code": a.item_code, "status": a.status, "severity": SEV_NAMES[a.severity]}

        if a.status == STATUS_OK:
            open_alerts = self.alerts.open_for_item(a.item_code)
            if open_alerts and not dry_run:
                for al in open_alerts:
                    self._close(al, "resolved", "stock is back above all thresholds")
            return {**base, "action": "resolved" if open_alerts else "ok", "alert_ids": [al.id for al in open_alerts]}

        prev = self.alerts.latest_for_item(a.item_code)
        if prev:
            escalated = a.severity > prev.severity
            reason = None
            if prev.status == "awaiting":
                reason = "pending_approval"
            elif prev.status == "snoozed" and prev.snoozed_until and now < prev.snoozed_until:
                reason = "snoozed"
            elif prev.status == "dismissed" and prev.suppress_until and now < prev.suppress_until:
                reason = "ignored_by_owner"
            elif prev.status == "actioned" and prev.cooldown_until and now < prev.cooldown_until:
                reason = "ordered_recently"
                escalated = a.severity == SEV_CRITICAL and prev.severity < SEV_CRITICAL
            if reason and not escalated:
                return {**base, "action": f"skipped:{reason}", "alert_id": prev.id}

        if budget <= 0:
            return {**base, "action": "skipped:run_cap_reached"}
        if dry_run:
            return {**base, "action": "would_alert", "headline": a.reasons[0].text if a.reasons else ""}

        # Replace anything still open for this item, then create the new alert + approval atomically.
        for old in self.alerts.open_for_item(a.item_code):
            self._close(old, "superseded", f"replaced by a newer {SEV_NAMES[a.severity]} alert")
        with self.transaction():
            alert = self.alerts.create(a.item_code, a.kind, a.severity, a.to_dict(), "", run_id)
            req = self._with_extras(build_request(a, alert.id, sources), a)
            self.alerts.set_reasoning(alert.id, render_plain(req))
            record = self.approvals.submit(req, deliver=False)
            self.alerts.set_approval(alert.id, record.id)
        self.approvals.deliver(record)          # network I/O after the database commit
        return {**base, "action": "new_alert", "alert_id": alert.id, "approval_id": record.id}

    # ================================================================== stock alerts: information only, before any request
    def _notify(self, a: Analysis, policy: Policy, run_id: str, dry_run: bool, budget: int, request_follows: bool) -> Optional[dict[str, Any]]:
        """Send the alert for this item if one is due: the first time it is low, when it gets worse (low -> urgent), or when it is
        still low after `stock_alert_repeat_days`. Not while the owner has snoozed it."""
        system = self.settings.system()
        if not system.stock_alerts or a.status == STATUS_OK:
            return None
        now, level = self.clock(), alert_level(a)
        base = {"item_code": a.item_code, "status": a.status, "severity": SEV_NAMES[a.severity], "level": level}
        prev = self.notices.latest_active(a.item_code)
        if prev is not None and LEVELS[level] <= LEVELS[prev.level]:
            if prev.status == "snoozed" and prev.snoozed_until and now < prev.snoozed_until:
                return {**base, "action": "skipped:alert_snoozed"}
            if now - prev.created_at < timedelta(days=system.stock_alert_repeat_days):
                return None                                        # already told about this, no need to repeat yet
        if budget <= 0:
            return {**base, "action": "skipped:alert_cap_reached"}
        if dry_run:
            return {**base, "action": "would_notify"}
        for old in self.notices.active_for_item(a.item_code):      # the new alert replaces an older one that is still waiting (its request first, then its status: see `_close`)
            if old.approval_id:
                self.approvals.cancel(old.approval_id, f"replaced by a newer {level.lower()} stock alert")
            self.notices.set_status(old.id, "superseded")
        with self.transaction():
            notice = self.notices.create(a.item_code, level, None)
            req = self._with_extras(build_alert_request(a, notice.id, level, request_follows, int(a.policy.get("snooze_days", 3))), a)
            record = self.approvals.submit(req, deliver=False)
            self.notices.set_approval(notice.id, record.id)
        self.approvals.deliver(record)                             # network I/O after the database commit
        return {**base, "action": "new_notice", "notice_id": notice.id, "approval_id": record.id}

    def end_quiet_period(self, item_code: str) -> None:
        """An order for this item was cancelled: nothing is on its way, so the quiet period after 'Create order' ends and the next check can tell about a shortage."""
        prev = self.alerts.latest_for_item(item_code)
        now = self.clock()
        if prev is not None and prev.status == "actioned" and prev.cooldown_until and now < prev.cooldown_until:
            self.alerts.update_status(prev.id, "actioned", "the order was cancelled, so the quiet period ended", cooldown_until=now)
            self._clear_notices(item_code)

    def _end_quiet_period_if_order_done(self, a: Analysis, dry_run: bool) -> Optional[dict[str, Any]]:
        """After "Create order" the item is left alone for `reorder_cooldown_days`, so it is not asked about again while its order is on the way. If that order has since been
        received (or closed, or cancelled) and the item is short again, nothing is on the way any more: the quiet period ends and the old stock alert no longer counts, so the new
        shortage is told at once. Without this a new sales order that empties the stock again goes unnoticed for days."""
        prev = self.alerts.latest_for_item(a.item_code)
        now = self.clock()
        if self.order_settled is None or prev is None or prev.status != "actioned" or not prev.cooldown_until or now >= prev.cooldown_until:
            return None
        if not self.order_settled(a.item_code, prev.created_at):
            return None
        if not dry_run:
            self.alerts.update_status(prev.id, "actioned", "the order was received or closed, so the quiet period ended", cooldown_until=now)
            self._clear_notices(a.item_code)
        return {"item_code": a.item_code, "status": a.status, "action": "would_end_quiet_period" if dry_run else "quiet_period_ended",
                "why": "the order made after your last 'Create order' is received or closed, and the item is short again"}

    def _clear_notices(self, item_code: str) -> None:
        for n in self.notices.active_for_item(item_code):
            if n.approval_id:                                      # its request first, then its status: see `_close`
                self.approvals.cancel(n.approval_id, "stock is back above your limits")
            self.notices.set_status(n.id, "cleared")

    def _on_notice_decision(self, record: ApprovalRecord, decision: Decision) -> None:
        notice = self.notices.by_approval(record.id)
        if notice is None:
            return
        if decision.action == "ack":
            self.notices.set_status(notice.id, "acknowledged")
        elif decision.action == "snooze":
            days = _bounded_int(decision.params.get("days"), 1, 60, int(record.request.facts.get("snooze_days", 3)))
            self.notices.set_status(notice.id, "snoozed", snoozed_until=self.clock() + timedelta(days=days))

    def _close(self, alert: Alert, status: str, note: str) -> None:
        # The request is cancelled FIRST and the alert's status set after. A person's answer takes the request's row and then the alert's (`decide`, then `_on_decision`); a run that took the
        # two the other way round could deadlock with that person, one holding each row and asking for the other. (An awaiting alert is set to "cancelled" by the cancellation itself,
        # and to `status` right after: the same end state as before.)
        if alert.status == "awaiting" and alert.approval_id:
            self.approvals.cancel(alert.approval_id, note)
        self.alerts.update_status(alert.id, status, note)

    # ================================================================== approval outcomes
    def _on_decision(self, record: ApprovalRecord, decision: Decision) -> None:
        alert = self.alerts.by_approval(record.id)
        if alert is None:
            return
        pol = alert.analysis.get("policy", {})
        now = self.clock()
        if decision.action == "approve":
            cooldown = now + timedelta(days=int(pol.get("reorder_cooldown_days", 3)))
            self.alerts.update_status(alert.id, "actioned", f"approved by {decision.actor_id} via {decision.channel}", cooldown_until=cooldown)
            payload = self._refreshed(self._work_order_payload(alert, record, decision), alert)
            self.workorders.emit(REORDER_NEEDED, payload, source=f"inventory_alert:{alert.id}")
        elif decision.action == "snooze":
            days = _bounded_int(decision.params.get("days"), 1, 60, int(pol.get("snooze_days", 3)))
            self.alerts.update_status(alert.id, "snoozed", f"snoozed {days}d by {decision.actor_id}", snoozed_until=now + timedelta(days=days))
        elif decision.action == "dismiss":
            days = int(pol.get("dismiss_days", 7))
            self.alerts.update_status(alert.id, "dismissed", f"ignored by {decision.actor_id}", suppress_until=now + timedelta(days=days))

    def _on_expired(self, record: ApprovalRecord) -> None:
        alert = self.alerts.by_approval(record.id)
        if alert and alert.status == "awaiting":
            self.alerts.update_status(alert.id, "expired", "no answer before the request expired")

    def _on_cancelled(self, record: ApprovalRecord) -> None:
        alert = self.alerts.by_approval(record.id)
        if alert and alert.status == "awaiting":
            self.alerts.update_status(alert.id, "cancelled", record.status_note or "cancelled")

    STOCK_FIGURES = ("calendar", "on_hand", "reserved", "available", "incoming", "planned", "position", "daily_rate", "forecast_rate", "lead_time_days", "cover_days",
                     "stockout_date", "latest_order_date")
    SIZING_FIGURES = ("suggested_qty", "suggestion_type", "suggested_qty_basis")

    def _refreshed(self, payload: dict[str, Any], alert: Alert) -> dict[str, Any]:
        """The stock and usage figures an alert carries are from the day it was raised, and the owner may approve it days later (stock arrives, sales change).
        The order is sized from today's figures instead. If today's figures no longer call for an order, the old quantity is kept but the request shows today's stock
        and says so, so the owner can decide. Unchanged when the ERP cannot be read just now."""
        try:
            now_a, _src = self.analyze_one(payload["item_code"])
        except Exception as exc:  # noqa: BLE001 - fall back to the figures the suggestion was made with
            log.warning("could not refresh the figures of %s when its reorder was approved: %s", payload["item_code"], exc)
            return payload
        fresh = on_demand_payload(now_a, "", None)
        out = {**payload, **{k: fresh[k] for k in self.STOCK_FIGURES}, "analysis": fresh["analysis"], "figures_refreshed": True}
        was, when = payload.get("suggested_qty"), f"{alert.created_at:%d %b}"
        if fresh.get("suggested_qty"):
            out.update({k: fresh[k] for k in self.SIZING_FIGURES})
            out["reasons"] = [r["text"] for r in fresh["analysis"].get("reasons", [])] or payload["reasons"]
            if was and abs(float(was) - float(out["suggested_qty"])) > 0.05 * float(was):
                out["suggested_qty_basis"] = f"{fresh['suggested_qty_basis']}. The reorder suggestion of {when} said {_n(was)}; the figures were read again when you approved it"
        else:
            out["reasons"] = [f"Today's figures no longer show a shortage. The reorder was suggested on {when} because: {(payload.get('reasons') or ['stock was low'])[0]}"]
            out["suggested_qty_basis"] = f"{payload.get('suggested_qty_basis') or 'the suggested quantity'} (as suggested on {when}; check the stock position before you approve)"
        return out

    @staticmethod
    def _work_order_payload(alert: Alert, record: ApprovalRecord, decision: Decision) -> dict[str, Any]:
        a = alert.analysis
        usage = a.get("usage") or {}
        return {
            "item_code": a["item_code"], "item_name": a["item_name"], "uom": a["uom"],
            "item_group": a.get("item_group"),
            "kind": alert.kind, "severity": SEV_NAMES[alert.severity],
            "suggested_qty": a.get("suggested_qty"), "suggestion_type": a.get("suggestion_type"), "suggested_qty_basis": a.get("suggested_qty_basis"),
            "calendar": a.get("windows") or [],
            "on_hand": a["on_hand"], "reserved": a.get("reserved"), "available": a.get("available"),
            "incoming": a["incoming"], "planned": a.get("planned"), "position": a["position"],
            "in_transit": a.get("in_transit"), "in_transit_mode": (a.get("policy") or {}).get("in_transit_stock"),
            "daily_rate": usage.get("planning_rate"), "forecast_rate": a.get("forecast_rate"),
            "lead_time_days": a["lead_time_days"], "cover_days": a.get("cover_days_on_hand"),
            "stockout_date": a.get("stockout_date"), "latest_order_date": a.get("latest_order_date"),
            "reasons": [r["text"] for r in a.get("reasons", [])],
            "alert_id": alert.id, "approval_id": record.id,
            "approved_by": decision.actor_id, "approved_via": decision.channel,
            "approved_at": decision.decided_at.isoformat(),
            "analysis": a,
        }


def on_demand_payload(a: Analysis, who: str, qty: Optional[float] = None, reason: Optional[str] = None) -> dict[str, Any]:
    """The work order for a purchase somebody ASKED for ("create a purchase order for X"): the same fields as one started by a low-stock alert, so the vendor
    comparison and the request look the same. The quantity is the one asked for, else the one the analysis suggests."""
    d = a.to_dict()
    usage = d.get("usage") or {}
    return {
        "item_code": d["item_code"], "item_name": d["item_name"], "uom": d["uom"], "item_group": d.get("item_group"),
        "kind": "ON_DEMAND", "severity": "INFO",
        "suggested_qty": qty or d.get("suggested_qty"), "suggestion_type": d.get("suggestion_type"),
        "suggested_qty_basis": "the quantity you asked for" if qty else d.get("suggested_qty_basis"),
        "calendar": d.get("windows") or [],
        "on_hand": d["on_hand"], "reserved": d.get("reserved"), "available": d.get("available"), "incoming": d["incoming"], "planned": d.get("planned"), "position": d["position"],
        "in_transit": d.get("in_transit"), "in_transit_mode": (d.get("policy") or {}).get("in_transit_stock"),
        "daily_rate": usage.get("planning_rate"), "forecast_rate": d.get("forecast_rate"), "lead_time_days": d["lead_time_days"], "cover_days": d.get("cover_days_on_hand"),
        "stockout_date": d.get("stockout_date"), "latest_order_date": d.get("latest_order_date"),
        "reasons": [reason or f"{who} asked for a purchase order for this item."],
        "alert_id": None, "approval_id": None, "approved_by": who, "approved_via": "chat", "approved_at": None, "analysis": d,
    }


def _bounded_int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default
