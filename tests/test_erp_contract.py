"""The shared ERP-client checks (ai_munimji_engine.testkit.contract), run on the demo ERP in every suite, and on the REST client against a live ERPNext when one is named.

The demo ERP always runs. The REST client runs only when the loader's description of a site is given (ERP_CONTRACT_SITE: the JSON that `python -m tools.testdata.cli contract`
writes) with the site's address and an API key (ERP_CONTRACT_URL, ERP_CONTRACT_KEY, ERP_CONTRACT_SECRET): the suite on GitHub has no ERPNext, so it skips them.

The Frappe client runs the same checks in the app (AI-Munimji, ai_munimji/tests/test_erp_contract.py), on a bench.
"""
import json
import os
from datetime import date
from pathlib import Path
from urllib.parse import quote

import pytest

from ai_munimji_engine.testkit import contract
from erp_agents.core.erp.world_erp import world_site

NAMES = [c.name for c in contract.CHECKS]
TODAY = date(2026, 11, 6)
LIVE = os.environ.get("ERP_CONTRACT_SITE") and os.environ.get("ERP_CONTRACT_URL") and os.environ.get("ERP_CONTRACT_KEY") and os.environ.get("ERP_CONTRACT_SECRET")


def test_there_are_checks_and_each_has_a_distinct_name():
    assert len(NAMES) >= 40 and len(set(NAMES)) == len(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_the_demo_erp(name):
    erp, site = world_site(today=TODAY)
    [result] = contract.run_checks(erp, site, only={name})
    assert result.status == "pass", f"{result.status}: {result.detail}"


def test_the_demo_erp_passes_every_check_in_one_run():
    erp, site = world_site(today=TODAY)
    results = contract.run_checks(erp, site)
    assert [r for r in results if r.status != "pass"] == []
    assert len(results) == len(NAMES)


def live_client_and_site():
    from erp_agents.core.erp.erpnext import ERPNextClient

    described = json.loads(Path(os.environ["ERP_CONTRACT_SITE"]).read_text(encoding="utf-8"))
    client = ERPNextClient(os.environ["ERP_CONTRACT_URL"], os.environ["ERP_CONTRACT_KEY"], os.environ["ERP_CONTRACT_SECRET"], company=described["company"])
    base = client.base_url

    def submit(doctype: str, name: str) -> None:
        doc = client.session.get(f"{base}/api/resource/{quote(doctype)}/{quote(name)}").json()["data"]
        done = client.session.post(f"{base}/api/method/frappe.client.submit", json={"doc": json.dumps(doc)})
        assert done.status_code < 400, f"submit {doctype} {name}: {done.status_code} {done.text[:200]}"

    def discard(doctype: str, name: str) -> None:
        client.session.delete(f"{base}/api/resource/{quote(doctype)}/{quote(name)}")           # a cancelled or draft document the check made; absent is fine

    return client, contract.Site(today=date.fromisoformat(described["today"]), names=described["names"], submit=submit, discard=discard)


@pytest.mark.skipif(not LIVE, reason="no live ERPNext named (ERP_CONTRACT_SITE, _URL, _KEY, _SECRET)")
@pytest.mark.parametrize("name", NAMES)
def test_the_rest_client_on_a_live_erpnext(name):
    client, site = live_client_and_site()
    [result] = contract.run_checks(client, site, only={name})
    assert result.status == "pass", f"{result.status}: {result.detail}"
