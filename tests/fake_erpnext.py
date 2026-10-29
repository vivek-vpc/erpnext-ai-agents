"""A small stateful ERPNext behind a `requests` adapter, so the REAL ERPNextClient runs against it, and a demo ERP whose documents are created through that client.

Not a test file (nothing here is collected); the fault-injection tests of Phase 3 use it.

FakeERPNext stores the documents it is sent, answers the few reads the creating methods make (the custom fields, the company, the supplier, the documents of a reference), and can
go wrong in the ways a real server does, one fault per document POST, in the order given:

    "lose"     the document is stored, then the connection drops before the answer arrives
    "timeout"  the document is stored, then the answer takes too long (a read timeout)
    "partial"  the document is stored and ERPNext answers 200, but the body is cut off
    "nameless" the document is stored and the answer is valid JSON that has no document name
    "500", "503"  nothing is stored; the server answers with that error
    "refuse"   nothing is stored; ERPNext answers 417 with a message (a validation error)
    "ok"       no fault this time
"""
from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlsplit

import requests
from requests.adapters import BaseAdapter

from erp_agents.core.erp.erpnext import ERPNextClient
from erp_agents.core.erp.mock import MockERP

PREFIX = {"Purchase Order": "PUR-ORD-2026-", "Material Request": "MAT-MR-2026-", "Stock Entry": "MAT-STE-2026-"}


class FakeERPNext(BaseAdapter):
    def __init__(self, faults: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.docs: dict[str, list[dict[str, Any]]] = {}
        self.faults = list(faults)
        self.fields_exist = True
        self.sent: list[tuple[str, str]] = []            # (method, path) of every request that reached the server

    # ------------------------------------------------------------------ what a test looks at
    def stored(self, doctype: str) -> list[dict[str, Any]]:
        return self.docs.get(doctype, [])

    # ------------------------------------------------------------------ the server
    def send(self, request, **kwargs):  # noqa: ANN001 - the adapter interface
        split = urlsplit(request.url)
        path = unquote(split.path)
        self.sent.append((request.method, path))
        if request.method == "GET":
            return self._get(request, path, parse_qs(split.query))
        if request.method == "POST" and path.startswith("/api/resource/"):
            return self._post(request, path.removeprefix("/api/resource/"))
        return self._reply(request, 404, {"exception": "not found"})

    def close(self) -> None:
        pass

    def _get(self, request, path: str, query: dict[str, list[str]]):  # noqa: ANN001
        if path.startswith("/api/resource/Custom Field/"):
            return self._reply(request, 200 if self.fields_exist else 404, {"data": {}} if self.fields_exist else {"exception": "not found"})
        if path.startswith("/api/resource/Company/") or path.startswith("/api/resource/Supplier/"):
            return self._reply(request, 200, {"data": {"default_currency": "INR"}})
        doctype = path.removeprefix("/api/resource/")
        if doctype in PREFIX:
            filters = json.loads((query.get("filters") or ["[]"])[0])
            ref = next((f[2] for f in filters if f[0] == "custom_agent_ref"), None)
            fields = json.loads((query.get("fields") or ['["name"]'])[0])
            rows = [{k: d.get(k) for k in fields} for d in self.stored(doctype) if ref is not None and d.get("custom_agent_ref") == ref and d.get("docstatus", 0) < 2]
            return self._reply(request, 200, {"data": rows})
        return self._reply(request, 404, {"exception": f"no such thing: {path}"})

    def _post(self, request, doctype: str):  # noqa: ANN001
        if doctype == "Custom Field":
            return self._reply(request, 200, {"data": {"name": "created"}})
        body = json.loads(request.body or b"{}")
        fault = self.faults.pop(0) if self.faults else "ok"
        if fault in ("500", "503"):
            return self._reply(request, int(fault), {"exception": "the server is having trouble"})
        if fault == "refuse":
            return self._reply(request, 417, {"exception": "ValidationError", "_server_messages": json.dumps([json.dumps({"message": "Supplier S-A is disabled"})])})
        rows = self.docs.setdefault(doctype, [])
        name = f"{PREFIX[doctype]}{len(rows) + 1:05d}"
        rows.append({**body, "name": name, "docstatus": 0})
        if fault == "lose":
            raise requests.ConnectionError("the connection was reset before the answer arrived")
        if fault == "timeout":
            raise requests.ReadTimeout("the answer took too long")
        if fault == "partial":
            return self._reply(request, 200, None, raw=('{"data": {"name": "' + name)[:-4].encode())
        if fault == "nameless":
            return self._reply(request, 200, {"data": {}})
        return self._reply(request, 200, {"data": {"name": name}})

    @staticmethod
    def _reply(request, status: int, body: Optional[dict[str, Any]], raw: Optional[bytes] = None):  # noqa: ANN001
        r = requests.Response()
        r.status_code = status
        r._content = raw if raw is not None else json.dumps(body).encode()
        r.url, r.request = request.url, request
        r.headers["Content-Type"] = "application/json"
        return r


def rest_client(server: FakeERPNext) -> ERPNextClient:
    """The real REST client, talking to `server`."""
    client = ERPNextClient("https://erp.example.test", "KEY", "SECRET", company="Test Co")
    client.session.mount("https://", server)
    return client


class HybridERP(MockERP):
    """Reads come from the demo ERP (items, vendors, stock); every document is created through the REAL REST client against `server`."""

    def __init__(self, server: FakeERPNext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.server = server
        self.client = rest_client(server)

    def create_purchase_order(self, draft, acting=None):  # noqa: ANN001
        return self.client.create_purchase_order(draft, acting)

    def create_material_request(self, draft, acting=None):  # noqa: ANN001
        return self.client.create_material_request(draft, acting)

    def create_stock_transfer(self, draft, acting=None):  # noqa: ANN001
        return self.client.create_stock_transfer(draft, acting)
