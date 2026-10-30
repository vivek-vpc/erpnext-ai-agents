"""The penetration test of 19 Oct 2026 tried every route by script (T2: a stranger, T3: a person of a lower level, T9: hostile strings in every parameter). Here the same attacks run over every route the
application has, taken from the register in docs/SECURITY_CHECKS.md and compared with the routes the application really registers, so a route added without a decision about who may use it fails the build."""
import json

import pytest

from surface import HOSTILE, LEVELS, RANK, STATIC_AND_HIDDEN, TOKENS, call, make_surface, register, url_for


@pytest.fixture
def surface(make_app, clock):
    return make_surface(make_app, clock)

STRANGER_FORMS = [
    {},
    {"Authorization": "Bearer nonsense"},
    {"Authorization": "Bearer "},
    {"Authorization": "bearer"},
    {"Authorization": "Basic dXNlcjpwYXNz"},
    {"Authorization": "Bearer " + "A" * 4000},
]


def test_the_register_lists_exactly_the_routes_the_application_has(surface):
    _, _, _, ops = surface
    rows = register()
    in_register = {r.key for r in rows}
    assert len(in_register) == len(rows), "a route is listed twice in docs/SECURITY_CHECKS.md"
    real = set(ops) | STATIC_AND_HIDDEN
    assert real - in_register == set(), f"routes the application has that docs/SECURITY_CHECKS.md does not list (decide who may use each): {sorted(real - in_register)}"
    assert in_register - real == set(), f"routes listed in docs/SECURITY_CHECKS.md that the application does not have: {sorted(in_register - real)}"
    assert {r.level for r in rows} <= set(LEVELS)


def test_the_register_agrees_with_what_the_source_asks_for_on_each_route():
    """The level on each row is the one the route's own code asks for (`Depends(require("..."))`), read from the source, not guessed from behaviour."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "src" / "erp_agents"
    wanted = {}
    for path in (root / "core" / "api" / "app.py", root / "packs" / "stock_procurement" / "api.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.FunctionDef):
                continue
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in ("get", "post", "put", "delete") and dec.args:
                    roles = [sub.args[0].value for sub in ast.walk(node.args) if isinstance(sub, ast.Call) and getattr(sub.func, "attr", getattr(sub.func, "id", "")) == "require" and sub.args]
                    wanted[(dec.func.attr.upper(), dec.args[0].value.replace(":path", ""))] = roles[0] if roles else None
    protected = {r.key: r.level for r in register() if r.protected}
    assert len(protected) > 40
    for key, level in protected.items():
        assert wanted.get(key) == level, f"{key}: the register says {level}, the code asks for {wanted.get(key)}"
    for key, asks in wanted.items():
        if asks is not None:
            assert protected.get(key) == asks, f"{key}: the code asks for {asks} but the register does not list it at that level"


def test_a_stranger_is_turned_away_on_every_protected_route_in_six_ways(surface):
    _, _, client, ops = surface
    rows = [r for r in register() if r.protected]
    bad = []
    for row in rows:
        for form in STRANGER_FORMS:
            r = call(client, row, headers=form, ops=ops)
            if r.status_code != 401:
                bad.append((row, list(form.values())[:1], r.status_code))
    assert not bad, f"answered something other than 401 to a stranger: {bad[:8]}"


@pytest.mark.parametrize("persona", ["viewer", "approver", "admin"])
def test_each_level_gets_exactly_what_the_register_says(surface, persona):
    """A person below a route's level is refused with 403 (before the body is even read); a person at or above it is not refused, and nothing answers with a server error."""
    _, _, client, ops = surface
    bad = []
    for row in [r for r in register() if r.protected]:
        r = call(client, row, token=TOKENS[persona], ops=ops)
        refused = RANK[persona] < RANK[row.level]
        if refused and r.status_code != 403:
            bad.append((row, "should be refused", r.status_code))
        if not refused and (r.status_code in (401, 403) or r.status_code >= 500):
            bad.append((row, "should be allowed", r.status_code))
    assert not bad, f"{persona}: {bad[:8]}"


