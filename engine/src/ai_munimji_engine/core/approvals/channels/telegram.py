"""Telegram Bot channel (free; inline buttons; no 24-hour window and no message templates to approve).

Two ways to receive button presses:
  * polling  - the application asks Telegram for updates (works behind a router with no public address)
  * webhook  - Telegram calls the application (needs a public HTTPS address; checked with a secret header)
Only people the application knows (a chat id it can tie to a person) may decide anything; everyone else is ignored.
A stranger (or a new colleague) who sends /start gets their own chat id back, so they know what to give the person who runs the application.
The bot token is a password: it never appears in an error or a log line.

The channel makes no network call of its own. It is given `http`, an object with `post(url, json=..., timeout=...)` that answers with an object having `status_code` and
`json()` (a `requests.Session` is one): the service passes a session that refuses redirects (the token is in every address), and a Frappe site passes its own. Nothing
here imports an HTTP library, so the engine stays installable on its own (tests/test_engine_imports.py).
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote

from ..models import ApprovalRecord, DeliveryResult, InboundEvent
from ..render import render_html, truncate
from .base import Channel, decode_action, encode_action, safe_equals

log = logging.getLogger(__name__)
MAX_TEXT = 3900   # Telegram limit is 4096 characters
SECRET_OK = re.compile(r"^[A-Za-z0-9_-]{1,256}$")          # what Telegram accepts as a webhook secret
UPDATE_TYPES = ["callback_query", "message"]               # button presses, and /start so people can learn their chat id
COMMANDS = ("/start", "/id", "/help", "/whoami")
REPLY_EVERY = 30.0                                          # seconds: one answer per person, so the bot cannot be made to chatter
PRIVATE_CHAT = re.compile(r"^\d{1,20}$")                    # a person's own chat id; groups and channels have a minus sign

# the words the bot uses about itself; an application with other names or other abilities passes its own
PRODUCT = "the Stock Assistant"
BOT_DESCRIPTION = "the Stock Assistant bot for a company's inventory"
KNOWN_HELP = (
    "Low-stock alerts and reorder requests arrive here with buttons: tap one to answer. Nothing is ordered until someone approves it.\n"
    "You can also write to me: ask about stock, vendors or orders (for example: how much FAS-0004 do we have?), reply to a request to ask about it, "
    "or tap Change quantity / Change vendor on an order request before you approve it."
)


class MemoryOffset:
    """Where polling has got to (the id of the next update to ask for). An application that restarts passes one that keeps it; Telegram hands out again what was not confirmed,
    and the inbound ledger recognises it."""

    def __init__(self) -> None:
        self.value = 0

    def get(self) -> int:
        return self.value

    def set(self, value: int) -> None:
        self.value = value


class MemoryThrottle:
    """One answer per person per `every` seconds, kept in this process. An application with several processes passes one that shares its memory."""

    def __init__(self, every: float = REPLY_EVERY) -> None:
        self.every = every
        self._replied: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        last = self._replied.get(key)
        if last is not None and now - last < self.every:
            return False
        self._replied = {k: t for k, t in self._replied.items() if now - t < self.every}
        self._replied[key] = now
        return True


class TelegramChannel(Channel):
    type = "telegram"
    supports_webhook = True
    needs_address = True

    def __init__(
        self,
        bot_token: str,
        webhook_secret: Optional[str] = None,
        mode: str = "polling",
        http: Any = None,
        api_base: str = "https://api.telegram.org",
        *,
        product: str = PRODUCT,
        bot_description: str = BOT_DESCRIPTION,
        known_help: str = KNOWN_HELP,
        throttle: Optional[Any] = None,
        offset: Optional[Any] = None,
        timeout_for: Optional[Callable[[str], float]] = None,
    ) -> None:
        if not bot_token:
            raise ValueError("telegram: bot_token is required")
        if str(mode).lower() not in ("polling", "webhook"):
            raise ValueError("telegram: mode must be 'polling' or 'webhook'")
        if http is None:
            raise ValueError("telegram: http is required (an object with post(url, json=, timeout=); the engine makes no network call of its own)")
        self.token = bot_token
        self.webhook_secret = webhook_secret or None
        self.mode = str(mode).lower()
        self.http = http
        self.api = f"{api_base.rstrip('/')}/bot{bot_token}"
        self.product, self.bot_description, self.known_help = product, bot_description, known_help
        self.throttle = throttle if throttle is not None else MemoryThrottle()
        self.offset = offset if offset is not None else MemoryOffset()
        self.timeout_for = timeout_for                  # method -> seconds, for an application that wants the cosmetic calls short; None = the defaults below

    # ---------------------------------------------------------------- low level
    def _redact(self, text: Any) -> str:
        """The bot token is in every request address, so network errors quote it. Never let it out."""
        out = str(text)
        for form in {self.token, quote(self.token, safe=""), quote(self.token)}:
            out = out.replace(form, "<bot-token>")
        return out

    def _call(self, method: str, payload: dict[str, Any], timeout: float = 15) -> dict[str, Any]:
        if self.timeout_for is not None:
            timeout = self.timeout_for(method) or timeout
        try:
            resp = self.http.post(f"{self.api}/{method}", json=payload, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Telegram {method}: could not reach Telegram ({self._redact(f'{type(exc).__name__}: {exc}')[:200]})") from None
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            raise RuntimeError(f"Telegram {method}: non-JSON response (HTTP {resp.status_code})") from None
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {self._redact(data.get('description', 'error'))} (code {data.get('error_code')})")
        return data

    def address_problem(self, address: Optional[str]) -> Optional[str]:
        a = (address or "").strip()
        if not a:
            return "is empty (put the person's Telegram chat id there: send /start to the bot and it replies with it)"
        if not PRIVATE_CHAT.match(a):
            return f"({a[:20]}) is not a person's own chat id: use the number the bot sends back to /start in a private chat (group and channel ids are not supported)"
        return None

    # ---------------------------------------------------------------- outbound
    def send_approval(self, record: ApprovalRecord, address: Optional[str]) -> DeliveryResult:
        if not address:
            return DeliveryResult(ok=False, error="telegram: recipient has no chat id")
        req = record.request
        rows, row = [], []
        for opt in req.options:
            row.append({"text": opt.label, "callback_data": encode_action(record.id, opt.action)})
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        try:
            data = self._call("sendMessage", {
                "chat_id": address,
                "text": truncate(render_html(req), MAX_TEXT),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": rows},
            })
            return DeliveryResult(ok=True, external_id=str(data["result"]["message_id"]))
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(ok=False, error=self._redact(exc))

    def send_text(self, address: Optional[str], text: str) -> DeliveryResult:
        if not address:
            return DeliveryResult(ok=False, error="telegram: no chat id")
        try:
            data = self._call("sendMessage", {"chat_id": address, "text": truncate(text, MAX_TEXT), "disable_web_page_preview": True})
            return DeliveryResult(ok=True, external_id=str(data["result"]["message_id"]))
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(ok=False, error=self._redact(exc))

    echoes_outcome = True               # update_outcome replies to the request message with the outcome text

    def update_outcome(self, record: ApprovalRecord, external_id: Optional[str], address: Optional[str], text: str) -> None:
        if not (external_id and address):
            return
        try:
            self._call("editMessageReplyMarkup", {"chat_id": address, "message_id": int(external_id), "reply_markup": {"inline_keyboard": []}})
            self._call("sendMessage", {"chat_id": address, "text": text, "reply_to_message_id": int(external_id)})
        except Exception as exc:  # noqa: BLE001 - cosmetic, never fail a decision over it
            log.info("telegram update_outcome skipped: %s", self._redact(exc))

    # ---------------------------------------------------------------- inbound
    def verify_webhook(self, headers: Mapping[str, str], query: Mapping[str, str], body: bytes) -> bool:
        if not self.webhook_secret:
            return False   # refuse unauthenticated webhooks
        got = {k.lower(): v for k, v in headers.items()}.get("x-telegram-bot-api-secret-token", "")
        return safe_equals(got, self.webhook_secret)

    def parse_webhook(self, payload: dict[str, Any]) -> list[InboundEvent]:
        update_id = payload.get("update_id")
        uid = str(update_id) if isinstance(update_id, int) and not isinstance(update_id, bool) else None          # the id of this update: Telegram gives the same one when it delivers it again
        cq = payload.get("callback_query")
        if cq:
            decoded = decode_action(cq.get("data", ""))
            if not decoded or not isinstance(cq.get("from"), dict):
                return []
            return [InboundEvent(
                channel=self.type, sender=str(cq["from"]["id"]), approval_id=decoded[0], action=decoded[1], ack_token=str(cq["id"]), external_id=uid,
            )]
        msg = payload.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("from"), dict) and isinstance(msg.get("chat"), dict):
            # only a person writing to the bot privately; never another bot, a group or a channel
            if msg["chat"].get("type") == "private" and not msg["from"].get("is_bot") and isinstance(msg.get("text"), str):
                replied = msg.get("reply_to_message") if isinstance(msg.get("reply_to_message"), dict) else {}
                return [InboundEvent(channel=self.type, sender=str(msg["from"]["id"]), text=msg["text"][:300],
                                     reply_to=str(replied["message_id"]) if replied.get("message_id") is not None else None, external_id=uid)]
        return []

    def acknowledge(self, event: InboundEvent, text: str) -> None:
        try:
            if event.ack_token:
                self._call("answerCallbackQuery", {"callback_query_id": event.ack_token, "text": text[:190]})
            else:
                self.send_text(event.sender, text)
        except Exception as exc:  # noqa: BLE001
            log.info("telegram acknowledge failed: %s", self._redact(exc))

    def handle_command(self, event: InboundEvent, known: bool) -> bool:
        """/start, /id, /help from anybody: tell them their own chat id and what this bot is. Anything else from a stranger is ignored quietly.
        One answer per person per half minute, and the answer never contains anything but their own id."""
        words = (event.text or "").strip().split()
        command = words[0].split("@")[0].lower() if words else ""
        if command not in COMMANDS and known:
            return False                                   # a colleague typing something else gets the usual "use the buttons" reply
        if self.throttle.allow(event.sender):
            if known:
                text = f"You are set up on {self.product}. Your Telegram chat id is {event.sender}.\n{self.known_help}"
            else:
                text = (f"This is {self.bot_description}. Your Telegram chat id is {event.sender}.\n"
                        f"Give that number to the person who runs {self.product}. Until they add it, you will not receive requests and cannot answer any.")
            self.send_text(event.sender, text)
        return True

    # ---------------------------------------------------------------- polling / setup
    def poll_once(self, timeout: int = 20) -> list[InboundEvent]:
        data = self._call("getUpdates", {"offset": self.offset.get(), "timeout": timeout, "allowed_updates": UPDATE_TYPES}, timeout=timeout + 10)
        events: list[InboundEvent] = []
        for upd in data.get("result", []):
            self.offset.set(max(self.offset.get(), upd["update_id"] + 1))
            events.extend(self.parse_webhook(upd))
        return events

    def set_webhook(self, url: str) -> dict[str, Any]:
        if not self.webhook_secret:
            raise ValueError("telegram: set webhook_secret (TELEGRAM_WEBHOOK_SECRET) before registering a webhook")
        if not SECRET_OK.match(self.webhook_secret):
            raise ValueError("telegram: the webhook secret may only contain letters, digits, '_' and '-' (1 to 256 characters)")
        if not str(url).lower().startswith("https://"):
            raise ValueError("telegram: a webhook address must start with https://")
        return self._call("setWebhook", {"url": url, "secret_token": self.webhook_secret, "allowed_updates": UPDATE_TYPES})

    def delete_webhook(self) -> dict[str, Any]:
        return self._call("deleteWebhook", {})

    # ---- helpers for the command line and the settings page
    def me(self) -> dict[str, Any]:
        return self._call("getMe", {})["result"]

    def webhook_info(self) -> dict[str, Any]:
        return self._call("getWebhookInfo", {})["result"]

    def recent_chats(self) -> list[dict[str, Any]]:
        """People who have written to the bot lately (Telegram keeps updates for 24 hours). Does not mark anything as read."""
        data = self._call("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
        seen: dict[str, dict[str, Any]] = {}
        for upd in data.get("result", []):
            msg = upd.get("message") or {}
            chat, frm = msg.get("chat") or {}, msg.get("from") or {}
            if chat.get("type") == "private" and not frm.get("is_bot"):
                name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or frm.get("username") or "?"
                seen[str(chat["id"])] = {"chat_id": str(chat["id"]), "name": name, "username": frm.get("username"), "last_text": str(msg.get("text", ""))[:40]}
        return list(seen.values())
