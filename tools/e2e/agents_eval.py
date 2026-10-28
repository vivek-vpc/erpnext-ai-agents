"""Measures the AI agents against your real ERPNext and a real model: how long each task takes, how many model replies it needs, whether the answer was accepted by the
checks, and whether it is RIGHT (compared with the answer the tools themselves give).

Safe by construction, like run_live.py: the application under test uses an in-memory database and no Telegram or WhatsApp, so nothing reaches anybody's phone and
nothing is written to ERPNext (the order request used for the second-opinion case exists only in memory). ERPNext is only read.

    python -m tools.e2e.agents_eval --config config/settings.erpnext.yaml --model llama3.1
    python -m tools.e2e.agents_eval --config ... --model llama3.2:3b --cases brief,ask --out report.md
    python -m tools.e2e.agents_eval --config ... --base-url https://llm.example.com --allow-remote      # a hosted model: the data listed in docs/PRIVACY.md is sent there

    python -m tools.e2e.agents_eval --suite golden --model llama3.1                                       # the golden cases that need a model, on the demo ERP
    python -m tools.e2e.agents_eval --suite golden --provider anthropic --against ollama:llama3.1 --out compare.md      # two models side by side

A run takes minutes per task on a laptop CPU; use --cases to choose. The report is a Markdown table you can paste into docs/AGENTS.md.
"""
from __future__ import annotations

import argparse
import copy
import re
import statistics
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

from erp_agents.core.assistant.verify import numbers
from erp_agents.core.bootstrap import build_app
from erp_agents.core.config import load_config
from erp_agents.core.common import Actor

ASKER = Actor("eval", frozenset({"viewer"}), "eval")
APPROVER = Actor("eval-approver", frozenset({"approver"}), "eval")
ALL_CASES = ("brief", "ask", "review")


def has_number(text: str, value: float) -> bool:
    return any(abs(n - value) <= 0.51 for n in numbers(text))


class Report:
    def __init__(self, model: str, base_url: str) -> None:
        self.model, self.base_url, self.rows = model, base_url, []

    def add(self, case: str, prompt: str, res: dict[str, Any], wall: float, correct: Optional[bool], detail: str) -> None:
        row = {"case": case, "prompt": prompt, "wall": wall, "source": res.get("source"), "status": res.get("status"), "correct": correct, "detail": detail, "note": res.get("note") or "",
               "seconds": res.get("seconds", 0)}
        self.rows.append(row)
        flag = {True: "right", False: "WRONG", None: "-"}[correct]
        print(f"  {case:7} {wall:7.0f}s  source={str(row['source']):5} status={str(row['status']):22} correct={flag:5}  {prompt[:60]}")

    def markdown(self, task_rows: list[dict[str, Any]]) -> str:
        out = [f"# Measured: {self.model} at {self.base_url}", "", f"Run on {date.today().isoformat()} against the live ERPNext, read-only.", "",
               "| Case | Question | Wall time | Model replies | Reading prompt | Writing | Prompt tokens | Output tokens | Accepted | Right |", "|---|---|---|---|---|---|---|---|---|---|"]
        for r, t in zip(self.rows, task_rows):
            timings = (t.get("trace") or [{}, {}, {}])[2].get("timings", []) if t and t.get("trace") and len(t["trace"]) > 2 else []
            read, write = sum(x.get("reading_prompt", 0) for x in timings), sum(x.get("writing", 0) for x in timings)
            accepted = "yes" if r["source"] in ("model", "agent") else "no (rules used)"
            right = {True: "yes", False: "NO", None: "n/a"}[r["correct"]]
            out.append(f"| {r['case']} | {r['prompt'][:70]} | {r['wall']:.0f} s | {t.get('steps', '')} | {read:.0f} s | {write:.0f} s | {t.get('prompt_tokens', '')} | {t.get('output_tokens', '')} | {accepted} | {right} |")
        scored = [r for r in self.rows if r["correct"] is not None]
        walls = [r["wall"] for r in self.rows]
        out += ["", f"- Tasks: {len(self.rows)}; median {statistics.median(walls):.0f} s, slowest {max(walls):.0f} s." if walls else "- No tasks run.",
                f"- Accepted by the checks: {sum(1 for r in self.rows if r['source'] in ('model', 'agent'))} of {len(self.rows)}.",
                f"- Right, of those with an answer to compare with: {sum(1 for r in scored if r['correct'])} of {len(scored)}.", ""]
        for r in self.rows:
            out += [f"**{r['case']}: {r['prompt']}**  ", f"{r['detail']}" + (f"  \nNote: {r['note']}" if r["note"] else ""), ""]
        return "\n".join(out)


