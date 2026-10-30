"""Sign-in through ERPNext, so people and permissions are maintained in ONE place.

How it works
  1. The person types their ERPNext username and password. We send them straight to ERPNext's own login and keep
     nothing but the resulting ERPNext session, in memory, for the lifetime of the Stock Assistant session.
  2. With that session we ask ERPNext (as the person) who they are, which roles they hold, and what each doctype's
     permission table says. Nothing is guessed or maintained here.
  3. What they may do in this app follows from ERPNext's own permission table:
        look      can read one of the DocTypes the enabled packs name for looking (the stock pack: Item or Purchase Order; a purchaser needs no Item permission to be let in)
        decide    can create one of the DocTypes the enabled packs name for deciding (the stock pack: a Purchase Order, which is what approving a purchase order does for them)
        settings  holds the System Manager role (or is Administrator)

The agent's service account is still what talks to ERPNext for background checks and for creating the drafts; that is why "decide" requires the PERSON to be allowed to create
the document in ERPNext: the app never grants more than ERPNext itself would allow them. The DocTypes come from the packs' manifests (`erp_look`, `erp_decide`); the defaults
below are the stock pack's, so a bare ErpNextLogin behaves as it always did (tests/test_packs.py checks they match the manifest).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import quote

import requests

from .. import obs
from ..common import Actor
from ..httpguard import guard_session

log = logging.getLogger(__name__)

CAP_LOOK, CAP_DECIDE, CAP_SETTINGS = "look", "decide", "settings"
HIDDEN_ROLES = {"All", "Guest", "Desk User"}                 # ERPNext adds these to everybody; they say nothing about the person
LEVEL_ORDER = [CAP_SETTINGS, CAP_DECIDE, CAP_LOOK]
ADMIN_ROLE = "System Manager"
DEFAULT_LOOK = ("Item", "Purchase Order")           # a person who can READ any of these may look
DEFAULT_DECIDE = ("Purchase Order",)                # a person who can CREATE any of these may decide



def _cannot_reach(exc: Exception) -> "LoginError":
    """ERPNext could not be reached. Whoever asked is told only that; the address and the technical error (which name the internal server) go to the log."""
    log.warning("sign-in: ERPNext could not be reached: %s", exc)
    return LoginError("unreachable", "ERPNext cannot be reached right now.")

class LoginError(Exception):
    """code: invalid | locked | unreachable | no_access | verification_failed"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code, self.message = code, message or code


class Verification(Exception):
    """ERPNext wants a second step (a verification code) before it opens a session."""

    def __init__(self, tmp_id: str, prompt: str, session: Any) -> None:
        super().__init__("verification code needed")
        self.tmp_id, self.prompt, self.session = tmp_id, prompt, session


@dataclass(frozen=True)
class Identity:
    user: str
    full_name: str
    roles: tuple[str, ...]
    caps: frozenset[str]
    mobile: str = ""
    csrf: str = field(default="", repr=False, compare=False)       # ERPNext's anti-forgery token for this session (never sent to the browser)

    @property
    def level(self) -> str:
        return next((c for c in LEVEL_ORDER if c in self.caps), "none")

    def actor(self) -> Actor:
        role_of = {CAP_LOOK: "viewer", CAP_DECIDE: "approver", CAP_SETTINGS: "admin"}
        return Actor(id=self.user, roles=frozenset(role_of[c] for c in self.caps), via="erpnext", caps=self.caps)

    def public(self) -> dict[str, Any]:
        return {"user": self.user, "full_name": self.full_name, "level": self.level, "caps": sorted(self.caps),
                "roles": [r for r in self.roles if r not in HIDDEN_ROLES]}


def caps_from_boot(user: str, roles: set[str], can_read: set[str], can_create: set[str], look: tuple[str, ...] = DEFAULT_LOOK,
                   decide: tuple[str, ...] = DEFAULT_DECIDE) -> frozenset[str]:
    """What this person may do here, from the permissions ERPNext itself computed for them when it opened its desk page
    (`can_read` / `can_create` are the doctypes ERPNext lets THIS user read / create, user-level restrictions included)."""
    admin = user == "Administrator" or ADMIN_ROLE in roles
    may_create = user == "Administrator" or any(d in can_create for d in decide)
    caps: set[str] = set()
    if admin or may_create or any(d in can_read for d in look):
        caps.add(CAP_LOOK)
    if may_create:
        caps.add(CAP_DECIDE)
    if admin:
        caps.add(CAP_SETTINGS)
    return frozenset(caps)


