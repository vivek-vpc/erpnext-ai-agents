"""python -m tools.evals [--check] [--case ID ...] [--group NAME] [--show ID] [--list] [--markdown FILE] [--json FILE]

Runs the golden cases against the application with a scripted stand-in for the language model. No model, no ERPNext, no network. See docs/EVALUATION.md."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Optional

from .cases import load_cases
from .metrics import gate, load_thresholds, summarise
from .report import markdown
from .runner import Result, run_case


def show(result: Result) -> None:
    c, o = result.case, result.observed
    print(f"{c.id}  [{c.group}]  entry={c.entry} as={c.asker}\n  question: {c.question}")
    if result.skipped:
        print("  skipped:", result.skipped)
        return
    print(f"  intent={o.intent!r} status={o.status!r} source={o.source!r} agent={o.agent!r} outcome={o.outcome!r} routed_by_ai={o.routed_by_ai} "
          f"approval requests created={o.prepared} ERP writes={o.erp_writes}")
    print("  tools:", ", ".join(f"{t['name']}({json.dumps(t['args'], default=str)})" for t in o.tools) or "none")
    print("  answer:", o.answer)
    if o.text != o.answer:
        print("  with its tables:", o.text[:1200])
    if o.note:
        print("  the task record says:", o.note)
    if o.error:
        print("  refused:", o.error)
    if result.failures:
        print("  DOES NOT MEET THE CASE:" + "".join(f"\n    - {f}" for f in result.failures))
    else:
        print("  meets the case")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tools.evals", description=__doc__.split("\n\n")[0])
    p.add_argument("--check", action="store_true", help="exit 1 when a threshold in tests/evals/thresholds.json is missed (what CI runs)")
    p.add_argument("--case", action="append", default=[], help="run only this case id (repeatable)")
    p.add_argument("--group", help="run only this group")
    p.add_argument("--show", metavar="ID", help="run one case and print what the application really answered (for writing a case)")
    p.add_argument("--list", action="store_true", help="list the case ids and exit")
    p.add_argument("--markdown", metavar="FILE", help="append the report to this file (CI passes $GITHUB_STEP_SUMMARY)")
    p.add_argument("--json", metavar="FILE", help="write the summary as JSON")
    args = p.parse_args(argv)
    logging.disable(logging.WARNING - 1)                  # the application logs a line at start-up for every world; only warnings and errors matter here

    try:
        cases = load_cases()
    except ValueError as exc:
        print(f"the cases cannot be loaded: {exc}")
        return 2
    if args.list:
        for c in cases:
            print(f"{c.id:48} {c.group:18} {c.entry:8} {c.question}")
        return 0
    if args.show:
        wanted = [c for c in cases if c.id == args.show]
        if not wanted:
            print(f"no case {args.show!r}")
            return 2
        show(run_case(wanted[0]))
        return 0
    chosen = [c for c in cases if (not args.case or c.id in args.case) and (not args.group or c.group == args.group)]
    if not chosen:
        print("no case matches")
        return 2
    results = []
    for c in chosen:
        r = run_case(c)
        results.append(r)
        mark = "skip" if r.skipped else ("gap " if c.known_gap and r.counts else ("ok  " if r.counts else "FAIL"))
        print(f"{mark} {c.id}" + ("" if r.counts or r.skipped else "".join(f"\n       - {f}" for f in r.failures)))
    summary = summarise(results)
    partial = bool(args.case or args.group)
    problems = gate(summary, load_thresholds()) if args.check and not partial else []
    text = markdown(summary, problems)
    print("\n" + text)
    if args.markdown:
        with open(args.markdown, "a", encoding="utf-8") as fh:
            fh.write(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
    if args.check and partial:
        print("--check needs the whole suite: no --case or --group")
        return 2
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
