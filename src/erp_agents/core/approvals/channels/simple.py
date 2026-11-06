"""Console (development) and Inbox (backend for your own mobile app) channels."""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from ai_munimji_engine.core.approvals.models import ApprovalRecord, DeliveryResult
from ai_munimji_engine.core.approvals.render import render_plain
from ai_munimji_engine.core.approvals.channels.base import Channel

log = logging.getLogger(__name__)


class ConsoleChannel(Channel):
    """Prints requests to the terminal. Decide with:  erp-agents approvals decide <id> <action>"""

    type = "console"

    def __init__(self, writer: Optional[Callable[[str], Any]] = None) -> None:
        self.writer = writer or print
        self.sent: list[str] = []

    def _emit(self, text: str) -> None:
        self.sent.append(text)
        self.writer(text)

    def send_approval(self, record: ApprovalRecord, address: Optional[str]) -> DeliveryResult:
        opts = " | ".join(f"{o.action} ({o.label})" for o in record.request.options)
        self._emit(
            "=" * 72 + "\n" + render_plain(record.request) +
            f"\n\nREQUEST {record.id}   options: {opts}\n  -> erp-agents approvals decide {record.id} <action>\n" + "=" * 72
        )
        return DeliveryResult(ok=True, external_id=record.id)

    def send_text(self, address: Optional[str], text: str) -> DeliveryResult:
        self._emit(text)
        return DeliveryResult(ok=True)


class InboxChannel(Channel):
    """The in-app approval inbox.

    Approvals already live in the database, so 'delivery' just means: it shows up in
    GET /api/v1/approvals for that user. A future mobile app polls (or receives a push through
    `push_hook`) and answers via POST /api/v1/approvals/{id}/decision - the same path every other
    channel ends up in.
    """

    type = "inbox"

    def __init__(self, push_hook: Optional[Callable[[ApprovalRecord, Optional[str]], None]] = None) -> None:
        self.push_hook = push_hook   # e.g. send an FCM/APNs push; failures never block delivery

    def send_approval(self, record: ApprovalRecord, address: Optional[str]) -> DeliveryResult:
        if self.push_hook:
            try:
                self.push_hook(record, address)
            except Exception:  # noqa: BLE001 - the inbox already has the request; a failing push hook must not lose it
                log.exception("the inbox push hook failed")
        return DeliveryResult(ok=True, external_id=record.id)

    def send_text(self, address: Optional[str], text: str) -> DeliveryResult:
        return DeliveryResult(ok=True)