def derive_caps(user: str, roles: set[str], item_perms: list[dict], order_perms: list[dict]) -> frozenset[str]:
    """What this person may do here, from ERPNext's own permission rows, for the default DocTypes (Item and Purchase Order). Pure function (unit tested)."""
    return derive_caps_for(user, roles, {"Item": item_perms, "Purchase Order": order_perms}, DEFAULT_LOOK, DEFAULT_DECIDE)


def derive_caps_for(user: str, roles: set[str], perms: dict[str, list[dict]], look: tuple[str, ...], decide: tuple[str, ...]) -> frozenset[str]:
    """What this person may do here, from ERPNext's own permission rows (`perms`: DocType -> its permission rows), for the DocTypes the packs name. Pure function."""
    everyone = {"All", *roles}

    def allowed(rows: list[dict], flag: str) -> bool:
        # ERPNext role permissions at level 0; rows that only apply to a document's owner are ignored (stricter)
        return any(int(r.get("permlevel") or 0) == 0 and r.get(flag) and not r.get("if_owner") and r.get("role") in everyone for r in rows)

    caps: set[str] = set()
    admin = user == "Administrator" or ADMIN_ROLE in roles
    may_create_orders = user == "Administrator" or any(allowed(perms.get(d, []), "create") for d in decide)
    if admin or may_create_orders or any(allowed(perms.get(d, []), "read") for d in look):
        caps.add(CAP_LOOK)
    if may_create_orders:
        caps.add(CAP_DECIDE)
    if admin:
        caps.add(CAP_SETTINGS)
    return frozenset(caps)


