"""The only things the model may use: numbered facts copied from what the agents recorded for one request.

Nothing here is computed for the model's benefit; every line restates stored data (the request text the owner
received, the rule checks of the analysis, the vendor scores of the proposal).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ai_munimji_engine.core.approvals.models import ApprovalRecord


@dataclass(frozen=True)
class Fact:
    id: str
    text: str


def _num(x: Any) -> str:
    if x is None:
        return "unknown"
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, (int, float)):
        text = f"{float(x):,.2f}".rstrip("0").rstrip(".")
        return "0" if text in ("-0", "") else text
    return str(x)


def build_facts(record: ApprovalRecord, analysis: Optional[dict[str, Any]] = None, ranking: Optional[dict[str, Any]] = None) -> list[Fact]:
    req = record.request
    lines: list[str] = [f"Request: {req.title}", f"Summary: {req.headline}", f"Severity: {req.severity}"]
    if record.status == "decided" and record.decision:
        d = record.decision
        lines.append(f"Decision: {d.action} by {d.actor_id} via {d.channel} on {d.decided_at:%d %b %Y %H:%M}")
    else:
        lines.append(f"Request status: {record.status}")
    for section in [*req.sections, *req.details]:
        lines += [f"{section.heading}: {line}" for line in section.lines]
    lines += [f"Possible answer: {o.label} - {o.description}" for o in req.options if o.description]

    if analysis:
        for r in analysis.get("rules", []):
            if not r.get("enabled"):
                lines.append(f"Rule {r['rule']} ({r['label']}): switched off")
                continue
            lines.append(f"Rule {r['rule']} ({r['label']}): value {_num(r.get('value'))}, threshold {_num(r.get('threshold'))}, "
                         f"{'TRIPPED' if r.get('tripped') else 'not tripped'}")
        u = analysis.get("usage") or {}
        if u:
            lines.append(f"Usage: planned {_num(u.get('planning_rate'))} per day (method {u.get('method')}); last {u.get('recent_days')} days "
                         f"{_num(u.get('recent_rate'))} per day; last {u.get('base_days')} days {_num(u.get('base_rate'))} per day; "
                         f"{u.get('data_days')} days of history")
        lines.append(f"Lead time {_num(analysis.get('lead_time_days'))} days ({analysis.get('lead_time_source')}); "
                     f"safety stock {_num(analysis.get('safety_days'))} days")
        lines.append(f"Stock: on hand {_num(analysis.get('on_hand'))}, available {_num(analysis.get('available'))}, "
                     f"position {_num(analysis.get('position'))} {analysis.get('uom', '')}".rstrip())
        for b in analysis.get("breakdown", []):
            if b.get("value") or b.get("use") not in ("not used",):
                lines.append(f"Stock column {b['label']}: {_num(b.get('value'))} ({b.get('use')})")
        if analysis.get("suggested_qty_basis"):
            lines.append(f"Suggested quantity: {_num(analysis.get('suggested_qty'))} because {analysis['suggested_qty_basis']}")
        lines += [f"Note: {n}" for n in analysis.get("notes", [])]

    if ranking:
        w = ranking.get("weights", {})
        if w:
            lines.append("Vendor weights: " + ", ".join(f"{k} {_num(v)}%" for k, v in w.items()))
        for v in ranking.get("vendors", []):
            if not v.get("eligible"):
                lines.append(f"Vendor {v['name']} ({v['supplier']}): not scored because {v.get('excluded_reason')}")
                continue
            s = v.get("scores", {})
            lines.append(
                f"Vendor {v['name']} ({v['supplier']}): unit price {_num(v.get('unit_price'))}, total {_num(v.get('total'))}, "
                f"delivery {_num(v.get('lead_days'))} days{' (assumed)' if v.get('lead_assumed') else ''}, "
                f"on-time {'unknown' if v.get('on_time_rate') is None else _num(v['on_time_rate'] * 100) + '%'}, "
                f"credit {_num(v.get('payment_days'))} days, past orders {v.get('orders')}, "
                f"scores cost {_num(s.get('cost'))} delivery {_num(s.get('delivery'))} reliability {_num(s.get('reliability'))} "
                f"terms {_num(s.get('terms'))}, total score {_num(v.get('score'))}"
                + (f"; not chosen: {v['why_not']}" if v.get("why_not") else "; chosen")
            )
        lines += [f"Vendor note: {n}" for n in ranking.get("notes", [])]

    seen: set[str] = set()
    facts: list[Fact] = []
    for line in lines:
        line = " ".join(str(line).split())
        if line and line not in seen:
            seen.add(line)
            facts.append(Fact(f"F{len(facts) + 1}", line))
    return facts
