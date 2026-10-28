"""The result of a run as a Markdown table (what CI prints on a pull request, and what docs/EVALUATION.md shows), and the side-by-side table for two models."""
from __future__ import annotations

from typing import Any


def markdown(summary: dict[str, Any], problems: list[str], title: str = "AI evaluation (deterministic, no model, no ERPNext)") -> str:
    m, b, g = summary["model"], summary["bad_replies"], summary["good_replies"]
    out = [f"## {title}", "", f"**{summary['met']} of {summary['counted']} cases pass** ({summary['pass_rate']:.1%})."
           + (f" {summary['skipped']} skipped." if summary["skipped"] else ""), "",
           "| Group | Cases | Met | Pass rate |", "|---|---:|---:|---:|"]
    out += [f"| {name} | {x['cases']} | {x['met']} of {x['counted']} | {x['rate']:.0%} |" for name, x in summary["groups"].items()]
    out += ["", "| Model path | |", "|---|---:|", f"| Cases that asked the (stand-in) model | {m['cases']} |", f"| Reply accepted at once | {m['accepted']} |",
            f"| Reply rejected, repaired on the second try | {m['repaired']} |", f"| Model said the data does not answer | {m['declined']} |",
            f"| Replies rejected twice, rules' answer used | {m['fallback']} |", f"| Model unreachable | {m['unavailable']} |",
            f"| Replies rejected by the checks (all kinds) | {m['rejects']} |", f"| Bad replies caught | {b['caught']} of {b['cases']} |",
            f"| Good replies wrongly rejected | {g['wrongly_rejected']} of {g['cases']} |", f"| Fallback rate | {m['fallback_rate']:.1%} |"]
    if summary["known_gaps"]:
        out += ["", f"Known gaps (cases that show a real defect, not yet fixed): {', '.join(summary['known_gaps'])}."]
    out += ["", ("**Gate: FAILED**" if problems else "**Gate: passed**")] + [f"- {p}" for p in problems]
    return "\n".join(out) + "\n"


def comparison(rows: list[tuple[str, dict[str, Any]]]) -> str:
    """Two (or more) models on the same cases: one column each. `rows` = [(label, summary)]."""
    labels = [label for label, _ in rows]
    head = "| | " + " | ".join(labels) + " |"
    out = ["## Same cases, different models", "", head, "|---|" + "---:|" * len(labels)]

    def line(name: str, get: Any) -> str:
        return f"| {name} | " + " | ".join(str(get(s)) for _, s in rows) + " |"
    out.append(line("Cases run", lambda s: s["ran"]))
    out.append(line("Right answers", lambda s: f"{s['met']} ({s['pass_rate']:.0%})"))
    names = sorted({g for _, s in rows for g in s["groups"]})
    for g in names:
        out.append(line(f"- {g}", lambda s, g=g: (f"{s['groups'][g]['met']} of {s['groups'][g]['cases']}" if g in s["groups"] else "-")))
    out.append(line("Accepted by the checks at once", lambda s: s["model"]["accepted"]))
    out.append(line("Repaired on the second try", lambda s: s["model"]["repaired"]))
    out.append(line("Rules' answer used instead", lambda s: s["model"]["fallback"]))
    out.append(line("Said the data does not answer", lambda s: s["model"]["declined"]))
    out.append(line("Unreachable", lambda s: s["model"]["unavailable"]))
    out.append(line("Tokens (in / out)", lambda s: f"{s['model']['tokens_in']:,} / {s['model']['tokens_out']:,}"))
    out.append(line("Model time", lambda s: f"{s['model']['seconds']:.0f} s"))
    return "\n".join(out) + "\n"