def pick_items(app: Any) -> dict[str, Any]:
    rows = app.inventory.overview()
    ok = next((r for r in rows if r["status"] == "OK" and (r.get("on_hand") or 0) > 0 and r.get("daily_use")), None)
    hot = next((r for r in rows if r["status"] == "BREACH" and (r.get("cover_days") or 0) > 0), None) or next((r for r in rows if r["status"] == "BREACH"), None)
    priced = None
    for r in rows[:40]:
        v = app.toolkit.call("vendor_options", {"item_code": r["item_code"], "qty": 500}, ASKER)
        if v.get("best") and len([x for x in v.get("vendors", []) if x.get("eligible")]) >= 2:
            priced = (r["item_code"], v)
            break
    return {"ok": ok, "hot": hot, "priced": priced}


def run(args: argparse.Namespace) -> int:
    cfg = copy.deepcopy(load_config(args.config))
    db = args.db or ":memory:"
    cfg["database"] = {**cfg.get("database", {}), "path": db}
    for name in ("telegram", "whatsapp"):                                  # nothing may reach anybody's phone
        cfg.setdefault("channels", {}).setdefault(name, {})["enabled"] = False
    cfg["agents"] = {"enabled": True, "provider": args.provider, "model": args.model, "base_url": args.base_url, "timeout_seconds": args.timeout, "max_steps": args.max_steps,
                     "allow_remote": args.allow_remote or args.provider != "ollama"}          # a hosted provider is remote by nature: choosing it on the command line is the consent
    app = build_app(config=cfg, db_path=db)
    cases = [c.strip() for c in args.cases.split(",") if c.strip()]
    info = app.agents.describe()
    report = Report(f"{info['model']} ({info['provider_label']})", info["base_url"])
    task_ids: list[Optional[str]] = []
    print(f"Model {info['model']} via {info['provider_label']} at {info['base_url']}; cases: {', '.join(cases)}")
    if info["hosted"] and not info["key_configured"]:
        print(f"No API key: add {info['key_variable']}=... to the .env file first.")
        return 2
    status = app.agents.status(check_model=True)
    print("  model server:", status.get("model_check") or status)
    if not status["package_installed"]:
        print(status["install_hint"])
        return 2

    def timed(fn: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], float]:
        t0 = time.monotonic()
        res = fn()
        return res, time.monotonic() - t0

    items = pick_items(app)
    if "brief" in cases:
        res, wall = timed(lambda: app.agents.brief("eval"))
        rules = app.agents.brief("eval", use_model=False)
        listed = [p["item_code"] for p in res["priorities"]]
        same_table = listed == [p["item_code"] for p in rules["priorities"]]
        report.add("brief", "today's stock brief", res, wall, same_table, f"Headline: {res['headline']}\n\nTable matches the rules' table: {same_table}. Watch-outs: "
                                                                                 + "; ".join(f"{w['item_code']} ({w['by']}): {w['note']}" for w in res["watch_outs"]))
        task_ids.append(res.get("task_id"))
    if "ask" in cases:
        asks: list[tuple[str, Callable[[str], Optional[bool]]]] = []
        if items["ok"]:
            r = items["ok"]
            asks.append((f"How much {r['item_code']} do we have right now?", lambda a, v=r["on_hand"]: has_number(a, v)))
        if items["hot"]:
            r = items["hot"]
            days = app.toolkit.call("stock_status", {"item_code": r["item_code"]}, ASKER).get("cover_days")
            asks.append((f"How many days of stock do we have left of {r['item_code']}?", lambda a, v=days: None if v is None else has_number(a, v)))
        if items["priced"]:
            code, v = items["priced"]
            asks.append((f"Which vendor is the best choice for {code} for 500?", lambda a, name=v["best"]: name.lower() in a.lower()))
        today = app.inventory.today().isoformat()
        late = [d["document"] for d in app.toolkit.call("open_orders", {}, ASKER).get("documents", []) if d.get("due") and d["due"] < today and (d.get("still_to_come") or 0) > 0]
        # nothing late: any "yes" or any order named as late is a wrong answer; something late: every late order must be named
        asks.append(("Are any of our purchase orders late?", lambda a, docs=late: all(d in a for d in docs) if docs else not re.search(r"\byes\b|PUR-ORD-", a, re.I)))
        wanted = [w.strip().lower() for w in args.only.split(",") if w.strip()]
        asks = [(q, o) for q, o in asks if not wanted or any(w in q.lower() for w in wanted)]
        for q, oracle in asks:
            res, wall = timed(lambda q=q: app.agents.ask(q, ASKER))
            answered = res.get("status") == "answered"
            report.add("ask", q, res, wall, oracle(res.get("answer", "")) if answered else None, res.get("answer", ""))
            task_ids.append(res.get("task_id"))
    if "review" in cases and items["hot"]:
        code = items["hot"]["item_code"]
        made = app.toolkit.call("prepare_order_request", {"item_code": code, "reason": "evaluation"}, APPROVER)
        if made.get("request_id"):
            res, wall = timed(lambda: app.agents.review(made["request_id"], "eval", force=True))
            report.add("review", f"second opinion on {made['qty']} of {code}", res, wall, None,
                       f"Verdict: {res['verdict']}. Checks: " + "; ".join(f["text"] for f in res["findings"]) + ". Model's points: " + "; ".join(res["points"]))
            task_ids.append(res.get("task_id"))
        else:
            print("  review: could not prepare a request for", code, made)
    task_rows = [app.agent_log.get(t) if t else {} for t in task_ids]
    md = report.markdown(task_rows)
    spent_in, spent_out = sum(t.get("prompt_tokens") or 0 for t in task_rows if t), sum(t.get("output_tokens") or 0 for t in task_rows if t)
    md += f"\n- Tokens used by this run: {spent_in + spent_out:,} ({spent_in:,} in, {spent_out:,} out)" + (" on a hosted model, billed by the provider." if info["hosted"] else ", on this computer (free).") + "\n"
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print("Report written to", args.out)
    else:
        print("\n" + md)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--config", default="config/settings.erpnext.yaml")
    p.add_argument("--provider", default="ollama", choices=["ollama", "anthropic", "openai", "gemini"], help="a hosted provider needs its key in the environment (ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY)")
    p.add_argument("--model", default="", help="empty = the provider's suggested model")
    p.add_argument("--base-url", default="", help="empty = the provider's own address")
    p.add_argument("--allow-remote", action="store_true", help="the model server is not on this machine (the data in docs/PRIVACY.md is sent there)")
    p.add_argument("--cases", default=",".join(ALL_CASES), help="comma list of: " + ", ".join(ALL_CASES))
    p.add_argument("--only", default="", help="ask cases: run only the questions that contain one of these comma-separated words")
    p.add_argument("--db", help="keep the record of the tasks in this SQLite file (default: in memory, forgotten), to read what was sent to the model")
    p.add_argument("--timeout", type=int, default=900, help="seconds to wait for ONE model reply")
    p.add_argument("--max-steps", type=int, default=6)
    p.add_argument("--out", help="write the Markdown report here")
    p.add_argument("--suite", choices=["legacy", "golden"], default="legacy", help="legacy = the three cases above, on your real ERPNext; golden = the cases of tests/evals that need a model, on the demo ERP "
                                                                                    "(the same cases CI replays with a scripted model; see docs/EVALUATION.md)")
    p.add_argument("--group", action="append", default=[], help="golden: only this group of cases (repeatable)")
    p.add_argument("--case", action="append", default=[], help="golden: only this case id (repeatable)")
    p.add_argument("--against", help="golden: a second model to run the same cases on and compare, as provider:model (or an Ollama model name); needs --allow-remote if it is not on this machine")
    p.add_argument("--against-base-url", default="", help="golden: the second model's address (empty = the provider's own)")
    p.add_argument("--record", metavar="DIR", help="golden: save what each model said as a case with an llm: script, ready to become a golden case once you have checked it")
    args = p.parse_args(argv)
    if args.suite == "golden":
        from tools.evals.live import golden
        return golden(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
