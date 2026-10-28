"""Browser and network hardening for the HTTP API (found and verified in the penetration test of 19 Oct 2026, docs/PENTEST_REPORT.md).

* Security headers on every answer. The console page also gets a Content-Security-Policy that allows only ITS OWN inline script (pinned by
  hash, computed from the file at start-up), so even a script that slipped into the page through some future bug would not run, and the page
  cannot be put inside another site's frame (clickjacking on the Approve buttons).
* A cap on request size, so nobody can make the server read hundreds of megabytes before it says "no".
* A strict test for "this request comes from the server machine itself" (used for the emergency access keys), and an optional mode in which
  everything except the channel webhooks is invisible to anyone arriving through a tunnel or proxy.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

log = logging.getLogger(__name__)

LOOPBACK = ("127.0.0.1", "::1", "localhost")
# Any of these means the request was relayed (a tunnel, a proxy, a CDN). A relayed request is never "the server machine".
PROXY_HEADERS = {"forwarded", "via", "true-client-ip", "cdn-loop", "x-real-ip", "x-client-ip", "x-originating-ip", "x-cluster-client-ip", "client-ip", "x-host"}
PROXY_PREFIXES = ("x-forwarded", "cf-", "fastly-", "x-envoy", "x-amzn", "x-azure", "x-arr-", "x-proxy", "x-ngrok", "ngrok-")
# Access keys that anybody can read in this project's documentation: never accepted from outside the server machine.
KNOWN_DEFAULT_TOKENS = {"demo-owner-token", "change-me", "changeme"}
MIN_KEY_LENGTH = 24

# bytes a request body may have, by path (the largest real one is a settings update; webhooks from WhatsApp/Telegram are a few KB)
BODY_LIMITS = (("/webhooks/", 1_000_000), ("/api/v1/auth/", 16_000), ("/api/", 256_000))
DEFAULT_BODY_LIMIT = 256_000


def hostname_of(host_header: str) -> str:
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def looks_proxied(headers: Any) -> bool:
    names = {k.lower() for k in headers.keys()}
    return bool(names & PROXY_HEADERS) or any(n.startswith(PROXY_PREFIXES) for n in names)


def is_local_request(request: Any) -> bool:
    """Only the person sitting at the server machine: the connection comes from the loopback address, nothing in the request says it was
    relayed, and it was addressed to a loopback name (a page on the internet cannot make a browser send `Host: localhost` to a tunnel)."""
    client = request.client.host if request.client else ""
    return client in ("127.0.0.1", "::1") and not looks_proxied(request.headers) and hostname_of(request.headers.get("host", "")) in LOOPBACK


ACCESS_KEY_MODES = ("local", "anywhere", "off")


def access_key_mode(auth_cfg: Optional[dict]) -> str:
    """Who may use the access keys from the settings file: 'local' (only on the server machine: the default, with or without ERPNext sign-in), 'anywhere' (any address
    that can reach the server; must be set explicitly) or 'off'. An unknown value is treated as the safe default."""
    value = str((auth_cfg or {}).get("access_keys") or "local").strip().lower()
    return value if value in ACCESS_KEY_MODES else "local"


def access_key_warning(auth_cfg: Optional[dict], has_keys: bool) -> Optional[str]:
    """The line to print at start-up when access keys are accepted from any address."""
    value = str((auth_cfg or {}).get("access_keys") or "").strip().lower()
    if value and value not in ACCESS_KEY_MODES:
        return f"WARNING: auth.access_keys is '{value[:20]}', which is not one of local, anywhere, off: access keys are accepted only on this machine."
    if has_keys and access_key_mode(auth_cfg) == "anywhere":
        return ("WARNING: auth.access_keys is 'anywhere': the access keys in the settings file are accepted from ANY address that can reach this server "
                "(the local network, a tunnel, a proxy). Anybody who learns a key gets that key's roles. Use long random keys, or set access_keys: local.")
    return None


def weak_key(token: str, min_length: int = MIN_KEY_LENGTH) -> bool:
    return len(token) < min_length or token.lower() in KNOWN_DEFAULT_TOKENS


def well_known_key_users(auth_cfg: Optional[dict]) -> list[str]:
    """The users whose configured access key is one that anybody can read in this project's documentation (never the key itself)."""
    return [str(t.get("user") or "?") for t in (auth_cfg or {}).get("tokens") or [] if str(t.get("token") or "").strip().lower() in KNOWN_DEFAULT_TOKENS]


def demo_key_problem(cfg: dict) -> Optional[str]:
    """Why the program must not start with this configuration, in words; None when it may. A well-known key is allowed only for a demo: `auth.allow_demo_key: true`
    (written as a real true) AND the built-in demo ERP. Anything else is refused at start-up (finding F-11, review of 25 Oct 2026)."""
    auth = cfg.get("auth") or {}
    users = well_known_key_users(auth)
    if not users:
        return None
    who = ", ".join(sorted(set(users)))
    if auth.get("allow_demo_key") is not True:
        return (f"the access key for {who} is a well-known demo key that anybody can read in this project's documentation, so the assistant will not start with it. "
                f"Set a long random key (at least {MIN_KEY_LENGTH} characters) as ERP_AGENTS_OWNER_TOKEN in .env. "
                "For a throw-away demo on the built-in demo ERP only, write auth.allow_demo_key: true in the settings file.")
    erp_type = str((cfg.get("erp") or {}).get("type") or "mock")
    if erp_type != "mock":
        return (f"auth.allow_demo_key is accepted only with the built-in demo ERP (erp.type: mock), and this settings file uses erp.type: {erp_type[:20]}, so the assistant "
                f"will not start with the well-known key for {who}. Set a long random ERP_AGENTS_OWNER_TOKEN in .env and remove allow_demo_key.")
    return None


