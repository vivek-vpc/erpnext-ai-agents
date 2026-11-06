"""Turn a channel-neutral ApprovalRequest into text for each kind of channel."""
from __future__ import annotations

import html
from typing import Iterable

from .models import ApprovalRequest, Section

SEVERITY_BADGE = {"CRITICAL": "🔴 CRITICAL", "HIGH": "🟠 ACTION NEEDED", "EARLY_WARNING": "🟡 EARLY WARNING", "INFO": "ℹ️ INFO"}


def badge(severity: str) -> str:
    return SEVERITY_BADGE.get(severity, severity)


def render_plain(req: ApprovalRequest, sections: Iterable[Section] | None = None, with_title: bool = True) -> str:
    parts: list[str] = []
    if with_title:
        parts.append(f"{badge(req.severity)}\n{req.title}")
    for s in (req.sections if sections is None else sections):
        parts.append(s.heading.upper() + "\n" + "\n".join(f"- {line}" for line in s.lines))
    return "\n\n".join(parts)


def render_html(req: ApprovalRequest, sections: Iterable[Section] | None = None, with_title: bool = True) -> str:
    """Telegram-flavoured HTML (only <b>, <i>, <code> are used)."""
    parts: list[str] = []
    if with_title:
        parts.append(f"{html.escape(badge(req.severity))}\n<b>{html.escape(req.title)}</b>")
    for s in (req.sections if sections is None else sections):
        body = "\n".join(f"• {html.escape(line)}" for line in s.lines)
        parts.append(f"<b>{html.escape(s.heading)}</b>\n{body}")
    return "\n\n".join(parts)


def render_whatsapp(req: ApprovalRequest, sections: Iterable[Section] | None = None, with_title: bool = True) -> str:
    """WhatsApp markup: *bold*."""
    parts: list[str] = []
    if with_title:
        parts.append(f"{badge(req.severity)}\n*{req.title}*")
    for s in (req.sections if sections is None else sections):
        body = "\n".join(f"• {line}" for line in s.lines)
        parts.append(f"*{s.heading}*\n{body}")
    return "\n\n".join(parts)


def truncate(text: str, limit: int, suffix: str = "\n… (tap Details for the full numbers)") -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))].rstrip() + suffix
