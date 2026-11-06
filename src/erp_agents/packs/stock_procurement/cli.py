"""The stock pack's command-line commands: the demo, the inventory check, the analysis of one item, the thresholds, the alerts and the vendor selector, and the agents' brief,
second opinion and question.

Each command starts with `pack_app`, which builds the application and says so plainly when this pack is switched off.
"""
from __future__ import annotations

import json
from typing import Any

from ai_munimji_engine.core.approvals.render import render_plain
from erp_agents.core.cli import CLI_ACTOR, pack_app, print_agent_result
from ai_munimji_engine.core.packs import CliCommand
from ai_munimji_engine.core.settings_store import SettingsError
from .agents.purchase_agent import review_text
from ai_munimji_engine.packs.stock_procurement.inventory.analysis import SEV_NAMES, Analysis

NAME = "stock_procurement"


def _parse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _print_analysis(a: Analysis) -> None:
    u = a.usage
    print(f"{a.item_code}  {a.item_name}   status={a.status} severity={SEV_NAMES[a.severity]}")
    print(f"  on hand {a.on_hand:g} {a.uom} | on order {a.incoming:g} | usage {u.planning_rate:.2f}/day (recent {u.recent_rate:.2f}, base {u.base_rate:.2f}) | forecast {a.forecast_rate:.2f}/day")
    if a.cover_days_on_hand is not None:
        print(f"  cover {a.cover_days_on_hand:.1f} days | stock-out {a.stockout_date} | lead time {a.lead_time_days:g}d ({a.lead_time_source})")
    for r in a.reasons:
        print(f"  - {r.text}")
    for d in a.drivers:
        print(f"  * {d}")
    for n in a.notes:
        print(f"  ! {n}")


def cmd_demo(args) -> int:
    import copy
    from erp_agents.core.config import DEFAULT_CONFIG
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["database"]["path"] = ":memory:"
    from erp_agents.core.bootstrap import build_app
    app = build_app(config=cfg)
    app.channels["console"].writer = lambda s: print(s) if args.verbose else None
    report = app.inventory.run()
    print(f"\nChecked {report.checked} items on the demo ERP: {report.ok} fine, {report.early_warnings} early warnings, {report.breaches} threshold breaches.\n")
    for act in report.actions:
        print(f"  {act['item_code']:<10} {act['status']:<14} {act['severity']:<14} -> {act['action']}")
    pending = app.approvals.list(status="pending")
    if not pending:
        return 0
    if args.interactive:
        for rec in sorted(pending, key=lambda r: r.request.facts.get("item_code", "")):
            print("\n" + "=" * 72 + "\n" + render_plain(rec.request) + "\n")
            while True:
                choice = input("[a]pprove  [s]nooze  [i]gnore  [d]etails  [enter]=skip > ").strip().lower()
                action = {"a": "approve", "s": "snooze", "i": "dismiss", "d": "details"}.get(choice)
                if not choice:
                    break
                if not action:
                    continue
                res = app.approvals.decide(rec.id, action, CLI_ACTOR, "cli")
                if res.status == "info":
                    print(render_plain(rec.request, sections=res.info_sections, with_title=False))
                    continue
                print(res.message)
                break
        print("\nWork orders handed to procurement:")
        for wo in app.workorders.list():
            p = wo["payload"]
            print(f"  {wo['type']}: {p['item_code']} ~{p['suggested_qty']:g} {p['uom']} ({p['severity']})")
    else:
        first = sorted(pending, key=lambda r: -["INFO", "EARLY_WARNING", "HIGH", "CRITICAL"].index(r.request.severity))[0]
        print("\nExample approval message (what the owner receives):\n" + "=" * 72)
        print(render_plain(first.request))
        print("=" * 72 + "\nRun with --interactive to answer the requests yourself, or --verbose to print all of them.")
    return 0


def cmd_run_once(args) -> int:
    app = pack_app(args, NAME)
    if app is None:
        return 1
    report = app.inventory.run(dry_run=args.dry_run, item_codes=args.item or None)
    mode = "DRY RUN (nothing sent)" if args.dry_run else "LIVE"
    print(f"{mode}: checked {report.checked}, ok {report.ok}, early warnings {report.early_warnings}, breaches {report.breaches}, "
          f"stock alerts {report.new_notices}, new reorder requests {report.new_alerts}, skipped {report.skipped}, resolved {report.resolved}")
    for act in report.actions:
        if act["action"] != "ok":
            print(f"  {act['item_code']:<14} {act['severity']:<14} {act['action']}")
    for err in report.errors:
        print(f"  ERROR {err}")
    return 1 if report.errors else 0


