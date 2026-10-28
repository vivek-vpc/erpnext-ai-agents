"""What the results add up to, and the gate that fails CI when they are not good enough (the limits are in tests/evals/thresholds.json, reviewed like code)."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from .cases import OUTCOMES, THRESHOLDS
from .runner import Result


def summarise(results: list[Result]) -> dict[str, Any]:
    ran = [r for r in results if not r.skipped]
    groups: dict[str, dict[str, int]] = defaultdict(lambda: {"cases": 0, "counted": 0, "met": 0})
    for r in ran:
        g = groups[r.case.group]
        g["cases"] += 1                                            # coverage: a case that shows a known gap still covers its question
        if not r.case.known_gap:
            g["counted"] += 1
            g["met"] += int(r.passed)
    model = [r for r in ran if r.observed.outcome != "none" or r.case.llm]
    outcomes = {o: sum(1 for r in model if r.observed.outcome == o) for o in OUTCOMES if o != "none"}
    bad = [r for r in ran if r.case.reply == "bad"]
    good = [r for r in ran if r.case.reply == "good"]
    known = [r for r in ran if r.case.known_gap]
    n_model = len(model)
    counted = [r for r in ran if not r.case.known_gap]
    return {
        "cases": len(results), "ran": len(ran), "skipped": len(results) - len(ran),
        "counted": len(counted), "met": sum(1 for r in counted if r.passed), "failed": [r.case.id for r in counted if not r.passed],
        "pass_rate": (sum(1 for r in counted if r.passed) / len(counted)) if counted else 0.0,
        "groups": {name: {**g, "rate": g["met"] / g["counted"] if g["counted"] else 1.0} for name, g in sorted(groups.items())},
        "known_gaps": [r.case.id for r in known if not r.passed], "known_gaps_fixed": [r.case.id for r in known if r.passed],
        "model": {"cases": n_model, **outcomes, "rejects": sum(r.observed.rejects for r in ran), "repairs": sum(r.observed.repairs for r in ran),
                  "fallback_rate": ((outcomes["fallback"] + outcomes["unavailable"]) / n_model) if n_model else 0.0,
                  "tokens_in": sum(r.observed.tokens_in for r in ran), "tokens_out": sum(r.observed.tokens_out for r in ran),
                  "seconds": round(sum(r.observed.seconds for r in ran), 2)},
        "bad_replies": {"cases": len(bad), "caught": sum(1 for r in bad if r.passed)},
        "good_replies": {"cases": len(good), "wrongly_rejected": sum(1 for r in good if r.observed.outcome in ("repaired", "fallback", "unavailable"))},
    }


def load_thresholds(path: Optional[Path] = None) -> dict[str, Any]:
    return json.loads((path or THRESHOLDS).read_text(encoding="utf-8"))


def gate(summary: dict[str, Any], limits: dict[str, Any]) -> list[str]:
    """Why the run is not good enough, in words; empty = it is."""
    bad: list[str] = []
    if summary["skipped"] > limits.get("skipped_max", 0):
        bad.append(f"{summary['skipped']} case(s) were skipped (the optional 'agents' extra is missing?), so the result says nothing about the model path")
    if not limits["min_cases"] <= summary["cases"] <= limits["max_cases"]:
        bad.append(f"{summary['cases']} cases: the suite is meant to hold {limits['min_cases']} to {limits['max_cases']}")
    floor = limits["pass_rate"]["overall"]
    if summary["pass_rate"] < floor:
        bad.append(f"overall pass rate {summary['pass_rate']:.1%} is under the limit of {floor:.1%}; failing: {', '.join(summary['failed'][:8])}{' ...' if len(summary['failed']) > 8 else ''}")
    for name, g in summary["groups"].items():
        need = limits["pass_rate"].get("per_group", {}).get(name, floor)
        if g["rate"] < need:
            bad.append(f"group {name}: pass rate {g['rate']:.1%} is under the limit of {need:.1%}")
    for name, minimum in limits.get("min_cases_per_group", {}).items():
        have = summary["groups"].get(name, {}).get("cases", 0)
        if have < minimum:
            bad.append(f"group {name}: {have} cases, at least {minimum} are needed to cover it")
    b = summary["bad_replies"]
    if b["cases"] and b["caught"] / b["cases"] < limits["bad_replies_caught"]:
        bad.append(f"bad model replies caught: {b['caught']} of {b['cases']}, the limit is {limits['bad_replies_caught']:.0%}: a wrong reply reached a person")
    g = summary["good_replies"]
    if g["wrongly_rejected"] > limits["good_replies_wrongly_rejected_max"]:
        bad.append(f"{g['wrongly_rejected']} good model replies were rejected by the checks (the limit is {limits['good_replies_wrongly_rejected_max']})")
    if len(summary["known_gaps"]) > limits["known_gaps_max"]:
        bad.append(f"{len(summary['known_gaps'])} known gaps ({', '.join(summary['known_gaps'])}), the limit is {limits['known_gaps_max']}")
    if summary["known_gaps_fixed"]:
        bad.append(f"known gap fixed, remove its known_gap line: {', '.join(summary['known_gaps_fixed'])}")
    m = summary["model"]
    if m["fallback_rate"] > limits["fallback_rate_max"]:
        bad.append(f"the rules' answer replaced the model's in {m['fallback_rate']:.1%} of model cases, the limit is {limits['fallback_rate_max']:.1%}")
    if m["repairs"] < limits["repairs_min"]:
        bad.append(f"only {m['repairs']} repairs happened, at least {limits['repairs_min']} are expected: the repair step is no longer being exercised")
    return bad