def test_a_person_who_may_only_look_can_change_nothing(surface):
    """T3 of 19 Oct: every route that changes or starts something refuses a viewer, except the two that are questions (the chat and the AI assistant's ask), which only read."""
    _, _, client, ops = surface
    questions = {("POST", "/api/v1/chat"), ("POST", "/api/v1/agents/ask")}
    for row in [r for r in register() if r.protected and r.method != "GET" and r.key not in questions]:
        assert call(client, row, token=TOKENS["viewer"], ops=ops).status_code == 403, row


def _leaks(r, payload):
    text = r.text
    problems = []
    if r.status_code >= 500:
        problems.append(f"server error {r.status_code}")
    low = text.lower()
    if "traceback" in low or "sqlite" in low or "syntax error" in low or 'file "' in low:
        problems.append("internal detail in the answer")
    if text.strip() in ("49", '"49"') or '"49"' in text and "7*7" in payload:
        problems.append("a template string was evaluated")
    if "set-cookie" in r.headers or any("a=b" in v for v in r.headers.values()):
        problems.append("a header was injected")
    if "\x1b[31m" in text and "\x1b[31m" in payload:
        problems.append("a terminal escape came back raw")
    return problems


def test_hostile_strings_in_every_path_and_query_parameter_cause_no_error_and_leak_nothing(surface):
    """T9 of 19 Oct: SQL, traversal, template, CRLF, NUL, 5 KB and invisible-character strings in every parameter of every route that takes one, signed in as an administrator."""
    _, _, client, ops = surface
    rows = [r for r in register() if r.protected and r.method == "GET"]
    tried, bad = 0, []
    for row in rows:
        op = ops[row.key]
        names = [p for p in op.get("parameters", []) if p["in"] in ("path", "query")]
        if not names:
            continue
        for payload in HOSTILE:
            query = {p["name"]: payload for p in names if p["in"] == "query"}
            r = client.get(url_for(row.path, payload), params=query, headers={"Authorization": f"Bearer {TOKENS['admin']}"})
            tried += 1
            bad += [(row.path, payload[:20], p) for p in _leaks(r, payload)]
    assert tried > 300, "the sweep should have tried every parameterised route with every string"
    assert not bad, bad[:8]


def _body_for(schema, components, payload, depth=0):
    """A request body in the shape the route asks for with every text field set to `payload`."""
    if "$ref" in schema:
        schema = components[schema["$ref"].split("/")[-1]]
    if "anyOf" in schema:
        options = [s for s in schema["anyOf"] if s.get("type") != "null"]
        return _body_for(options[0], components, payload, depth) if options else None
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        if not props:
            return {"x": payload}
        return {name: _body_for(sub, components, payload, depth + 1) for name, sub in props.items()} if depth < 4 else {}
    if kind == "array":
        return [_body_for(schema.get("items", {}), components, payload, depth + 1)]
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    return payload


def test_hostile_strings_in_every_text_field_of_every_request_body_cause_no_error_and_leak_nothing(surface):
    _, api, client, ops = surface
    components = api.openapi().get("components", {}).get("schemas", {})
    rows = [r for r in register() if r.protected and r.method in ("POST", "PUT") and "requestBody" in ops[r.key]]
    assert len(rows) >= 10
    tried, bad = 0, []
    for row in rows:
        schema = ops[row.key]["requestBody"]["content"]["application/json"]["schema"]
        for payload in HOSTILE:
            body = _body_for(schema, components, payload)
            r = call(client, row, token=TOKENS["admin"], value="x", ops=ops, body=body)
            tried += 1
            bad += [(row.path, payload[:20], p) for p in _leaks(r, payload)]
    assert tried > 150
    assert not bad, bad[:8]


def test_the_leak_check_itself_sees_a_leak():
    """The sweep is only worth something if it would notice: a fake answer that carries each kind of leak is reported."""
    class R:
        def __init__(self, status, text, headers=None):
            self.status_code, self.text, self.headers = status, text, headers or {}
    assert _leaks(R(500, "boom"), "x")
    assert _leaks(R(200, "Traceback (most recent call last)"), "x")
    assert _leaks(R(200, "sqlite3.OperationalError"), "x")
    assert _leaks(R(200, "49"), "${7*7}")
    assert _leaks(R(200, "{}", {"set-cookie": "a=b"}), "x")
    assert not _leaks(R(422, json.dumps({"detail": "bad"})), "x")
