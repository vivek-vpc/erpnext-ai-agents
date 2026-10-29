"""HTTP API: admin settings, inventory views, the approval inbox for a future mobile app, and channel webhooks.

Auth: `Authorization: Bearer <token>`; tokens map to a user and roles in settings.yaml.
  viewer   - read everything
  approver - + answer approval requests
  admin    - + change settings (thresholds, schedule, ...)
Webhooks are authenticated by the channel's own signature/secret instead.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..approvals.inbound import process_inbound
from ..auth.erpnext import LoginError, Verification
from .. import metrics, resilience
from ..acting import acting_as
from ..bootstrap import App
from ..common import Actor
from ..health import readiness
from ..settings_store import SettingsError
from .observability import RequestContext
from .hardening import MIN_KEY_LENGTH, BodyLimit, RemoteAccess, SecurityHeaders, access_key_mode, clean_for_log, console_policy, is_local_request, script_hash, weak_key

log = logging.getLogger(__name__)


class PolicyUpdate(BaseModel):
    set: dict[str, Any] = Field(default_factory=dict, description="Fields to override. null switches a nullable threshold off.")
    clear: list[str] = Field(default_factory=list, description="Fields to inherit again.")


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=140)
    password: str = Field(min_length=1, max_length=200)


class VerifyBody(BaseModel):
    tmp_id: str = Field(min_length=1, max_length=200)
    otp: str = Field(min_length=1, max_length=12)


class SystemUpdate(BaseModel):
    set: dict[str, Any]


class AgentCheckBody(BaseModel):
    provider: Literal["ollama", "anthropic", "openai", "gemini"]
    base_url: str = Field("", max_length=300)
    model: str = Field("", max_length=120)


class DecisionBody(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    note: str = ""


MAX_LIST_LIMIT = 500          # no list endpoint returns more rows than this in one request


def create_app(app_ctx: App, on_system_change=None, lifespan=None) -> FastAPI:
    ctx = app_ctx
    server_cfg = ctx.config.get("server") or {}
    # The interactive API documentation lists every route to anybody who asks. Off unless the administrator turns it on (`server.docs: true`).
    docs = bool(server_cfg.get("docs", False))
    mcp_apps: list[Any] = []                              # the MCP endpoint (mcp.http: true), whose session manager must run while the server is up

    @asynccontextmanager
    async def run_lifespans(a: FastAPI):
        async with AsyncExitStack() as stack:
            if lifespan is not None:
                await stack.enter_async_context(lifespan(a))
            for sub in mcp_apps:
                await stack.enter_async_context(sub.router.lifespan_context(sub))
            yield

    api = FastAPI(title="ERP Agents", version="0.1.0", lifespan=run_lifespans,
                  docs_url="/docs" if docs else None, redoc_url="/redoc" if docs else None, openapi_url="/openapi.json" if docs else None)
    if on_system_change:
        # the system and agent settings reschedule, and so does any section a pack names in `reschedule_scopes` (the stock pack: its follow-up times)
        ctx.settings.on_change(lambda scope, key: on_system_change() if scope in ("system", "agents", *(getattr(ctx, "reschedule_scopes", None) or ())) else None)

    # ------------------------------------------------------------------ auth
    def bearer(request: Request) -> str:
        header = request.headers.get("authorization", "")
        return header[7:] if header.lower().startswith("bearer ") else ""

    def key_allowed(request: Request) -> bool:
        """Access keys (the settings-file keys) are the administrator's emergency way in, not something every user needs. They are accepted only from the
        server machine itself (never through the public tunnel), with ERPNext sign-in and in the demo alike; `auth.access_keys: anywhere` (set explicitly,
        and warned about at start-up) or `off` changes that."""
        mode = access_key_mode(ctx.config.get("auth"))
        if mode == "off":
            return False
        if mode == "anywhere":
            return True
        return is_local_request(request)

    def current_actor(request: Request) -> Actor:
        token = bearer(request)
        if ctx.sessions is not None:                       # a person who signed in through ERPNext
            ident = ctx.sessions.get(token)
            if ident is not None:
                return ident.actor()
        # otherwise an access key from the settings file (the administrator's emergency way in)
        # constant-time comparison against every configured token
        found = None
        for t, actor in (ctx.tokens.items() if key_allowed(request) else ()):
            if hmac.compare_digest(t.encode(), token.encode()):
                found = actor
        # a short or well-known key (the demo's, or one printed in the documentation) is good enough on your own machine, never from outside it
        min_len = int((ctx.config.get("auth") or {}).get("min_key_length", MIN_KEY_LENGTH))
        if found is not None and weak_key(token, min_len) and not is_local_request(request):
            found = None
        if found is None:
            raise HTTPException(401, "Missing or invalid token")
        return found

    def require(role: str):
        def dep(actor: Actor = Depends(current_actor)) -> Actor:
            if not actor.has_role(role):
                raise HTTPException(403, f"{role} role required")
            return actor
        return dep

    # ------------------------------------------------------------------ sign-in through ERPNext
    def client_ip(request: Request) -> str:
        host = request.client.host if request.client else "?"
        if host in ("127.0.0.1", "::1"):                   # behind the local tunnel the real address is in this header
            return request.headers.get("cf-connecting-ip", host)
        return host

    def refuse(status: int, code: str, message: str):
        raise HTTPException(status, {"code": code, "message": message})

    def finish_login(session: Any, identity: Any, ip: str, keys: tuple) -> dict:
        ctx.throttle.clear(*keys)
        token = ctx.sessions.create(session, identity)
        log.info("%s signed in through ERPNext from %s (level %s)", clean_for_log(identity.user), clean_for_log(ip, 45), identity.level)
        return {"status": "ok", "token": token, **identity.public()}

    @api.get("/api/v1/auth/mode")
    def auth_mode(request: Request) -> dict:
        # the sign-in screen only offers the access key where it would be accepted (the server machine), so ordinary users never see it
        return {"erpnext": ctx.sessions is not None, "access_key": bool(ctx.tokens) and key_allowed(request)}

    @api.post("/api/v1/auth/login")
    def auth_login(body: LoginBody, request: Request) -> dict:
        if ctx.sessions is None:
            refuse(404, "not_enabled", "Sign-in with ERPNext is not turned on.")
        ip = client_ip(request)
        keys = (f"ip:{ip}", f"user:{ip}:{body.username.strip().lower()}")
        if ctx.throttle.blocked(*keys):
            refuse(429, "throttled", "Too many wrong tries. Wait a few minutes and try again.")
        try:
            session, ident = ctx.login.start(body.username.strip(), body.password)
        except Verification as v:
            ctx.sessions.hold_pending(v.tmp_id, v.session)
            return {"status": "verification", "tmp_id": v.tmp_id, "prompt": v.prompt}
        except LoginError as exc:
            if exc.code in ("invalid", "locked"):
                ctx.throttle.fail(*keys)
                log.warning("failed sign-in for %s from %s (%s)", clean_for_log(body.username.strip()), clean_for_log(ip, 45), exc.code)
            status = {"invalid": 401, "locked": 423, "no_access": 403, "unreachable": 503}.get(exc.code, 401)
            refuse(status, exc.code, exc.message)
        return finish_login(session, ident, ip, keys)

    @api.post("/api/v1/auth/verify")
    def auth_verify(body: VerifyBody, request: Request) -> dict:
        if ctx.sessions is None:
            refuse(404, "not_enabled", "Sign-in with ERPNext is not turned on.")
        ip = client_ip(request)
        keys = (f"ip:{ip}", f"code:{ip}:{body.tmp_id}")
        if ctx.throttle.blocked(*keys):
            refuse(429, "throttled", "Too many wrong tries. Wait a few minutes and try again.")
        session = ctx.sessions.take_pending(body.tmp_id)
        if session is None:
            refuse(401, "verification_failed", "That sign-in expired. Start again.")
        try:
            ident = ctx.login.verify(session, body.tmp_id, body.otp.strip())
        except LoginError as exc:
            ctx.throttle.fail(*keys)
            if exc.code == "verification_failed":
                ctx.sessions.hold_pending(body.tmp_id, session)          # let them try the code again
            refuse(403 if exc.code == "no_access" else 401, exc.code, exc.message)
        return finish_login(session, ident, ip, keys)

    @api.get("/api/v1/auth/me")
    def auth_me(request: Request, actor: Actor = Depends(require("viewer"))) -> dict:
        ident = ctx.sessions.get(bearer(request)) if ctx.sessions is not None else None
        if ident is not None:
            return ident.public()
        caps = [c for c, r in (("look", "viewer"), ("decide", "approver"), ("settings", "admin")) if actor.has_role(r)]
        level = "settings" if "settings" in caps else "decide" if "decide" in caps else "look" if caps else "none"
        return {"user": actor.id, "full_name": actor.id, "level": level, "caps": caps, "roles": [], "access_key": True}

    @api.post("/api/v1/auth/logout")
    def auth_logout(request: Request) -> dict:
        if ctx.sessions is not None:
            ctx.sessions.end(bearer(request))
        return {"ok": True}

    # ------------------------------------------------------------------ health
    @api.get("/health")
    def health(request: Request) -> dict[str, Any]:
        """Anybody may ask whether the service is alive. Only a signed-in person is told more (it asks ERPNext, and names the channels)."""
        try:
            current_actor(request)
        except HTTPException:
            return {"ok": True}
        return {"ok": True, "erp_reachable": ctx.erp.healthcheck(), "channels": sorted(ctx.channels),
                "dependencies": resilience.BREAKERS.states(include=("erpnext",))}          # closed = asked as usual, open = not asked for a while because it keeps failing

    @api.get("/ready")
    def ready(request: Request) -> JSONResponse:
        """Can it do its work? (/health only says it is alive.) Anybody may ask and is told yes or no, with 503 for no; a signed-in person also gets the checks and why."""
        checks = readiness(ctx)
        ok = all(c.ok for c in checks)
        body: dict[str, Any] = {"ready": ok}
        try:
            current_actor(request)
            body["checks"] = [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks]
        except HTTPException:
            pass
        return JSONResponse(body, status_code=200 if ok else 503)

    # ------------------------------------------------------------------ metrics: not there at all unless `observability.metrics: true`
    if ctx.observability is not None and ctx.observability.metrics:
        @api.get("/metrics", include_in_schema=False)
        def metrics_page(request: Request) -> PlainTextResponse:
            """Prometheus text. Allowed with the scraper's own bearer token (ERP_AGENTS_METRICS_TOKEN, 24 characters or more) or an administrator's sign-in; nobody else."""
            token, presented = os.environ.get(metrics.SCRAPER_ENV, "").strip(), bearer(request)
            if not (len(token) >= MIN_KEY_LENGTH and presented and hmac.compare_digest(presented.encode(), token.encode())):
                if not current_actor(request).has_role("admin"):         # a stranger is turned away first (401)
                    raise HTTPException(403, "admin role required")
            return PlainTextResponse(metrics.REGISTRY.render(), media_type=metrics.CONTENT_TYPE)

    # ------------------------------------------------------------------ packs
    @api.get("/api/v1/packs")
    def list_packs(_: Actor = Depends(require("viewer"))) -> list[dict]:
        """Every installed pack: what it is, whether it is on, whether its code is loaded. Switching one off takes effect at once; switching on one that was off when the application
        started needs a restart (`restart_required`). Change it with PUT /api/v1/settings/module/packs {"set": {"enabled": {"<name>": true}}}."""
        rows = ctx.packs.describe()
        for row in rows:
            row["restart_required"] = bool(row["enabled"] and not row["loaded"] and row["status"] != "skeleton")
        return rows

    # ------------------------------------------------------------------ settings (admin)
    @api.get("/api/v1/settings/schema")
    def schema(_: Actor = Depends(require("viewer"))) -> dict:
        return ctx.settings.schemas()

    @api.get("/api/v1/settings/system")
    def get_system(_: Actor = Depends(require("viewer"))) -> dict:
        return ctx.settings.system().model_dump()

    @api.put("/api/v1/settings/system")
    def put_system(body: SystemUpdate, actor: Actor = Depends(require("admin"))) -> dict:
        try:
            return ctx.settings.update_system(actor, body.set).model_dump()
        except SettingsError as exc:
            raise HTTPException(422, str(exc))

    # Generic module settings (procurement, assistant, and any future module registered in SettingsStore).
    @api.get("/api/v1/settings/module/{kind}")
    def get_module_settings(kind: str, _: Actor = Depends(require("viewer"))) -> dict:
        try:
            return {"effective": ctx.settings.section(kind).model_dump(mode="json"), "overrides": ctx.settings._section_overrides(kind)}
        except SettingsError as exc:
            raise HTTPException(404, str(exc))

    @api.put("/api/v1/settings/module/{kind}")
    def put_module_settings(kind: str, body: PolicyUpdate, actor: Actor = Depends(require("admin"))) -> dict:
        try:
            ctx.settings.update_section(actor, kind, body.set, body.clear)
        except SettingsError as exc:
            raise HTTPException(422, str(exc))
        return {"effective": ctx.settings.section(kind).model_dump(mode="json"), "overrides": ctx.settings._section_overrides(kind)}

    @api.get("/api/v1/settings/audit")
    def audit(limit: int = Query(100, ge=1, le=MAX_LIST_LIMIT), scope_type: Optional[str] = None, scope_key: Optional[str] = None,
              _: Actor = Depends(require("viewer"))) -> list[dict]:
        return ctx.settings.audit(limit, scope_type, scope_key)

    # ------------------------------------------------------------------ the AI agents (off until switched on in Settings; long tasks run in the background and are polled)
    def visible_task(row: dict, actor: Actor, trace: bool = False) -> dict:
        """A question and its answer belong to the person who asked it (and the administrator); the exact messages sent to the model are for the administrator only."""
        if row["task"] == "ask" and row["asked_by"] != actor.id and not actor.has_role("admin"):
            raise HTTPException(404, "not found")
        if not (trace and actor.has_role("admin")):
            row = {k: v for k, v in row.items() if k != "trace"}
        return row

    @api.get("/api/v1/agents/status")
    def agents_status(check: bool = False, _: Actor = Depends(require("viewer"))) -> dict:
        return ctx.agents.status(check_model=check)

    @api.get("/api/v1/agents/privacy")
    def agents_privacy(_: Actor = Depends(require("viewer"))) -> dict:
        from ..agents.privacy import privacy_summary
        return privacy_summary(ctx.agents.settings, ctx.packs.privacy_lines())

    @api.post("/api/v1/agents/check")
    def agents_check(body: AgentCheckBody, _: Actor = Depends(require("admin"))) -> dict:
        try:
            return ctx.agents.check(body.provider, body.base_url, body.model)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @api.get("/api/v1/agents/usage")
    def agents_usage(days: int = 30, _: Actor = Depends(require("viewer"))) -> dict:
        return ctx.agents.usage(max(1, min(days, 365)))

    @api.get("/api/v1/agents/tasks")
    def agents_tasks(agent: Optional[str] = None, task: Optional[str] = None, limit: int = 20, actor: Actor = Depends(require("viewer"))) -> list[dict]:
        out = []
        for row in ctx.agent_log.list(agent=agent, task=task, limit=limit):
            try:
                out.append(visible_task(row, actor))
            except HTTPException:
                continue
        return out

    @api.get("/api/v1/agents/tasks/{task_id}")
    def agents_task(task_id: str, trace: bool = False, actor: Actor = Depends(require("viewer"))) -> dict:
        row = ctx.agent_log.get(task_id)
        if row is None:
            raise HTTPException(404, "not found")
        return visible_task(row, actor, trace)

    @api.get("/api/v1/work-orders")
    def work_orders(status: Optional[str] = None, type: Optional[str] = None, _: Actor = Depends(require("viewer"))) -> list[dict]:
        return ctx.workorders.list(status, type)

    # ------------------------------------------------------------------ approval inbox (mobile app backend)
    @api.get("/api/v1/approvals")
    def list_approvals(status: Optional[str] = "pending", limit: int = Query(50, ge=1, le=MAX_LIST_LIMIT), _: Actor = Depends(require("approver"))) -> list[dict]:
        return [r.model_dump(mode="json") for r in ctx.approvals.list(status, limit)]

    @api.get("/api/v1/approvals/{approval_id}")
    def get_approval(approval_id: str, _: Actor = Depends(require("approver"))) -> dict:
        rec = ctx.approvals.get(approval_id)
        if rec is None:
            raise HTTPException(404, "not found")
        return rec.model_dump(mode="json")

    @api.post("/api/v1/approvals/{approval_id}/decision")
    def decide(approval_id: str, body: DecisionBody, request: Request, actor: Actor = Depends(require("approver"))) -> dict:
        # A person signed in through ERPNext: whatever this approval creates in ERPNext is created with THEIR OWN ERPNext login,
        # so ERPNext records them as the creator and applies their permissions. Any other caller has no such session.
        who = ctx.sessions.acting(bearer(request)) if ctx.sessions is not None else None
        with acting_as(who):
            result = ctx.approvals.decide(approval_id, body.action, actor, "app", body.params, body.note)
        code = {"decided": 200, "info": 200, "revised": 200, "need_input": 200, "refused": 422, "already_decided": 409, "expired": 410, "cancelled": 410,
                "not_found": 404, "invalid_action": 422, "forbidden": 403}.get(result.status, 500)
        if code != 200:
            raise HTTPException(code, result.message)
        return result.model_dump(mode="json")

    # ------------------------------------------------------------------ "Why did you do that?": the decision history (history.py) and its seal (audit.py)
    def history_filters(q: str, kind: Optional[str], item: Optional[str], who: Optional[str], since: Optional[str], until: Optional[str], status: Optional[str]) -> dict:
        return {"q": q, "kind": kind, "item": item, "who": who, "since": since, "until": until, "status": status}

    @api.get("/api/v1/history")
    def history(q: str = Query("", max_length=200), kind: Optional[str] = Query(None, max_length=60), item: Optional[str] = Query(None, max_length=140),
                who: Optional[str] = Query(None, max_length=140), since: Optional[str] = None, until: Optional[str] = None, status: Optional[str] = Query(None, max_length=20),
                limit: int = Query(50, ge=1, le=MAX_LIST_LIMIT), offset: int = Query(0, ge=0), _: Actor = Depends(require("approver"))) -> dict:
        """Every past decision with its reasoning, newest first: requests the program raised and how they were answered, and changes of settings. Words must all appear."""
        try:
            return ctx.history.search(limit=limit, offset=offset, **history_filters(q, kind, item, who, since, until, status))
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @api.get("/api/v1/history.csv")
    def history_csv(q: str = Query("", max_length=200), kind: Optional[str] = Query(None, max_length=60), item: Optional[str] = Query(None, max_length=140),
                    who: Optional[str] = Query(None, max_length=140), since: Optional[str] = None, until: Optional[str] = None, status: Optional[str] = Query(None, max_length=20),
                    _: Actor = Depends(require("approver"))) -> PlainTextResponse:
        try:
            text = ctx.history.csv(**history_filters(q, kind, item, who, since, until, status))
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return PlainTextResponse(text, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="decision-history.csv"'})

    @api.get("/api/v1/history/verify")
    def history_verify(_: Actor = Depends(require("admin"))) -> dict:
        """Is the decision history as it was sealed? Lists any request or settings change changed or deleted afterwards."""
        ctx.audit.sweep()
        return ctx.audit.verify()

    @api.get("/api/v1/history/{approval_id}")
    def history_entry(approval_id: str, _: Actor = Depends(require("approver"))) -> dict:
        """One decision in full: everything the person saw, the answer, what came of it, earlier versions, the AI's work on it, and what the packs add."""
        out = ctx.history.entry(approval_id)
        if out is None:
            raise HTTPException(404, "not found")
        return out

    # ------------------------------------------------------------------ packs: each one that is switched on adds its routes (and may bring the console)
    deps = SimpleNamespace(require=require, MAX_LIST_LIMIT=MAX_LIST_LIMIT)

    def pack_gate(name: str):
        def gate(request: Request) -> None:
            current_actor(request)                           # a stranger is turned away first (401): nobody learns which packs exist
            if not ctx.packs.is_enabled(name):               # switched off after start-up: its routes answer 404 at once
                raise HTTPException(404, "not found")
        return gate

    web_dir: Optional[Path] = None
    for name in ctx.packs.enabled_names():
        pack = ctx.packs.loaded.get(name)
        if pack is None:
            continue
        router = APIRouter(dependencies=[Depends(pack_gate(name))])
        pack.routes(router, ctx, deps)
        api.include_router(router)
        web_dir = web_dir or pack.web_dir()

    # ------------------------------------------------------------------ the console (static; talks to the API above), from the pack that has screens
    if web_dir is not None and web_dir.is_dir():
        api.mount("/ui", StaticFiles(directory=web_dir, html=True), name="ui")

        @api.get("/", include_in_schema=False)
        def root() -> RedirectResponse:
            return RedirectResponse("/ui/")

    # ------------------------------------------------------------------ channel webhooks
    # GET answers a channel's verification request (WhatsApp), POST receives its messages.
    # Registered separately so each method gets its own OpenAPI operation id.
    @api.get("/webhooks/{channel_type}", operation_id="webhook_get")
    async def webhook_get(channel_type: str, request: Request):
        channel = ctx.channels.get(channel_type)
        if channel is None or not channel.supports_webhook:
            raise HTTPException(404, "unknown channel")
        handled = channel.handle_verification(dict(request.query_params))
        if handled is None:
            raise HTTPException(404, "not supported")
        status, text = handled
        return PlainTextResponse(text, status_code=status)

    @api.post("/webhooks/{channel_type}", operation_id="webhook_post")
    async def webhook_post(channel_type: str, request: Request):
        channel = ctx.channels.get(channel_type)
        if channel is None or not channel.supports_webhook:
            raise HTTPException(404, "unknown channel")
        body = await request.body()
        if not channel.verify_webhook(dict(request.headers), dict(request.query_params), body):
            raise HTTPException(401, "bad signature")
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(400, "invalid JSON")
        for event in channel.parse_webhook(payload):
            try:
                process_inbound(event, ctx.router, ctx.approvals, ctx.conversation)
            except Exception:  # noqa: BLE001 - always 200 so the platform does not retry forever
                log.exception("failed processing %s event", channel_type)
        return {"ok": True}

    # ------------------------------------------------------------------ MCP over HTTP (off unless `mcp.http: true`; needs the optional 'agents' extra)
    mcp_cfg = ctx.config.get("mcp") or {}
    if mcp_cfg.get("http"):
        from starlette.routing import Route
        from ..mcp_server import http_endpoint, remote_policy_problem, remote_warning

        def who_is_calling(scope: dict) -> Optional[Actor]:
            try:
                return current_actor(Request(scope))
            except HTTPException:
                return None

        problem = remote_policy_problem(mcp_cfg)
        if problem:
            raise ValueError(problem)
        warning = remote_warning(mcp_cfg)
        if warning:
            log.warning(warning)
        remote_ok = bool(mcp_cfg.get("remote"))

        def reachable(scope: dict) -> bool:                 # private by default: the server machine only, never through a tunnel or proxy
            return remote_ok or is_local_request(Request(scope))

        endpoint, sub_app = http_endpoint(ctx.toolkit, who_is_calling, mcp_cfg.get("allowed_hosts") or None, mcp_cfg.get("allowed_origins") or None, reachable)
        api.router.routes.append(Route("/mcp", endpoint=endpoint))
        mcp_apps.append(sub_app)

    # Layers, innermost first: size limit, then the remote-access mode, then security headers on whatever answer comes out (even a refusal).
    api.add_middleware(BodyLimit)
    api.add_middleware(RemoteAccess, mode=lambda: str((ctx.config.get("server") or {}).get("remote_access", "full")).lower())
    api.add_middleware(SecurityHeaders, console_csp=console_policy(script_hash(web_dir / "index.html") if web_dir is not None else None))
    api.add_middleware(RequestContext)                         # outermost: the request id is bound for everything inside, and returned in X-Request-ID
    return api
