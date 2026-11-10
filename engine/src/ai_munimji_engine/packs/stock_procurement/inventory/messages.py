"""Build the owner-facing explanation from an Analysis.

Everything here is template text filled from stored numbers - nothing is generated freehand, so the
explanation can never disagree with the data. (An LLM could later *rephrase* these sections; the
facts would still come from here.)
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from ai_munimji_engine.core.approvals.models import ACTION_DETAILS, ApprovalOption, ApprovalRequest, Section
from ai_munimji_engine.packs.stock_procurement.erp import ItemInfo, column_phrase
from .analysis import SEV_CRITICAL, SEV_NAMES, STATUS_BREACH, STATUS_EARLY, Analysis, _d, _n, where_stock_is
from .events import describe

KIND = "inventory.reorder"
KIND_MONITOR_DOWN = "inventory.monitor_down"
KIND_ALERT = "inventory.alert"
KIND_TRANSFER = "inventory.transfer"


def build_request(a: Analysis, alert_id: str, policy_sources: Optional[dict[str, str]] = None) -> ApprovalRequest:
    u = a.uom
    sev = SEV_NAMES[a.severity]
    stats = a.usage
    assert stats is not None  # type narrowing: the analysis always fills usage before this runs  # nosec B101

    if a.status == STATUS_EARLY:
        title = f"Heads-up: {a.item_name} ({a.item_code}) may need reordering soon"
        headline = (
            f"{a.item_name}: still above thresholds, but expected to hit the order point in ~{_n(a.days_to_trigger)} days "
            f"based on past usage."
        )
    else:
        title = f"Reorder suggested: {a.item_name} ({a.item_code})"
        headline = f"{a.item_name}: {_n(a.on_hand)} {u} on hand, about {_n(a.cover_days_on_hand)} days left."

    # ---- 1. situation --------------------------------------------------------------------------
    situation = _situation(a)

    # ---- 2. why am I telling you -------------------------------------------------------------------
    why = [r.text for r in a.reasons] or ["No rule tripped."]
    why.extend(a.drivers)

    # ---- 3. how I estimated usage ----------------------------------------------------------------------
    est = []
    if stats.method == "blend":
        est.append(
            f"Planned usage {_n(stats.planning_rate, 2)}/day = {_n(stats.recent_weight * 100, 0)}% x last {stats.recent_days} days "
            f"({_n(stats.recent_rate, 2)}/day) + {_n((1 - stats.recent_weight) * 100, 0)}% x last {stats.base_days} days ({_n(stats.base_rate, 2)}/day)."
        )
    elif stats.method == "recent":
        est.append(f"Planned usage {_n(stats.planning_rate, 2)}/day = average of the last {stats.recent_days} days.")
    else:
        est.append(f"Planned usage {_n(stats.planning_rate, 2)}/day = average of the last {stats.base_days} days.")
    if a.forecast_rate and abs(a.forecast_rate - stats.planning_rate) > 1e-6:
        est.append(f"Forecast for the coming weeks {_n(a.forecast_rate, 2)}/day after the seasonal adjustment.")
    if stats.high_rate and a.high_rate_cover_days is not None and stats.high_rate > max(stats.planning_rate, a.forecast_rate) * 1.05:
        est.append(
            f"In a busy week (top 20% of weeks) usage is ~{_n(stats.high_rate, 2)}/day, which would shorten cover to ~{_n(a.high_rate_cover_days)} days."
        )

    # ---- 4. suggestion ----------------------------------------------------------------------------------------
    suggestion = []
    if a.suggested_qty and a.suggestion_type == "bridge":
        suggestion.append(f"Option 1: ask the supplier to expedite the open order. Option 2: buy a bridge quantity of about {_n(a.suggested_qty)} {u}.")
        suggestion.append(f"How: {a.suggested_qty_basis}.")
        suggestion.append("Vendor and final price are chosen in the next (procurement) step - nothing is ordered by approving this.")
    elif a.suggested_qty:
        suggestion.append(f"Suggested order: about {_n(a.suggested_qty)} {u}.")
        suggestion.append(f"How: {a.suggested_qty_basis}.")
        suggestion.append("Vendor and final price are chosen in the next (procurement) step - nothing is ordered by approving this.")
    else:
        suggestion.append(a.suggested_qty_basis or "Order quantity to be decided by the buyer.")

    # ---- 5. confidence ------------------------------------------------------------------------------------------
    conf = [f"{a.confidence}: " + "; ".join(a.confidence_why) + "."]
    conf.extend(a.notes)

    sections = [
        Section(heading="Situation", lines=situation),
        Section(heading="Why you are seeing this", lines=why),
        Section(heading="How usage was estimated", lines=est),
        Section(heading="Suggested action", lines=suggestion),
        Section(heading="Confidence", lines=conf),
    ]

    snooze = a.policy.get("snooze_days", 3)
    dismiss = a.policy.get("dismiss_days", 7)
    options = [
        ApprovalOption(action="approve", label="Start reorder", style="primary",
                       description="Hand this to procurement to pick a vendor and draft the PO"),
        ApprovalOption(action="snooze", label=f"Remind in {snooze}d",
                       description=f"Ask again in {snooze} days"),
        ApprovalOption(action="dismiss", label="Ignore", style="danger",
                       description=f"Do not raise this again for {dismiss} days unless it gets worse"),
        ApprovalOption(action=ACTION_DETAILS, label="Details", terminal=False,
                       description="Show every number and the settings used"),
    ]

    return ApprovalRequest(
        kind=KIND, title=title, headline=headline, severity=sev, sections=sections,
        details=build_details(a, policy_sources), options=options,
        ref_type="inventory_alert", ref_id=alert_id,
        facts={
            "item_code": a.item_code, "kind": a.kind, "on_hand": a.on_hand, "cover_days": a.cover_days_on_hand,
            "suggested_qty": a.suggested_qty, "lead_time_days": a.lead_time_days,
        },
    )


def _situation(a: Analysis) -> list[str]:
    u, stats = a.uom, a.usage
    assert stats is not None  # type narrowing: the analysis always fills usage before this runs  # nosec B101
    situation = []
    stock_line = f"On hand: {_n(a.on_hand)} {u}"
    held = a.on_hand - a.available
    if held > 0:
        stock_line += f" ({_n(held)} reserved, {_n(a.available)} available)"
    if a.in_transit and (a.policy or {}).get("in_transit_stock") == "shelf":
        stock_line += f" (including {_n(a.in_transit)} {u} {column_phrase('in_transit')}, counted as on hand as you chose)"
    coming = []
    for b in a.breakdown:
        if b["use"] != "add" or not b["value"]:
            continue
        text = f"{_n(b['value'])} {u} {column_phrase(b['column'])}"
        if b["column"] == "ordered_qty":
            text += f", arriving {_d(a.next_arrival)}" if a.next_arrival else ", arrival date unknown"
        coming.append(text)
    if coming:
        stock_line += " (+ " + "; + ".join(coming) + ")"
    situation.append(stock_line + ".")
    split = where_stock_is(a)
    if len(split) > 1:                                      # one warehouse says nothing the line above does not
        situation.append("By warehouse: " + "; ".join(split) + ".")
    if a.cover_days_on_hand is not None:
        usage_line = f"At about {_n(stats.planning_rate, 2)} {u}/day this lasts "
        situation.append(usage_line + (f"~{_n(a.cover_days_on_hand)} days - stock-out around {_d(a.stockout_date)}." if a.stockout_date else "more than 10 years."))
    if a.replenish_in_days is not None:
        src = "open order due" if a.next_arrival else f"supplier lead time {_n(a.lead_time_days)} days ({a.lead_time_source})"
        line = f"Replenishment: {src} -> new stock around {_d(a.replenish_date)}."
        if a.gap_days is not None:
            if a.gap_days > 0:
                line += f" That is ~{_n(a.gap_days)} days AFTER you run out."
            else:
                line += f" That is ~{_n(-a.gap_days)} days before you run out."
        situation.append(line)
    if a.latest_order_date and a.cover_days_on_hand is not None:
        latest = date.fromisoformat(a.latest_order_date)
        today = date.fromisoformat(a.as_of)
        if latest < today:
            situation.append(f"Latest safe order date was {_d(a.latest_order_date)} - already {(today - latest).days} day(s) overdue.")
        else:
            situation.append(f"Latest safe order date: {_d(a.latest_order_date)}.")
    return situation


def alert_level(a: Analysis) -> str:
    """URGENT when stock is needed urgently (out of stock, or new stock cannot arrive before it runs out); otherwise LOW."""
    return "URGENT" if a.status == STATUS_BREACH and a.severity >= SEV_CRITICAL else "LOW"


def build_alert_request(a: Analysis, notice_id: str, level: str, request_follows: bool, snooze_days: int = 3) -> ApprovalRequest:
    """An information-only stock alert: what is wrong, why, and what happens next. The only choices are to acknowledge or be reminded."""
    u, name = a.uom, a.item_name
    out = a.on_hand <= 0 or (a.position is not None and a.position <= 0)
    if level == "URGENT":
        title = f"URGENT: {name} ({a.item_code}) is out of stock" if out else f"URGENT: {name} ({a.item_code}) is needed urgently"
        headline = (f"{name} is out of stock." if out else
                    f"{name}: {_n(a.on_hand)} {u} left, about {_n(a.cover_days_on_hand)} days of stock, but new stock takes about {_n(a.lead_time_days)} days to arrive.")
        severity = "CRITICAL"
    elif a.status == STATUS_EARLY:
        title = f"Low stock soon: {name} ({a.item_code})"
        headline = f"{name}: {_n(a.on_hand)} {u} left. At the current pace it reaches your reorder point in about {_n(a.days_to_trigger)} days."
        severity = "EARLY_WARNING"
    else:
        title = f"Low stock: {name} ({a.item_code})"
        headline = f"{name}: {_n(a.on_hand)} {u} left, enough for about {_n(a.cover_days_on_hand)} days. That is below your reorder limit."
        severity = "HIGH"
    why = [r.text for r in a.reasons] or ["A rule you set was tripped."]
    why.extend(a.drivers)
    nxt = (["A request to reorder follows this alert, so you can decide what to do."] if request_follows
           else ["No action is needed yet. If stock keeps falling you will be asked to reorder."])
    nxt.append("This alert is information only: nothing is ordered or changed.")
    return ApprovalRequest(
        kind=KIND_ALERT, title=title, headline=headline, severity=severity,
        sections=[Section(heading="Situation", lines=_situation(a)), Section(heading="Why you are seeing this", lines=why), Section(heading="What happens next", lines=nxt)],
        details=build_details(a, None),
        options=[ApprovalOption(action="ack", label="Got it", style="primary", description="I have seen this"),
                 ApprovalOption(action="snooze", label=f"Remind in {snooze_days}d", description=f"Remind me again in {snooze_days} days if it is still low"),
                 ApprovalOption(action=ACTION_DETAILS, label="Details", terminal=False, description="Show every number")],
        ref_type="stock_notice", ref_id=notice_id,
        facts={"item_code": a.item_code, "level": level, "on_hand": a.on_hand, "cover_days": a.cover_days_on_hand, "notice": True, "snooze_days": snooze_days},
    )


def build_details(a: Analysis, sources: Optional[dict[str, str]] = None) -> list[Section]:
    """Every figure behind the recommendation, plus which admin setting each rule came from."""
    stats = a.usage
    assert stats is not None  # type narrowing: the analysis always fills usage before this runs  # nosec B101
    numbers = [
        f"As of {_d(a.as_of)}. Available {_n(a.available)} (on hand minus what you subtract), position {_n(a.position)} (available plus what you add) {a.uom}."
        + (f" Warehouses: {', '.join(a.policy['warehouses'])}." if a.policy.get("warehouses") else " All warehouses."),
        "Stock columns: " + "; ".join(
            f"{b['label']} {_n(b['value'])}" + ("" if b["use"] == "start" else f" ({b['use']})") for b in a.breakdown if b["use"] != "not used" or b["value"]) + ".",
        f"Usage: last {stats.recent_days}d {_n(stats.recent_rate, 2)}/day, last {stats.base_days}d {_n(stats.base_rate, 2)}/day "
        f"(history available: {stats.data_days} days, {stats.nonzero_days} days with movement).",
        f"Lead time {_n(a.lead_time_days)} days ({a.lead_time_source}); safety {_n(a.safety_days)} days.",
    ]
    if stats.trend_pct is not None:
        numbers.append(f"Trend vs base: {stats.trend_pct:+.0f}%.")
    if a.windows:
        numbers.append("Calendar events applied to this item: " + describe(a.windows) + ".")
    if a.seasonality:
        se = a.seasonality
        numbers.append("Seasonality: " + (f"last-year ratio x{_n(se.ratio, 2)} ({se.note})" if se.ratio is not None else se.note) + ".")

    rule_lines = []
    for r in a.rules:
        if not r["enabled"]:
            rule_lines.append(f"{r['rule']}: off")
            continue
        state = "TRIPPED" if r["tripped"] else "ok"
        rule_lines.append(f"{r['rule']}: {r['label']} - value {_n(r['value'], 1)} vs threshold {_n(r['threshold'], 1)} -> {state}")

    out = [Section(heading="Numbers", lines=numbers), Section(heading="Rules checked", lines=rule_lines)]
    if sources:
        changed = [f"{k} set by {v}" for k, v in sorted(sources.items()) if k in {"min_qty", "min_days_cover", "safety_stock_days",
                                                                                   "lead_time_days", "use_lead_time_rule", "lookahead_days",
                                                                                   "stock_subtract", "stock_add", "in_transit_stock", "warehouses"}]
        if changed:
            out.append(Section(heading="Where the thresholds come from", lines=changed))
    return out


def build_transfer_request(transfer_id: str, item: ItemInfo, qty: float, source: str, destination: str, held_source: dict[str, float], held_destination: dict[str, float],
                           where: list[str], reason: str = "") -> ApprovalRequest:
    """Ask a person to move stock from one warehouse to another. `held_source` / `held_destination` are the warehouse's own figures: on_hand and reserved (promised to
    customers). Answering Create transfer only makes a DRAFT in the ERP: no stock moves until someone submits it there."""
    u, name = item.uom, item.name
    src_left, dst_after = held_source["on_hand"] - qty, held_destination["on_hand"] + qty
    promised = held_source.get("reserved", 0.0)
    stock = [f"{source}: {_n(held_source['on_hand'])} {u}" + (f" ({_n(promised)} promised to customers)" if promised > 0 else ""),
             f"{destination}: {_n(held_destination['on_hand'])} {u}"]
    after = [f"{source} would hold {_n(src_left)} {u} and {destination} {_n(dst_after)} {u}. The total you hold does not change."]
    if src_left <= 0:
        after.append(f"That is everything {source} has of it.")
    if promised > 0 and src_left < promised:
        after.append(f"Careful: {source} would then hold less than the {_n(promised)} {u} promised to customers from there.")
    sections = [Section(heading="What happens", lines=[f"Create a draft stock transfer in ERPNext: {_n(qty)} {u} of {name} from {source} to {destination}.",
                                                       "It is only a draft. No stock moves until someone opens it in ERPNext and submits it."]),
                Section(heading="Stock now", lines=stock), Section(heading="After the transfer", lines=after)]
    if reason:
        sections.insert(0, Section(heading="Why", lines=[reason]))
    return ApprovalRequest(
        kind=KIND_TRANSFER, title=f"Move {_n(qty)} {u} of {name} ({item.code}): {source} to {destination}",
        headline=f"Move {_n(qty)} {u} of {name} from {source} to {destination}.", severity="INFO", sections=sections,
        details=[Section(heading="Where it is now, warehouse by warehouse", lines=where or ["ERPNext did not say."])],
        options=[ApprovalOption(action="approve", label="Create transfer", style="primary", description=f"Make a draft transfer of {_n(qty)} {u} in ERPNext"),
                 ApprovalOption(action="dismiss", label="Don't move it", style="danger", description="Move nothing"),
                 ApprovalOption(action=ACTION_DETAILS, label="Details", terminal=False, description="Show where the stock is now")],
        ref_type="stock_transfer", ref_id=transfer_id,
        facts={"item_code": item.code, "qty": qty, "transfer": transfer_id, "from_warehouse": source, "to_warehouse": destination})


def build_monitor_down_request(failures: int, since: str, last_error: str, recent_errors: list[str]) -> ApprovalRequest:
    """Tell the owner the stock check itself is not working, so silence from the monitor does not look like 'all is well'."""
    return ApprovalRequest(
        kind=KIND_MONITOR_DOWN, title="Stock monitor cannot read ERPNext",
        headline=f"The stock check has failed {failures} runs in a row (since {since}). No stock alerts are being raised.",
        severity="HIGH",
        sections=[
            Section(heading="What happened", lines=[f"The last {failures} scheduled stock checks could not finish. Latest error: {last_error}"]),
            Section(heading="What this means", lines=["Nothing is wrong with your stock figures that we know of, but they are not being watched: a shortage would not be reported until the connection is back."]),
            Section(heading="What to do", lines=["Check that ERPNext is running and reachable from this server, and that the agent's API key and secret are still valid. The check retries on its own at the next scheduled run."]),
        ],
        details=[Section(heading="Recent errors", lines=recent_errors or [last_error])],
        options=[
            ApprovalOption(action="ack", label="Got it", style="primary", description="I will look into it"),
            ApprovalOption(action=ACTION_DETAILS, label="Details", terminal=False, description="Show the recent errors"),
        ],
        ref_type="inventory_monitor", ref_id="monitor", facts={"consecutive_failures": failures, "since": since},
    )
