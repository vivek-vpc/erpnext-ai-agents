"""Build channel instances from the `channels:` section of settings.yaml."""
from __future__ import annotations

from typing import Any, Callable

from ai_munimji_engine.core.approvals.channels.base import Channel
from .simple import ConsoleChannel, InboxChannel
from .telegram import TelegramChannel
from .whatsapp import WhatsAppChannel

CHANNEL_TYPES: dict[str, Callable[..., Channel]] = {
    "telegram": TelegramChannel,
    "whatsapp": WhatsAppChannel,
    "console": ConsoleChannel,
    "inbox": InboxChannel,
}


def register_channel_type(name: str, factory: Callable[..., Channel]) -> None:
    """Plug in a new channel type (e.g. 'slack', 'email', 'mobile_push')."""
    CHANNEL_TYPES[name] = factory


def _enabled(value: Any) -> bool:
    """`enabled` may come from an ${ENV} expansion, which is always a string: '', 'false', 'no', '0' and 'off'
    are off, so `enabled: "${WHATSAPP_TOKEN}"` switches a channel on only when its secret is set."""
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "no", "0", "off")
    return bool(value)


def build_channels(config: dict[str, dict[str, Any]], http: Any = None) -> dict[str, Channel]:
    channels: dict[str, Channel] = {}
    for name, cfg in (config or {}).items():
        cfg = dict(cfg or {})
        if not _enabled(cfg.pop("enabled", True)):
            continue
        factory = CHANNEL_TYPES.get(name)
        if factory is None:
            raise ValueError(f"Unknown channel type {name!r}. Known: {sorted(CHANNEL_TYPES)}")
        if http is not None and name in ("telegram", "whatsapp"):
            cfg["http"] = http
        channels[name] = factory(**cfg)
    return channels
