"""WhatsApp Business Cloud API channel (Meta).

Sends the full reasoning as a text message, then an interactive message with the choices
(reply buttons when there are <= 3 options, otherwise a list). Replies arrive on a webhook
(`/webhooks/whatsapp`), verified with Meta's X-Hub-Signature-256 HMAC.

Known platform limits (not handled in this build):
  * Business-initiated free-form messages are only delivered inside the 24h customer-service window;
    outside it WhatsApp accepts them and then silently drops them. With `template_name` configured the
    channel therefore sends that approved template FIRST (one body variable {{1}} = a one-line summary,
    three quick-reply buttons: Start reorder / Remind me later / Ignore), then the full reasoning as a
    normal message (delivered while the window is open). `template_first: false` restores the old order
    (free-form first, template only if Meta rejects it with error 131047). If the template send fails the
    channel falls back to the free-form messages.
  * Reply-button titles are limited to 20 characters, list rows to 24.
Payload shapes follow Meta's published Cloud API documentation; verify against your sandbox number.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Mapping, Optional

import requests

from ...netguard import no_redirects
from ..models import ApprovalRecord, ApprovalRequest, DeliveryResult, InboundEvent
from ..render import render_whatsapp, truncate
from .base import Channel, decode_action, encode_action, safe_equals

log = logging.getLogger(__name__)
MAX_TEXT = 3800          # text message limit is 4096
MAX_INTERACTIVE_BODY = 1000   # interactive body limit is 1024
MAX_TEMPLATE_VAR = 900        # template body is limited to 1024 characters including the fixed text
# Lines taken from each section for the one-line template summary (inventory alerts, purchase-order proposals).
SUMMARY_LINES = {"Situation": 3, "Why you are seeing this": 1, "Suggested action": 1,
                 "Recommendation": 3, "Why this vendor": 1, "Other vendors": 2}
OUTSIDE_WINDOW = "(code 131047)"   # "re-engagement message": no customer message in the last 24h


def _as_bool(value: Any) -> bool:
    return value.strip().lower() not in ("", "false", "no", "0", "off") if isinstance(value, str) else bool(value)


def template_summary(req: ApprovalRequest) -> str:
    """One line of the key facts for the template variable (no newlines allowed): headline, the situation
    lines, the main reason and the suggested order. The full reasoning is in the follow-up message."""
    def one(line: str) -> str:
        return " ".join(line.split())

    parts = [one(req.headline)]
    for section in req.sections:                         # how many lines of each section make the summary
        parts += [one(line) for line in section.lines[:SUMMARY_LINES.get(section.heading, 0)]]
    return truncate(" | ".join(p for p in parts if p), MAX_TEMPLATE_VAR, suffix="…")


class WhatsAppChannel(Channel):
    type = "whatsapp"
    supports_webhook = True
    needs_address = True

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        app_secret: Optional[str] = None,
        verify_token: Optional[str] = None,
        api_version: str = "v20.0",
        http: Any = None,
        api_base: str = "https://graph.facebook.com",
        template_name: Optional[str] = None,
        template_language: str = "en",
        template_first: bool = True,
        kind_templates: Optional[dict[str, str]] = None,
    ) -> None:
        if not access_token or not phone_number_id:
            raise ValueError("whatsapp: access_token and phone_number_id are required")
        self.template_name = template_name or None
        self.template_language = template_language or "en"
        self.template_first = _as_bool(template_first)
        # Per request kind: its own template (button wording differs), or "" for no template. Unlisted kinds use template_name.
        self.kind_templates = {k: (v or "") for k, v in (kind_templates or {}).items()}
        self.access_token = access_token
        self.app_secret = app_secret
        self.verify_token = verify_token
        self.http = http or no_redirects(requests.Session())          # the access token is in every request's header
        self.url = f"{api_base.rstrip('/')}/{api_version}/{phone_number_id}/messages"

    # ---------------------------------------------------------------- low level
    def _post(self, payload: dict[str, Any]) -> DeliveryResult:
        try:
            resp = self.http.post(
                self.url, json={"messaging_product": "whatsapp", **payload},
                headers={"Authorization": f"Bearer {self.access_token}"}, timeout=15,
            )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(ok=False, error=f"whatsapp: {exc}")
        if resp.status_code >= 400 or "error" in data:
            err = data.get("error", {})
            return DeliveryResult(ok=False, error=f"whatsapp: {err.get('message', 'HTTP ' + str(resp.status_code))} (code {err.get('code')})")
        msgs = data.get("messages") or [{}]
        return DeliveryResult(ok=True, external_id=msgs[0].get("id"))

    # ---------------------------------------------------------------- outbound
    def send_text(self, address: Optional[str], text: str) -> DeliveryResult:
        if not address:
            return DeliveryResult(ok=False, error="whatsapp: recipient has no number")
        return self._post({"to": address, "type": "text", "text": {"body": truncate(text, MAX_TEXT), "preview_url": False}})

    def send_approval(self, record: ApprovalRecord, address: Optional[str]) -> DeliveryResult:
        if not address:
            return DeliveryResult(ok=False, error="whatsapp: recipient has no number")
        req = record.request
        tried_template = False
        template = self._template_for(record)
        if template and self.template_first:
            # An approved template is delivered even when the owner has not written in the last 24h, so it goes
            # first. The full reasoning follows as a normal message: delivered while the window is open, silently
            # dropped by WhatsApp otherwise (the summary and the console still carry it).
            tried_template = True
            sent = self._send_template(record, address, template)
            if sent.ok:
                self.send_text(address, render_whatsapp(req))
                return sent
            log.warning("whatsapp template send failed (%s) - falling back to a normal message", sent.error)
        first = self.send_text(address, render_whatsapp(req))
        if not first.ok:
            # Outside the 24h customer-service window Meta only accepts an approved template message.
            if template and not tried_template and OUTSIDE_WINDOW in (first.error or ""):
                return self._send_template(record, address, template)
            return first
        body = truncate(f"{req.headline}\n\nChoose an action:", MAX_INTERACTIVE_BODY, suffix="…")
        opts = req.options
        if len(opts) <= 3:
            interactive: dict[str, Any] = {
                "type": "button",
                "body": {"text": body},
                "action": {"buttons": [
                    {"type": "reply", "reply": {"id": encode_action(record.id, o.action), "title": o.label[:20]}} for o in opts
                ]},
            }
        else:
            interactive = {
                "type": "list",
                "body": {"text": body},
                "action": {"button": "Choose action", "sections": [{"title": "Actions", "rows": [
                    {"id": encode_action(record.id, o.action), "title": o.label[:24], "description": (o.description or "")[:72]}
                    for o in opts[:10]
                ]}]},
            }
        return self._post({"to": address, "type": "interactive", "interactive": interactive})

    def send_template_text(self, address: str, template: str, text: str) -> DeliveryResult:
        """An approved template with one body variable, for a message to someone who may not have written in the last 24 hours (a pack's message to a business
        partner). The text becomes the variable, on one line."""
        if not address:
            return DeliveryResult(ok=False, error="whatsapp: recipient has no number")
        body = truncate(" ".join(text.split()), MAX_TEMPLATE_VAR, suffix="…")
        return self._post({"to": address, "type": "template",
                           "template": {"name": template, "language": {"code": self.template_language},
                                        "components": [{"type": "body", "parameters": [{"type": "text", "text": body}]}]}})

    def plain_only(self, kind: str) -> None:
        """A pack says that this kind of request carries its own buttons: it is sent as a plain message, never with the default template (whose buttons would say something
        else), unless the settings file names a template for it."""
        self.kind_templates.setdefault(kind, "")

    def _template_for(self, record: ApprovalRecord) -> Optional[str]:
        if record.kind in self.kind_templates:
            return self.kind_templates[record.kind] or None
        return self.template_name

    def _send_template(self, record: ApprovalRecord, address: str, template: str) -> DeliveryResult:
        """Template with one body variable (a one-line summary) and up to 3 quick-reply buttons, in the order
        of the request's options (approve, snooze, dismiss). The button payloads carry the approval id."""
        components: list[dict[str, Any]] = [{"type": "body", "parameters": [{"type": "text", "text": template_summary(record.request)}]}]
        for index, option in enumerate(record.request.options[:3]):
            components.append({
                "type": "button", "sub_type": "quick_reply", "index": str(index),
                "parameters": [{"type": "payload", "payload": encode_action(record.id, option.action)}],
            })
        return self._post({
            "to": address, "type": "template",
            "template": {"name": template, "language": {"code": self.template_language}, "components": components},
        })

    # ---------------------------------------------------------------- inbound
    def verify_webhook(self, headers: Mapping[str, str], query: Mapping[str, str], body: bytes) -> bool:
        if not self.app_secret:
            return False
        sig = {k.lower(): v for k, v in headers.items()}.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(self.app_secret.encode(), body, hashlib.sha256).hexdigest()
        return safe_equals(sig, expected)

    def handle_verification(self, query: Mapping[str, str]) -> Optional[tuple[int, str]]:
        if query.get("hub.mode") == "subscribe" and self.verify_token and safe_equals(query.get("hub.verify_token", ""), self.verify_token):
            return 200, query.get("hub.challenge", "")
        return 403, "forbidden"

    def parse_webhook(self, payload: dict[str, Any]) -> list[InboundEvent]:
        events: list[InboundEvent] = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                for msg in change.get("value", {}).get("messages", []):
                    sender = str(msg.get("from", ""))
                    mid = str(msg["id"])[:200] if msg.get("id") else None
                    if msg.get("type") == "interactive":
                        inter = msg.get("interactive", {})
                        reply = inter.get("button_reply") or inter.get("list_reply") or {}
                        decoded = decode_action(reply.get("id", ""))
                        if decoded:
                            events.append(InboundEvent(channel=self.type, sender=sender, approval_id=decoded[0], action=decoded[1], external_id=mid))
                    elif msg.get("type") == "button":            # quick-reply button of a template message
                        decoded = decode_action((msg.get("button") or {}).get("payload", ""))
                        if decoded:
                            events.append(InboundEvent(channel=self.type, sender=sender, approval_id=decoded[0], action=decoded[1], external_id=mid))
                    elif msg.get("type") == "text":
                        events.append(InboundEvent(channel=self.type, sender=sender, text=msg.get("text", {}).get("body", ""), external_id=mid))
        return events
