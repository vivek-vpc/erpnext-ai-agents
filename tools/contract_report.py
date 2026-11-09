"""Runs the shared ERP-client checks (ai_munimji_engine.testkit.contract) on the demo ERP or on the REST client against a live ERPNext, and writes the results as JSON in the shape
that the Frappe app's runner writes (AI-Munimji dev/scripts/contract_report.py), so that `tools/contract_table.py` can set the three side by side.

    python tools/contract_report.py demo --out demo.json
    python tools/contract_report.py rest --site contract-site.json --url http://localhost:8000 --key K --secret S --out rest.json [--only name,name]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote


def demo(today: date):
    from erp_agents.core.erp.world_erp import world_site

    return world_site(today=today)


def rest(args):
    from ai_munimji_engine.testkit import contract
    from erp_agents.core.erp.erpnext import ERPNextClient

    described = json.loads(Path(args.site).read_text(encoding="utf-8"))
    client = ERPNextClient(args.url, args.key, args.secret, company=described["company"])
    base = client.base_url

    def submit(doctype: str, name: str) -> None:
        doc = client.session.get(f"{base}/api/resource/{quote(doctype)}/{quote(name)}").json()["data"]
        done = client.session.post(f"{base}/api/method/frappe.client.submit", json={"doc": json.dumps(doc)})
        assert done.status_code < 400, f"submit {doctype} {name}: {done.status_code} {done.text[:200]}"

    def discard(doctype: str, name: str) -> None:
        client.session.delete(f"{base}/api/resource/{quote(doctype)}/{quote(name)}")

    return client, contract.Site(today=date.fromisoformat(described["today"]), names=described["names"], submit=submit, discard=discard)


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("client", choices=["demo", "rest"])
    p.add_argument("--out", required=True)
    p.add_argument("--site", help="rest: the JSON the loader wrote")
    p.add_argument("--url", default=os.environ.get("ERP_CONTRACT_URL"))
    p.add_argument("--key", default=os.environ.get("ERP_CONTRACT_KEY"))
    p.add_argument("--secret", default=os.environ.get("ERP_CONTRACT_SECRET"))
    p.add_argument("--only")
    args = p.parse_args(argv)
    from ai_munimji_engine.testkit import contract

    if args.client == "demo":
        client, site = demo(date.today())
    else:
        if not (args.site and args.url and args.key and args.secret):
            print("rest needs --site, --url, --key and --secret")
            return 2
        client, site = rest(args)
    only = {n for n in (args.only or "").split(",") if n} or None
    results = contract.run_checks(client, site, only=only)
    for r in results:
        print(f"{r.status.upper():5} {r.name}" + (f"\n        {r.detail}" if r.detail else ""))
    print(f"\n{sum(r.status == 'pass' for r in results)} pass, {sum(r.status == 'fail' for r in results)} fail, {sum(r.status == 'error' for r in results)} error, "
          f"{sum(r.status == 'skip' for r in results)} skip ({args.client})")
    Path(args.out).write_text(json.dumps([r.__dict__ for r in results], indent=1), encoding="utf-8")
    return 0 if all(r.status == "pass" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
