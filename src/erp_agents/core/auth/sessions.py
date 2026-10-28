"""Stock Assistant sessions for people who signed in through ERPNext.

* The browser only ever holds an opaque random token; it maps to a record kept in MEMORY on the server (never on disk),
  so a restart signs everybody out and nothing sensitive is stored.
* Every `recheck_seconds` the person's ERPNext session is asked again who they are, so a disabled user or a removed
  role stops working here soon after it changes in ERPNext. If ERPNext cannot be reached at that moment the session is kept
  (a short outage must not lock out the people who handle it); it is re-asked on the next request.
* Wrong-password guessing is throttled here, because ERPNext locks an account after several failures and this public
  endpoint must not become a way to lock your staff out.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .. import obs
from ..acting import ActingAs
from .erpnext import ErpNextLogin, Identity, LoginError


def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Entry:
    identity: Identity
    session: Any
    created: float
    checked: float


class SessionStore:
    def __init__(self, provider: ErpNextLogin, clock: Callable[[], float] = time.time, ttl_seconds: float = 12 * 3600,
                 recheck_seconds: float = 15 * 60, max_sessions: int = 200, per_user_max: int = 5) -> None:
        self.provider, self.clock, self.ttl, self.recheck, self.max = provider, clock, ttl_seconds, recheck_seconds, max_sessions
        self.per_user_max = per_user_max
        self._items: dict[str, Entry] = {}
        self._pending: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def create(self, session: Any, identity: Identity) -> str:
        token = secrets.token_urlsafe(32)
        obs.register_secret(token, dynamic=True)                # the browser's token must not reach a log line, whatever logs it
        now = self.clock()
        with self._lock:
            self._expire(now)
            # One account cannot hold more than a handful of sessions (a phone, a laptop, a tablet): its oldest one goes first.
            mine = [k for k, e in self._items.items() if e.identity.user == identity.user]
            while len(mine) >= self.per_user_max:
                oldest = min(mine, key=lambda k: self._items[k].created)
                self._drop(oldest)
                mine.remove(oldest)
            if len(self._items) >= self.max:                       # make room rather than refuse a new person: from whoever holds the most sessions,
                heaviest = Counter(e.identity.user for e in self._items.values()).most_common(1)[0][0]       # so one account cannot push everybody else out
                oldest = min((k for k, e in self._items.items() if e.identity.user == heaviest), key=lambda k: self._items[k].created)
                self._drop(oldest)
            self._items[_key(token)] = Entry(identity, session, now, now)
        return token

    def get(self, token: str) -> Optional[Identity]:
        if not token:
            return None
        k = _key(token)
        now = self.clock()
        with self._lock:
            e = self._items.get(k)
            if e is None:
                return None
            if now - e.created > self.ttl:
                self._drop(k)
                return None
            due = now - e.checked > self.recheck
        if due:
            try:
                fresh = self.provider.identity(e.session, require_access=True)
            except LoginError as exc:
                if exc.code == "unreachable":
                    return e.identity
                with self._lock:
                    self._drop(k)
                return None
            with self._lock:
                e.identity, e.checked = fresh, now
        return e.identity

    def acting(self, token: str) -> Optional[ActingAs]:
        """The person's own ERPNext session, so a document can be created under their name. None for an unknown or ended session."""
        ident = self.get(token)
        if ident is None:
            return None
        with self._lock:
            e = self._items.get(_key(token))
        return ActingAs(user=ident.user, session=e.session, csrf=ident.csrf) if e else None

    def end(self, token: str) -> None:
        k = _key(token or "")
        with self._lock:
            e = self._items.get(k)
            if e is None:
                return
            self._drop(k)
        self.provider.logout(e.session)

    # ---- the verification-code step: the half-open ERPNext session waits here for a few minutes
    def hold_pending(self, tmp_id: str, session: Any) -> None:
        with self._lock:
            now = self.clock()
            self._pending = {k: v for k, v in self._pending.items() if now - v[0] < 300}
            self._pending[tmp_id] = (now, session)

    def take_pending(self, tmp_id: str) -> Optional[Any]:
        with self._lock:
            hit = self._pending.pop(tmp_id, None)
        return hit[1] if hit and self.clock() - hit[0] < 300 else None

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def _expire(self, now: float) -> None:
        for k in [k for k, e in self._items.items() if now - e.created > self.ttl]:
            self._drop(k)

    def _drop(self, k: str) -> None:
        self._items.pop(k, None)


class LoginThrottle:
    """At most `limit` failed attempts per `window` seconds for each key (client address, and address + username)."""

    def __init__(self, limit: int = 8, window: float = 300.0, clock: Callable[[], float] = time.time) -> None:
        self.limit, self.window, self.clock = limit, window, clock
        self._fails: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> deque:
        q = self._fails.get(key)
        if q is None:
            return deque()
        now = self.clock()
        while q and now - q[0] > self.window:
            q.popleft()
        if not q:
            del self._fails[key]                                 # forget addresses that have been quiet: the table must not grow without end
            return deque()
        return q

    def blocked(self, *keys: str) -> bool:
        with self._lock:
            return any(len(self._recent(k)) >= self.limit for k in keys)

    def fail(self, *keys: str) -> None:
        with self._lock:
            now = self.clock()
            for k in keys:
                q = self._recent(k)
                q.append(now)
                self._fails[k] = q
            if len(self._fails) > 5000:                          # a flood of different addresses: drop whatever has expired
                for stale in [k for k in list(self._fails) if not self._recent(k)]:
                    self._fails.pop(stale, None)

    def clear(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._fails.pop(k, None)
