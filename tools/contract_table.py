"""Sets the results of the shared ERP-client checks side by side: one row per check, one column per client.

    python tools/contract_table.py "Demo ERP=demo.json" "REST client=rest.json" "Frappe client=frappe.json" [more "Label=file.json" ...] [--out table.md]

Each file is the JSON that `tools/contract_report.py` (demo ERP, REST client) or the Frappe app's `dev/scripts/contract_report.py` writes: a list of {name, status, detail}. The rows follow the order
of the checks in `ai_munimji_engine.testkit.contract`; a check missing from a file shows as "-". Below the table, every row where the columns do not agree is listed with the detail of each column that
did not pass, so that a difference is explained rather than counted."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SHOWN = {"pass": "pass", "fail": "FAIL", "error": "ERROR", "skip": "skip", None: "-"}


def load(spec: str) -> tuple[str, dict[str, dict]]:
    label, _, path = spec.partition("=")
    if not path:
        raise SystemExit(f"{spec!r}: write it as Label=file.json")
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    return label, {r["name"]: r for r in rows}


def table(columns: list[tuple[str, dict[str, dict]]]) -> str:
    from ai_munimji_engine.testkit import contract

    order = [c.name for c in contract.CHECKS]
    known = set(order)
    order += sorted({n for _, rows in columns for n in rows if n not in known})
    writes = {c.name: c.writes for c in contract.CHECKS}
    labels = [label for label, _ in columns]
    out = ["| Check | Kind | " + " | ".join(labels) + " |", "|---|---|" + "---|" * len(labels)]
    for name in order:
        cells = [SHOWN.get((rows.get(name) or {}).get("status"), (rows.get(name) or {}).get("status")) for _, rows in columns]
        out.append(f"| {name} | {'writes' if writes.get(name) else 'reads'} | " + " | ".join(cells) + " |")
    totals = []
    for _, rows in columns:
        counts = {s: sum(1 for r in rows.values() if r["status"] == s) for s in ("pass", "fail", "error", "skip")}
        totals.append(f"{counts['pass']} pass" + "".join(f", {counts[s]} {s}" for s in ("fail", "error", "skip") if counts[s]))
    out.append("| **Total** | | " + " | ".join(totals) + " |")

    differing = [n for n in order if len({(rows.get(n) or {}).get("status") for _, rows in columns}) > 1]
    out += ["", f"**{len(differing)} of {len(order)} checks do not give the same result in every column.**" if differing else f"**All {len(order)} checks give the same result in every column.**"]
    for name in differing:
        out += ["", f"* `{name}`"]
        for label, rows in columns:
            r = rows.get(name)
            if r and r["status"] != "pass":
                out.append(f"  * {label}: {SHOWN[r['status']]}" + (f", {r['detail'].strip().splitlines()[0][:240]}" if r.get("detail") else ""))
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("columns", nargs="+", help='"Label=file.json", in the order of the columns')
    ap.add_argument("--out", help="write the table to this file instead of printing it")
    args = ap.parse_args()
    text = table([load(c) for c in args.columns])
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
