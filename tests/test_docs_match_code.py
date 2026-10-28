"""The documents say what the code does (Phase 0 of the hardening review): the number of tools, and every kind of ERPNext document the assistant writes.

Both numbers used to be typed by hand and drifted (the README said 15 tools when there were 16, then 17). They are now between marker comments in the documents, and these
tests compute the same text from the tool registry, the pack manifest and the ERPNext client, and fail when the two differ.
"""
import re
from pathlib import Path

import pytest

from erp_agents.core.packs import PackRegistry
from erp_agents.core.toolkit import PREPARE, READ

ROOT = Path(__file__).resolve().parent.parent
COUNT_FILES = ["README.md", "docs/AGENTS.md", "src/erp_agents/packs/stock_procurement/README.md"]
WRITE_FILES = ["README.md", "docs/AGENTS.md"]
COUNTS = re.compile(r"<!-- tools:counts -->(.*?)<!-- /tools:counts -->", re.S)
WRITES = re.compile(r"<!-- erp-writes -->(.*?)<!-- /erp-writes -->", re.S)
ERPNEXT_CLIENT = ROOT / "src" / "erp_agents" / "core" / "erp" / "erpnext.py"


def counts_text(app):
    specs = app.toolkit.specs()
    return f"{len(specs)} tools: {sum(s.effect == READ for s in specs)} read-only and {sum(s.effect == PREPARE for s in specs)} prepare-only"


@pytest.mark.parametrize("path", COUNT_FILES)
def test_the_tool_counts_in_the_documents_come_from_the_registry(make_app, path):
    found = COUNTS.findall((ROOT / path).read_text(encoding="utf-8"))
    assert found, f"{path} has no <!-- tools:counts -->...<!-- /tools:counts --> block"
    assert [f.strip() for f in found] == [counts_text(make_app())] * len(found)


def test_every_registered_tool_is_described_in_agents_md(make_app):
    text = (ROOT / "docs" / "AGENTS.md").read_text(encoding="utf-8")
    missing = [s.name for s in make_app().toolkit.specs() if not re.search(r"`" + re.escape(s.name) + r"[`(]", text)]
    assert not missing, f"docs/AGENTS.md does not describe: {', '.join(missing)}"


def test_the_prepare_only_tools_are_the_ones_that_can_lead_to_a_document(make_app):
    names = sorted(s.name for s in make_app().toolkit.specs() if s.effect == PREPARE)
    assert names == ["prepare_order_request", "prepare_quote_request", "prepare_stock_transfer"]


# ---------------------------------------------------------------------------------------------------- what is written to ERPNext
def documented(path):
    blocks = WRITES.findall((ROOT / path).read_text(encoding="utf-8"))
    assert len(blocks) == 1, f"{path} needs exactly one <!-- erp-writes --> block"
    return set(re.findall(r"^\| `([^`]+)` \|", blocks[0], re.M))


def allowed_by_the_manifest():
    needs = PackRegistry.discover().manifests["stock_procurement"].erp_permissions
    return {doctype for doctype, action in needs if action == "create"}


def written_by_the_client():
    src = ERPNEXT_CLIENT.read_text(encoding="utf-8")
    found = set(re.findall(r'_post_document\("([^"]+)"', src))                                    # drafts created for a person's decision
    found |= set(re.findall(r'\.post\(f"\{self\.base_url\}/api/resource/([A-Za-z ]+)"', src))        # a direct post of one named document type
    if "communication.email.make" in src:
        found.add("Communication")                                                                  # e-mail through the ERP's own mail account
    return found


@pytest.mark.parametrize("path", WRITE_FILES)
def test_the_documents_list_exactly_what_the_assistant_writes_to_erpnext(path):
    in_code = written_by_the_client()
    assert in_code >= {"Purchase Order", "Material Request", "Stock Entry", "Communication", "Custom Field"}      # the scan itself still sees the writes it is meant to see
    assert documented(path) == in_code, f"{path}: documented {sorted(documented(path))}, the ERPNext client writes {sorted(in_code)}"


def test_every_document_the_assistant_may_create_is_in_the_manifest_or_is_the_one_time_setup():
    # Custom Field is created only by the admin command `erp-agents erp-setup`, which needs more than the service account's day-to-day permissions
    assert written_by_the_client() - {"Custom Field"} == allowed_by_the_manifest()


def test_the_client_never_submits_a_document():
    src = ERPNEXT_CLIENT.read_text(encoding="utf-8")
    assert "frappe.client.submit" not in src and '"docstatus": 1' not in src and "'docstatus': 1" not in src
