"""Telegram Bot channel of the service: the engine's channel (ai_munimji_engine.core.approvals.channels.telegram, where the behaviour is described and tested) with the one
thing the engine does not have, an HTTP session. It is `requests`, and it refuses redirects: the bot token is in every address, and a server it was sent to could point it elsewhere.
"""
from __future__ import annotations

import time  # noqa: F401 - the engine's reply throttle reads time.monotonic through this module's name in the tests that fix the clock
from typing import Any, Optional

import requests

from ai_munimji_engine.core.approvals.channels.telegram import (  # noqa: F401 - re-exported: the service and its tests import them from here
    COMMANDS, MAX_TEXT, PRIVATE_CHAT, REPLY_EVERY, SECRET_OK, UPDATE_TYPES, TelegramChannel as EngineTelegramChannel,
)

from ...netguard import no_redirects


class TelegramChannel(EngineTelegramChannel):
    def __init__(self, bot_token: str, webhook_secret: Optional[str] = None, mode: str = "polling", http: Any = None,
                 api_base: str = "https://api.telegram.org", **options: Any) -> None:
        super().__init__(bot_token, webhook_secret, mode, http or no_redirects(requests.Session()), api_base, **options)

    @property
    def _offset(self) -> int:
        """Where polling has got to: a test (and a restart) sets it back to 0 and Telegram hands the unconfirmed updates out again."""
        return self.offset.get()

    @_offset.setter
    def _offset(self, value: int) -> None:
        self.offset.set(value)