class ErpNextLogin:
    def __init__(self, base_url: str, session_factory: Callable[[], Any] = requests.Session, timeout: float = 20.0,
                 perm_cache_seconds: float = 900.0, clock: Callable[[], float] = time.monotonic, look: tuple[str, ...] = DEFAULT_LOOK,
                 decide: tuple[str, ...] = DEFAULT_DECIDE) -> None:
        self.base = base_url.rstrip("/")
        self.look, self.decide = tuple(look), tuple(decide)
        self.new_session, self.timeout, self.perm_ttl, self.clock = (lambda: guard_session(obs.observe_session(session_factory(), "erpnext"), "erpnext")), timeout, perm_cache_seconds, clock
        self._perms: dict[str, tuple[float, list[dict]]] = {}

    # ------------------------------------------------------------------ the two sign-in steps
    def start(self, username: str, password: str) -> tuple[Any, Identity]:
        """Password step. Returns (ERPNext session, identity). Raises LoginError, or Verification when a code is needed."""
        s = self.new_session()
        body = self._post(s, "/api/method/login", {"usr": username, "pwd": password}, allow=(200, 401, 403))
        data = body.json() if _is_json(body) else {}
        if body.status_code == 200 and (data.get("tmp_id") or data.get("verification")):
            v = data.get("verification") or {}
            raise Verification(str(data.get("tmp_id") or ""), str(v.get("prompt") or "Enter the verification code from your authenticator app."), s)
        if body.status_code != 200:
            text = str(data.get("message") or "").lower()
            raise LoginError("locked" if "lock" in text or "too many" in text else "invalid")
        return s, self.identity(s, require_access=True)

    def verify(self, session: Any, tmp_id: str, otp: str) -> Identity:
        """Second step (ERPNext two-factor)."""
        r = self._post(session, "/api/method/login", {"tmp_id": tmp_id, "otp": otp}, allow=(200, 401, 403))
        if r.status_code != 200:
            raise LoginError("verification_failed")
        return self.identity(session, require_access=True)

    def logout(self, session: Any) -> None:
        try:
            session.get(self.base + "/api/method/logout", timeout=self.timeout)
        except Exception:  # noqa: BLE001 - the local session is dropped either way
            log.info("could not tell ERPNext about the sign-out")

    # ------------------------------------------------------------------ who is this, and what may they do
    def identity(self, session: Any, require_access: bool = False) -> Identity:
        user = self._get(session, "/api/method/frappe.auth.get_logged_user").json().get("message")
        if not user or user == "Guest":
            raise LoginError("invalid", "the ERPNext session is no longer valid")
        doc = self._get(session, f"/api/resource/User/{quote(user, safe='')}", allow=(200, 403, 404))
        if doc.status_code != 200:
            raise LoginError("no_access", "could not read your ERPNext account")
        u = doc.json().get("data") or {}
        if not u.get("enabled", 1) or u.get("user_type") == "Website User":
            raise LoginError("no_access", "this ERPNext account cannot use the Stock Assistant")
        boot, csrf = self._boot(session)
        if boot is not None:                       # ERPNext's own answer: the roles and doctypes it lets this person read and create
            bu = boot.get("user") or {}
            roles = sorted({r for r in (bu.get("roles") or []) if isinstance(r, str)})
            caps = caps_from_boot(user, set(roles), set(bu.get("can_read") or []), set(bu.get("can_create") or []), self.look, self.decide)
        else:                                      # an ERPNext that does not show it: read the permission table ourselves (needs the person to see their roles)
            roles = sorted({r["role"] for r in u.get("roles", []) if r.get("role")})
            caps = derive_caps_for(user, set(roles), {d: self._doctype_perms(session, d) for d in dict.fromkeys([*self.look, *self.decide])}, self.look, self.decide)
        ident = Identity(user=user, full_name=u.get("full_name") or user, roles=tuple(r for r in roles if r not in HIDDEN_ROLES), caps=caps,
                         mobile=u.get("mobile_no") or "", csrf=csrf)
        if require_access and not caps:
            raise LoginError("no_access", f"{ident.full_name} does not have access")
        return ident

    def _boot(self, session: Any) -> tuple[Optional[dict], str]:
        """The data ERPNext embeds in its desk page for the signed-in person (roles, can_read, can_create), and the CSRF token
        that session needs to create documents. (None, '') when the page does not have them."""
        try:
            page = self._get(session, "/app", allow=(200,)).text
        except LoginError as exc:
            if exc.code == "unreachable":
                raise
            return None, ""
        m = re.search(r'csrf_token["\']?\s*[:=]\s*["\']([^"\']{10,})["\']', page)
        csrf = m.group(1) if m else ""
        j = re.search(r"frappe\.boot\s*=\s*", page)
        if not j:
            return None, csrf
        try:
            boot, _end = json.JSONDecoder().raw_decode(page[j.end():])
        except ValueError:
            return None, csrf
        return (boot if isinstance(boot, dict) and isinstance(boot.get("user"), dict) and "can_create" in boot["user"] else None), csrf

    def _doctype_perms(self, session: Any, doctype: str) -> list[dict]:
        hit = self._perms.get(doctype)
        if hit and self.clock() - hit[0] < self.perm_ttl:
            return hit[1]
        r = self._get(session, "/api/method/frappe.desk.form.load.getdoctype", params={"doctype": doctype})
        docs = r.json().get("docs") or []
        rows = (docs[0].get("permissions") if docs else None) or []
        self._perms[doctype] = (self.clock(), rows)
        return rows

    # ------------------------------------------------------------------ http
    def _get(self, session: Any, path: str, params: Optional[dict] = None, allow: tuple = (200,)):
        try:
            r = session.get(self.base + path, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise _cannot_reach(exc) from None
        if r.status_code not in allow:
            if r.status_code in (401, 403):
                raise LoginError("invalid", "the ERPNext session is no longer valid")
            raise LoginError("unreachable", f"ERPNext answered HTTP {r.status_code}")
        return r

    def _post(self, session: Any, path: str, body: dict, allow: tuple = (200,)):
        try:
            r = session.post(self.base + path, json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            raise _cannot_reach(exc) from None
        if r.status_code not in allow:
            raise LoginError("unreachable", f"ERPNext answered HTTP {r.status_code}")
        return r


def _is_json(resp: Any) -> bool:
    try:
        resp.json()
        return True
    except Exception:  # noqa: BLE001
        return False
