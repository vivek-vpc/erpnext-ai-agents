"""Command line for the test-data loader.

    python -m tools.testdata.cli list                 show the steps
    python -m tools.testdata.cli run --step company   run one step (or several: --step a --step b; or --all)
    python -m tools.testdata.cli run --all --dry-run  show what would be created, change nothing
    python -m tools.testdata.cli cleanup-list         list everything created so far (from the manifest)
    python -m tools.testdata.cli contract --url http://localhost:8000 --key K --secret S --out contract-site.json
                                                      the small world of the shared ERP-client checks (ai_munimji_engine.testkit), into any ERPNext site
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from erp_agents.core.config import load_config
from erp_agents.core.erp.erpnext import ERPNextClient

from .steps import STEPS, Context
from .writer import Writer, WriterError

MANIFEST = Path(__file__).resolve().parent / "manifest.jsonl"


def build_context(dry_run: bool, config: str) -> Context:
    cfg = load_config(config)["erp"]
    if cfg.get("type") != "erpnext":
        raise SystemExit("The test data loader needs erp.type: erpnext in the config.")
    client = ERPNextClient(cfg["url"], cfg["api_key"], cfg["api_secret"])
    return Context(Writer(client.session, client.base_url, MANIFEST, dry_run=dry_run))


def run_contract(args) -> int:
    """Load the world of the shared ERP-client checks into the site at --url (default $ERP_CONTRACT_URL) and write what the site called its parts to --out."""
    import os

    import requests

    from . import contract
    url, key, secret = args.url or os.environ.get("ERP_CONTRACT_URL"), args.key or os.environ.get("ERP_CONTRACT_KEY"), args.secret or os.environ.get("ERP_CONTRACT_SECRET")
    if not (url and key and secret):
        print("give --url, --key and --secret (or ERP_CONTRACT_URL, ERP_CONTRACT_KEY, ERP_CONTRACT_SECRET)")
        return 2
    session = requests.Session()
    session.headers.update({"Authorization": f"token {key}:{secret}", "Accept": "application/json"})
    writer = Writer(session, url, None, dry_run=args.dry_run)                    # no manifest: the documents carry the marker TD/CT-..., and the output file names the rest
    try:
        site = contract.run(writer, Path(args.out), args.company)
    except (WriterError, contract.LoaderError) as exc:
        print(f"STOPPED: {exc}\nEverything created before this point is kept; running it again continues.")
        return 1
    print("Summary:\n" + writer.summary())
    print(f"Company {site['company']} ({site['abbr']}); reference day {site['today']}; the description of the site is in {args.out}" + ("  [DRY RUN: nothing written]" if args.dry_run else ""))
    return 0


def run_year(args) -> int:
    from collections import Counter
    from datetime import date as _date

    from . import post, sim
    plan = sim.build_plan()
    by = Counter(e.kind for e in plan.events)
    print("Plan:", ", ".join(f"{k} {v}" for k, v in sorted(by.items())), "|", {k: round(v) for k, v in plan.stats.items()})
    if args.plan_only:
        return 0
    ctx = build_context(False, args.config)
    poster = post.Poster(ctx, plan, only=set(args.kinds.split(",")) if args.kinds else None)
    try:
        poster.run(until=_date.fromisoformat(args.until) if args.until else None, since=_date.fromisoformat(args.since) if args.since else None,
                   stop_on_error=not args.keep_going)
    except WriterError as exc:
        print(f"\nSTOPPED: {exc}\nEverything posted before this is kept; re-running continues from here.")
        return 1
    finally:
        print("\nSummary:\n" + ctx.w.summary())
        for k, msg in poster.failures[:30]:
            print("  !", k, msg[:300])
    return 1 if poster.failures else 0


def run_scenarios(args) -> int:
    from collections import Counter
    from datetime import date as _date

    from . import post, scenarios, steps
    only = {c for c in (args.only or "").split(",") if c} or None
    plan = scenarios.build_plan(only=only)
    by = Counter(e.kind for e in plan.events)
    print("Plan:", ", ".join(f"{k} {v}" for k, v in sorted(by.items())), "| total", len(plan.events))
    if args.plan_only:
        return 0
    ctx = build_context(False, args.config)
    items = [i for i in scenarios.items() if not only or i.code in only]
    if not args.no_items:
        steps.step_items(ctx, items)
        steps.step_prices(ctx, items)
    poster = post.Poster(ctx, plan, only=set(args.kinds.split(",")) if args.kinds else None, extra_items=scenarios.items())
    try:
        poster.run(until=_date.fromisoformat(args.until) if args.until else None, since=_date.fromisoformat(args.since) if args.since else None,
                   stop_on_error=not args.keep_going)
    except WriterError as exc:
        print(f"\nSTOPPED: {exc}\nEverything posted before this is kept; re-running continues from here.")
        return 1
    finally:
        print("\nSummary:\n" + ctx.w.summary())
        for k, msg in poster.failures[:30]:
            print("  !", k, msg[:300])
    return 1 if poster.failures else 0


def run_days(args) -> int:
    from datetime import date as _date, timedelta

    from . import days
    today = _date.fromisoformat(args.d2) if args.d2 else _date.today()
    d1 = _date.fromisoformat(args.d1) if args.d1 else today - timedelta(days=1)
    ctx = build_context(False, args.config)
    poster = days.Days(ctx, d1, today)
    print(f"Two business days: {d1.isoformat()} (yesterday) and {today.isoformat()} (today)")
    try:
        poster.run(only=[b for b in (args.only or "").split(",") if b] or None, keep_going=not args.stop_on_error)
    except WriterError as exc:
        print(f"\nSTOPPED: {exc}\nEverything posted before this is kept; re-running continues from here.")
        return 1
    finally:
        print("\nCreated in this run:")
        for k, v in sorted(poster.made.items()):
            print(f"  {k:<28} {v}")
        print(f"  {'TOTAL':<28} {sum(poster.made.values())}")
        for n in poster.notes:
            print("  -", n)
        for k, msg in poster.failures:
            print("  !", k, msg[:400])
    return 1 if poster.failures else 0


def run_features(args) -> int:
    from datetime import date as _date

    from . import features
    state = json.loads(features.STATE.read_text(encoding="utf-8")) if features.STATE.exists() else {}
    today = _date.fromisoformat(args.today) if args.today else (_date.fromisoformat(state["_t0"]) if state.get("_t0") else _date.today())
    only = {s for s in (args.only or "").split(",") if s} or None
    if args.action == "check":
        features.check(args.config, today)
        return 0
    ctx = build_context(args.action == "plan", args.config)
    plan = features.Features(ctx, today, only)
    if args.action == "plan":
        try:
            rows = plan.summary()
        except features.PlanError as exc:
            print(f"PLAN ERROR: {exc}")
            return 1
        total: dict[str, int] = {}
        print(f"Feature scenarios as of {today.isoformat()} ({len(rows)} materials, 4 vendors). Nothing is read or written.\n")
        print(f"{'material':<12} {'scenario':<10} {'stock today':>12} {'opening':>9} {'lowest':>8}  documents")
        for r in rows:
            for k, v in r["docs"].items():
                total[k] = total.get(k, 0) + v
            print(f"{r['code']:<12} {r['scenario']:<10} {r['stock_now']:>12g} {r['opening']:>9g} {r['lowest']:>8g}  " + ", ".join(f"{v} {k}" for k, v in sorted(r["docs"].items())))
        print("\nDocuments in all: " + ", ".join(f"{v} {k}" for k, v in sorted(total.items())) + f"  (total {sum(total.values())})")
        return 0
    if args.action == "report":
        for r in features.stock_report(ctx, list(plan.mats)):
            print(f"{r['code']:<12} {r['name'][:62]:<62} on hand {r['on_hand']:>9,.0f} {r['uom']:<5} reserved {r['reserved']:>6,.0f}  on order {r['on_order']:>7,.0f}  value {r['value']:>12,.0f}")
        return 0
    if args.action == "redo-sales":
        done = plan.cancel_foreign_sales()
        print(f"Delivery notes and sales orders of the scenarios on customers other than the test customers: {done['cancelled']} cancelled, {done['deleted']} drafts deleted.")
        return 0
    if args.action == "quote-replies":
        if not args.item:
            print("give --item TST-RFQ-1")
            return 2
        plan.load_names()
        names = plan.quote_replies(args.item, args.qty, {"alpha": args.alpha, "beta": args.beta, "delta": args.delta})
        print("Supplier quotations posted:", ", ".join(names))
        return 0
    state["_t0"] = today.isoformat()
    features.STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    try:
        plan.summary()
    except features.PlanError as exc:
        print(f"PLAN ERROR: {exc}")
        return 1
    try:
        plan.run_all()
    except WriterError as exc:
        print(f"\nSTOPPED: {exc}\nEverything posted before this is kept; re-running continues from here.")
        plan.restore_same_rate()
        return 1
    finally:
        print("\nCreated in this run:")
        for k, v in sorted(plan.made.items()):
            print(f"  {k:<28} {v}")
        print(f"  {'TOTAL':<28} {sum(plan.made.values())}")
        for n in plan.notes:
            print("  -", n)
        for k, msg in plan.failures:
            print("  !", k, msg[:400])
    return 1 if plan.failures else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="testdata")
    p.add_argument("--config", default="config/settings.erpnext.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("--step", action="append", default=[])
    r.add_argument("--all", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    sub.add_parser("cleanup-list")
    y = sub.add_parser("year", help="plan and post the year of transactions")
    y.add_argument("--plan-only", action="store_true", help="show the plan, post nothing")
    y.add_argument("--since")
    y.add_argument("--until")
    y.add_argument("--kinds", help="comma separated, e.g. PO,PR,PI")
    y.add_argument("--keep-going", action="store_true", help="log failures and continue instead of stopping")
    sc = sub.add_parser("scenarios", help="ten materials with several vendors and customers each, and a history of purchases and sales with distinct demand patterns")
    sc.add_argument("--plan-only", action="store_true", help="show the plan, post nothing")
    sc.add_argument("--no-items", action="store_true", help="do not create the items and prices (they exist already)")
    sc.add_argument("--only", help="comma separated item codes, e.g. MET-0025,CHM-0025")
    sc.add_argument("--since")
    sc.add_argument("--until")
    sc.add_argument("--kinds", help="comma separated, e.g. PO,PR,PI")
    sc.add_argument("--keep-going", action="store_true", help="log failures and continue instead of stopping")
    d = sub.add_parser("days", help="two live business days (yesterday and today) of end-to-end cycles on top of the existing history")
    d.add_argument("--only", help="comma separated block names, e.g. rfq1,mr1,oldpo")
    d.add_argument("--d1")
    d.add_argument("--d2")
    d.add_argument("--stop-on-error", action="store_true")
    f = sub.add_parser("features", help="materials, vendors and documents for testing the follow-up and stock-intelligence features (delivery chasing, receipt check, scorecard ...)")
    f.add_argument("action", choices=["plan", "run", "report", "quote-replies", "check", "redo-sales"])
    f.add_argument("--only", help="comma separated scenarios: late,grn,scorecard,rfq,seasonal,slow,subs,counts")
    f.add_argument("--today", help="the day the relative dates count from (default: the day of the first run)")
    f.add_argument("--item", help="quote-replies: the material the quotations are for")
    f.add_argument("--qty", type=float, default=500.0, help="quote-replies: the quantity quoted")
    f.add_argument("--alpha", type=float, default=2700.0, help="quote-replies: Alpha's price per unit")
    f.add_argument("--beta", type=float, default=2600.0, help="quote-replies: Beta's price per unit")
    f.add_argument("--delta", type=float, default=2950.0, help="quote-replies: Delta's price per unit")
    c = sub.add_parser("contract", help="the small world of the shared ERP-client checks, posted into an ERPNext site (needs no config file)")
    c.add_argument("--url")
    c.add_argument("--key")
    c.add_argument("--secret")
    c.add_argument("--company", help="the company to use (default: the only one the site has)")
    c.add_argument("--out", default="contract-site.json", help="where to write the site's description (reference day, names); a second run keeps the reference day in it")
    c.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd == "list":
        for name, (fn, doc) in STEPS.items():
            print(f"  {name:<18} {doc}")
        return 0
    if args.cmd == "cleanup-list":
        if not MANIFEST.exists():
            print("nothing created yet")
            return 0
        rows = [json.loads(l) for l in MANIFEST.read_text(encoding="utf-8").splitlines() if l.strip()]
        from collections import Counter
        for k, v in sorted(Counter(r["doctype"] for r in rows).items()):
            print(f"  {k:<40} {v}")
        print(f"  total {len(rows)} records, manifest: {MANIFEST}")
        return 0

    if args.cmd == "contract":
        return run_contract(args)
    if args.cmd == "features":
        return run_features(args)
    if args.cmd == "year":
        return run_year(args)
    if args.cmd == "days":
        return run_days(args)
    if args.cmd == "scenarios":
        return run_scenarios(args)
    names = list(STEPS) if args.all else args.step
    unknown = [n for n in names if n not in STEPS]
    if not names or unknown:
        print("choose --all or --step NAME; known:", ", ".join(STEPS), ("| unknown: " + ", ".join(unknown)) if unknown else "")
        return 2
    ctx = build_context(args.dry_run, args.config)
    for name in names:
        fn, doc = STEPS[name]
        t = time.time()
        print(f"\n== {name}: {doc}" + ("  [DRY RUN]" if args.dry_run else ""))
        try:
            fn(ctx)
        except WriterError as exc:
            print(f"STOPPED in step '{name}': {exc}")
            print("Everything created before this point is kept (re-running is safe: existing records are skipped).")
            return 1
        print(f"   done in {time.time() - t:.0f}s")
    print("\nSummary:\n" + ctx.w.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
