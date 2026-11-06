"""Who an action is being done for, with their own ERPNext session.

When a person approves something in the console, the document it creates in ERPNext (a purchase order or requisition) is
created with THEIR OWN ERPNext login, so ERPNext records them as its creator and applies their own permissions. The session
lives only in server memory (see auth/sessions.py) and is handed down through this context for the duration of one request.
Approvals that arrive some other way (WhatsApp, an access key) have no such session; the document is then created by the
service account and carries the approver's name in a field instead.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional


@dataclass(frozen=True)
class ActingAs:
    user: str                                       # the ERPNext user id
    session: Any = field(repr=False)                # a logged-in requests session (never printed or stored)
    csrf: str = field(default="", repr=False)       # ERPNext's anti-forgery token for that session


_current: ContextVar[Optional[ActingAs]] = ContextVar("erp_agents_acting_as", default=None)


def current() -> Optional[ActingAs]:
    return _current.get()


@contextmanager
def acting_as(who: Optional[ActingAs]) -> Iterator[None]:
    token = _current.set(who)
    try:
        yield
    finally:
        _current.reset(token)
