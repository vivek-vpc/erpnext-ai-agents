"""The surface the security tests sweep: the register of routes in docs/SECURITY_CHECKS.md, the application built with one access key per level, and the hostile strings of the penetration test of 19 Oct 2026
(tools in the scratchpad of that review: pentest.py, T9). Tests that need these import them from here, so the register, the personas and the payloads are written once."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from erp_agents.core.api.app import create_app
from erp_agents.core.erp.mock import MockERP

ROOT = Path(__file__).resolve().parent.parent
REGISTER = ROOT / "docs" / "SECURITY_CHECKS.md"

LEVELS = ("public", "self", "viewer", "approver", "admin")
RANK = {"viewer": 0, "approver": 1, "admin": 2}
TOKENS = {"viewer": "viewer-key-0123456789", "approver": "approver-key-0123456789", "admin": "admin-key-0123456789"}
STATIC_AND_HIDDEN = {("GET", "/"), ("GET", "/ui/{path}"), ("GET", "/metrics")}      # real routes that the OpenAPI schema leaves out (the console, its redirect, the metrics page that exists only when switched on)

HOSTILE = [
    "x' OR '1'='1", "x'; DROP TABLE alerts;--", "%00", "x\x00y", "../../etc/passwd", "..\\..\\windows\\win.ini", "A" * 5000, "${7*7}", "{{7*7}}", "{vendor.__class__}", "%s%n",
    "<script>1</script>", "<img src=x onerror=alert(1)>", "x%0d%0aSet-Cookie:a=b", "x\r\nSet-Cookie: a=b", "x‮gpj.exe", "x​‍y", "\x1b[31mred",
]


class Row:
    def __init__(self, method: str, path: str, level: str, why: str) -> None:
        self.method, self.path, self.level, self.why = method, path, level, why

    @property
    def key(self) -> tuple:
        return (self.method, self.path)

    @property
    def protected(self) -> bool:
        return self.level in RANK

    def __repr__(self) -> str:
        return f"{self.method} {self.path} [{self.level}]"


def table(name: str, text: str = "") -> list:
    """The rows of a `<!-- name -->` table in the register, as lists of cells."""
    found = re.search(rf"<!-- {name} -->(.*?)<!-- /{name} -->", text or REGISTER.read_text(encoding="utf-8"), re.S)
    assert found, f"{REGISTER.name} has no <!-- {name} --> table"
    lines = [l for l in found.group(1).splitlines() if l.strip().startswith("|")]
    return [[c.strip() for c in l.strip().strip("|").split("|")] for l in lines[2:]]


def register() -> list:
    rows = []
    for cells in table("routes"):
        method, path, level, why = cells[0], cells[1].strip("`"), cells[2], cells[3]
        rows.append(Row(method, path, level, why))
    return rows


def build(make_app, clock, cfg_extra=None):
    """The application with three access keys (one per level) and the demo ERP, and a client that shows a server error as a 500 instead of raising it."""
    cfg = {"auth": {"tokens": [{"token": TOKENS["viewer"], "user": "vera", "roles": ["viewer"]}, {"token": TOKENS["approver"], "user": "alex", "roles": ["approver"]},
                               {"token": TOKENS["admin"], "user": "olga", "roles": ["admin", "approver"]}]}}
    for k, v in (cfg_extra or {}).items():
        cfg[k] = {**cfg.get(k, {}), **v}
    app = make_app(erp=MockERP(today=clock().date()), cfg=cfg)
    api = create_app(app)
    return app, api, TestClient(api, raise_server_exceptions=False)


def schema_operations(api) -> dict:
    """(METHOD, path) -> the OpenAPI operation, for every route the schema lists (the schema is built from the routes, whether or not the documentation pages are on)."""
    return {(m.upper(), p): op for p, ops in api.openapi()["paths"].items() for m, op in ops.items() if m in ("get", "post", "put", "delete", "patch")}


def url_for(path: str, value: str = "x") -> str:
    from urllib.parse import quote
    return re.sub(r"\{[^}]+\}", lambda m: quote(value, safe=""), path)


def call(client, row_or_key, token=None, value="x", headers=None, ops=None, body=None):
    """One call to a route with every path parameter set to `value`, the parameters the route needs to be harmless (a dry run, a quantity) and an empty body where it takes one."""
    method, path = row_or_key.key if isinstance(row_or_key, Row) else row_or_key
    op = (ops or {}).get((method, path)) or {}
    names = {p["name"]: p for p in op.get("parameters", [])}
    params = {}
    if "dry_run" in names:
        params["dry_run"] = "true"
    if "qty" in names:
        params["qty"] = "1"
    hdr = dict(headers or {})
    if token is not None:
        hdr["Authorization"] = f"Bearer {token}"
    kw = {"params": params, "headers": hdr}
    if body is not None:
        kw["json"] = body
    elif method in ("POST", "PUT", "PATCH") and "requestBody" in op:
        kw["json"] = {}
    return client.request(method, url_for(path, value), **kw)


def make_surface(make_app, clock, cfg_extra=None):
    """(app, api, client, operations): what the sweeps need. Each test module wraps it in its own `surface` fixture."""
    app, api, client = build(make_app, clock, cfg_extra)
    return app, api, client, schema_operations(api)