def cmd_analyze(args) -> int:
    app = pack_app(args, NAME)
    if app is None:
        return 1
    try:
        a, sources = app.inventory.analyze_one(args.item)
    except KeyError as exc:
        print(exc)
        return 1
    _print_analysis(a)
    if sources:
        print("  settings in force: " + ", ".join(f"{k} ({v})" for k, v in sorted(sources.items())))
    return 0


def cmd_policy(args) -> int:
    app = pack_app(args, NAME)
    if app is None:
        return 1
    key = "*" if args.scope == "global" else args.key
    if args.scope != "global" and not key:
        print("--key is required for group/item scope")
        return 2
    try:
        if args.action == "show":
            item = key if args.scope == "item" else None
            group = key if args.scope == "group" else None
            policy, sources = app.settings.policy_with_sources(item, group)
            print("overrides:", json.dumps(app.settings.get_overrides(args.scope, key), indent=2))
            for k, v in policy.model_dump().items():
                print(f"  {k:<22} {v!s:<12} {('<- ' + sources[k]) if k in sources else ''}")
        elif args.action == "set":
            changes = dict(kv.split("=", 1) for kv in args.values)
            app.settings.update_policy(CLI_ACTOR, args.scope, key, {k: _parse_value(v) for k, v in changes.items()})
            print("saved")
        elif args.action == "clear":
            app.settings.update_policy(CLI_ACTOR, args.scope, key, clear=args.values)
            print("cleared (now inherited)")
        elif args.action == "reset":
            app.settings.reset_scope(CLI_ACTOR, args.scope, key)
            print("all overrides removed for this scope")
        elif args.action == "audit":
            for r in app.settings.audit(30, args.scope if args.scope != "global" else None, key if args.scope != "global" else None):
                print(f"{r['ts']}  {r['actor']:<8} {r['scope_type']}:{r['scope_key']}  {r['field']}: {r['old']} -> {r['new']}")
    except (SettingsError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


def cmd_alerts(args) -> int:
    app = pack_app(args, NAME)
    if app is None:
        return 1
    for al in app.alerts.list(args.status, args.item):
        print(f"{al.id}  {al.created_at:%Y-%m-%d %H:%M}  {al.item_code:<12} {al.kind:<14} {SEV_NAMES[al.severity]:<13} {al.status:<10} {al.status_note}")
    return 0


def cmd_procurement(args) -> int:
    app = pack_app(args, NAME)
    if app is None:
        return 1
    if args.action == "run":
        report = app.procurement.run(dry_run=args.dry_run)
        mode = "DRY RUN (nothing sent)" if args.dry_run else "LIVE"
        print(f"{mode}: considered {report.considered}, proposed {report.proposed}, needs-your-decision {report.manual}, skipped {report.skipped}, resumed {report.resumed}")
        for act in report.actions:
            extra = f"-> {act['vendor']} x {act['qty']:g} = {act['total']:,.2f} (score {act['score']})" if "vendor" in act else act.get("problem", "")
            print(f"  {act['item_code']:<14} {act['action']:<20} {extra}")
        for err in report.errors:
            print(f"  ERROR {err}")
        return 1 if report.errors else 0
    for p in app.proposals.list(args.status):
        print(f"{p.id}  {p.created_at:%Y-%m-%d %H:%M}  {p.item_code:<12} {p.status:<12} {p.supplier_name or '-':<20} {p.po_name or ''}  {p.status_note}")
    return 0


# ------------------------------------------------------------------ the agents' tasks (erp-agents agents brief | review | ask)
def agent_brief(args, app) -> int:
    print_agent_result(app.agents.brief("cli", use_model=False if args.rules else None))
    return 0


def agent_review(args, app) -> int:
    if not args.text:
        print("Give the request id: erp-agents agents review <request id>   (see 'erp-agents approvals list')")
        return 2
    try:
        res = app.agents.review(args.text[0], "cli", force=args.force, use_model=False if args.rules else None)
    except ValueError as exc:
        print(exc)
        return 1
    print_agent_result({**res, "text": review_text(res)})
    return 0


def agent_ask(args, app) -> int:
    question = " ".join(args.text or [])
    if not question:
        print('Give a question: erp-agents agents ask "what is running low?"')
        return 2
    try:
        print_agent_result(app.agents.ask(question, CLI_ACTOR))
    except ValueError as exc:
        print(exc)
        return 1
    return 0


AGENT_ACTIONS = {"brief": agent_brief, "review": agent_review, "ask": agent_ask}


# ------------------------------------------------------------------ the commands and their arguments
def _demo(s) -> None:
    s.add_argument("--interactive", action="store_true", help="answer the approval requests in the terminal")
    s.add_argument("--verbose", action="store_true", help="print every approval message")


def _run_once(s) -> None:
    s.add_argument("--dry-run", action="store_true", help="analyse only; send nothing, store nothing")
    s.add_argument("--item", action="append", help="limit to this item code (repeatable)")


def _analyze(s) -> None:
    s.add_argument("item")


def _policy(s) -> None:
    s.add_argument("action", choices=["show", "set", "clear", "reset", "audit"])
    s.add_argument("--scope", choices=["global", "group", "item"], default="global")
    s.add_argument("--key", help="item group name or item code")
    s.add_argument("values", nargs="*", help="set: field=value (e.g. min_qty=150 min_days_cover=null); clear: field names")
    s.set_defaults(leftovers="values")          # on Python 3.10 and 3.11 what follows the options lands in the leftovers: parse_cli puts it back (core/cli.py)


def _alerts(s) -> None:
    s.add_argument("--status")
    s.add_argument("--item")


def _procurement(s) -> None:
    s.add_argument("action", choices=["run", "proposals"])
    s.add_argument("--dry-run", action="store_true", help="run: show what it would recommend; send and store nothing")
    s.add_argument("--status", help="proposals: filter by status")


def _followup(s) -> None:
    s.add_argument("action", choices=["late", "receipts", "scorecard", "quotes", "forecast", "slow", "substitutes", "counts", "variances", "run", "weekly"])
    s.add_argument("item", nargs="?", help="forecast, substitutes: the item code")
    s.add_argument("--dry-run", action="store_true", help="run, weekly: show what would be sent; send and store nothing")


def cmd_followup(args) -> int:
    """The purchasing follow-up and stock intelligence: the same results the agents' tools give, printed."""
    app = pack_app(args, NAME)
    if app is None:
        return 1
    if args.action in ("forecast", "substitutes") and not args.item:
        print(f"Give an item code: erp-agents followup {args.action} BOLT-M8")
        return 2
    if args.action == "run":
        out = {**app.followup.daily(args.dry_run), "count_differences": app.stock_insight.counts.run_variances(args.dry_run), "quotes": app.followup.quotes.check(args.dry_run)}
    elif args.action == "weekly":
        out = {"slow_stock": app.stock_insight.slow.run(args.dry_run), "count_plan": app.stock_insight.counts.run_plan(args.dry_run)}
    elif args.action == "quotes":
        out = {"rounds": [{k: r[k] for k in ("id", "item_code", "qty", "status", "reply_by", "status_note")} for r in app.followup.quotes.list()]}
    else:
        name, call = {"late": ("late_deliveries", {}), "receipts": ("receipt_mismatches", {}), "scorecard": ("vendor_scorecard", {}),
                      "forecast": ("demand_forecast", {"item_code": args.item}), "slow": ("slow_stock", {"limit": 25}), "substitutes": ("substitutes", {"item_code": args.item}),
                      "counts": ("stock_counts", {"kind": "plan"}), "variances": ("stock_counts", {"kind": "variances"})}[args.action]
        out = app.toolkit.call(name, call, CLI_ACTOR)
    print(json.dumps(out, indent=2, default=str))
    return 1 if isinstance(out, dict) and out.get("error") else 0


COMMANDS = [
    CliCommand("demo", "run the agent on a built-in demo ERP (no setup needed)", _demo, cmd_demo),
    CliCommand("run-once", "run the inventory check now", _run_once, cmd_run_once),
    CliCommand("analyze", "show the analysis for one item", _analyze, cmd_analyze),
    CliCommand("policy", "view/change thresholds (admin)", _policy, cmd_policy),
    CliCommand("alerts", "list alerts", _alerts, cmd_alerts),
    CliCommand("procurement", "vendor selector: run it or list its proposals", _procurement, cmd_procurement),
    CliCommand("followup", "late deliveries, goods receipt checks, vendor scorecard, quote rounds, forecasts, slow stock, substitutes, cycle counts", _followup, cmd_followup),
]