def clean_for_log(value: Any, limit: int = 80) -> str:
    """Whatever a stranger typed, made safe to write into a log line (no line breaks or control characters that could forge entries)."""
    return re.sub(r"[^\x20-\x7e]", "?", str(value))[:limit]


# ---------------------------------------------------------------------------------------------- security headers
def script_hash(html_path: Path) -> Optional[str]:
    """CSP source for the page's single inline script, or None if the page does not have exactly one."""
    try:
        html = html_path.read_text(encoding="utf-8")
    except OSError:
        return None
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S | re.I)
    if len(scripts) != 1:
        return None
    return "'sha256-" + base64.b64encode(hashlib.sha256(scripts[0].encode("utf-8")).digest()).decode() + "'"


def console_policy(script_source: Optional[str]) -> str:
    # style-src needs 'unsafe-inline' because the page positions things with style attributes; scripts, the dangerous part, are pinned.
    scripts = script_source or "'none'"
    return "; ".join([
        "default-src 'none'",
        f"script-src {scripts}",
        "style-src 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "base-uri 'none'",
        "object-src 'none'",
        "frame-ancestors 'none'",
    ])


BASE_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
}


class SecurityHeaders:
    def __init__(self, app: Any, console_csp: str) -> None:
        self.app, self.console_csp = app, console_csp

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        req_headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        https = req_headers.get("x-forwarded-proto", "").lower() == "https" or '"https"' in req_headers.get("cf-visitor", "")

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in (b"server",)]
                have = {k.lower() for k, _ in headers}
                add = dict(BASE_HEADERS)
                if path.startswith("/ui") or path == "/":
                    add["content-security-policy"] = self.console_csp
                    add["cache-control"] = "no-cache"
                else:
                    add["cache-control"] = "no-store"
                    add["content-security-policy"] = "default-src 'none'; frame-ancestors 'none'"
                if https:
                    add["strict-transport-security"] = "max-age=31536000"
                headers += [(k.encode(), v.encode()) for k, v in add.items() if k.encode() not in have]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ---------------------------------------------------------------------------------------------- request size
class BodyTooLarge(Exception):
    pass


def body_limit_for(path: str) -> int:
    for prefix, limit in BODY_LIMITS:
        if path.startswith(prefix):
            return limit
    return DEFAULT_BODY_LIMIT


class BodyLimit:
    """Refuse a request whose body is bigger than the limit for its path, before it is read (Content-Length) or as soon as it grows past it (chunked)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = body_limit_for(scope.get("path", ""))
        declared = None
        for k, v in scope.get("headers", []):
            if k == b"content-length":
                try:
                    declared = int(v)
                except ValueError:
                    declared = -1
        if declared is not None and (declared > limit or declared < 0):
            return await _refuse(send, 413 if declared > limit else 400, "Request too large" if declared > limit else "Bad Content-Length")
        seen = 0
        started = False
        exceeded = False
        refusal = json.dumps({"detail": "Request too large"}).encode()

        async def counting_receive() -> dict:
            nonlocal seen, exceeded
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    exceeded = True
                    raise BodyTooLarge()
            return message

        async def tracking_send(message: dict) -> None:
            nonlocal started
            if exceeded:
                # the framework turned the cut-off into some other error (a 400, say); whatever it says, the answer is "too large"
                if message["type"] == "http.response.start":
                    started = True
                    await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"),
                                                                                           (b"content-length", str(len(refusal)).encode()), (b"connection", b"close")]})
                elif message["type"] == "http.response.body":
                    await send({"type": "http.response.body", "body": refusal})
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except BodyTooLarge:
            if not started:
                await _refuse(send, 413, "Request too large")


async def _refuse(send: Callable, status: int, text: str) -> None:
    body = json.dumps({"detail": text}).encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------------------------- remote access mode
class RemoteAccess:
    """`server.remote_access: webhooks_only` - to anybody who is not at the server machine (arriving through a tunnel or proxy) only the channel
    webhooks and the plain liveness check exist. The console, the sign-in and the API answer 404 as if they were not there."""

    OPEN = ("/webhooks/", "/health")

    def __init__(self, app: Any, mode: Callable[[], str]) -> None:
        self.app, self.mode = app, mode

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "http" and self.mode() == "webhooks_only":
            path = scope.get("path", "")
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            client = (scope.get("client") or ("", 0))[0]
            local = client in ("127.0.0.1", "::1") and not looks_proxied(headers) and hostname_of(headers.get("host", "")) in LOOPBACK
            if not local and not path.startswith(self.OPEN):
                return await _refuse(send, 404, "Not found")
        await self.app(scope, receive, send)
