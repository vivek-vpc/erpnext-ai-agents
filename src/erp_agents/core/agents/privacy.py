"""What the AI agents send, to whom, and for how long it is kept - in one place, so the settings screen, the command line, the API and docs/PRIVACY.md say the same thing.

The toolkit decides what can leave (toolkit.py returns no customer names and no people's names or contact details); this module only describes it. A test keeps the two in step
(tests/test_privacy.py): if a tool starts returning something personal, that test fails.
"""
from __future__ import annotations

from typing import Any, Optional

from ai_munimji_engine.core.policy import PROVIDER_LABELS, AgentSettings
from .llm import is_remote

# What an agent may send to a language model is declared by each pack in its manifest (`sends_to_model`) and passed in by the caller; nothing is listed here, so a pack that
# is switched off adds nothing to the statement.
NEVER_SENT = [
    "Customer names, addresses, phone numbers or email addresses, and what any customer paid.",
    "Names, email addresses, phone numbers, Telegram or WhatsApp ids of your staff and approvers.",
    "Bank details, payment or invoice details, tax or company registration numbers.",
    "Passwords, ERPNext API keys, session tokens or this application's access keys.",
    "Anything from ERPNext that the tools do not return: the model can only ask the tools listed in docs/AGENTS.md.",
]
KEPT = [
    "Each task: what was asked, the result, which model answered, how long it took and how many tokens it used.",
    "Each tool call: which tool, with which arguments, for whom, and whether it worked.",
    "The exact messages sent to the model and its replies (the 'trace'): forgotten after the number of days set under 'keep_traces_days'; the result and the counts stay.",
]


def privacy_summary(cfg: AgentSettings, sent: Optional[list[str]] = None) -> dict[str, Any]:
    base = cfg.effective_base_url()
    remote = is_remote(base)
    hosted = cfg.provider != "ollama" and remote
    return {
        "agents_on": cfg.enabled,
        "provider": cfg.provider,
        "provider_label": PROVIDER_LABELS[cfg.provider],
        "model": cfg.effective_model(),
        "model_server": base,
        "hosted": hosted,
        "data_leaves_this_server": bool(cfg.enabled and remote),
        "where_it_goes": (f"{PROVIDER_LABELS[cfg.provider]}, a hosted service ({base}). It bills per token, and what is sent is under its terms." if hosted
                          else f"Another machine you run: {base}" if remote else "A model running on this same machine. Nothing is sent over the internet."),
        "tools_come_from": cfg.tools_url or "this application (built in)",
        "sent_to_the_model": list(sent or []),
        "never_sent_to_the_model": NEVER_SENT,
        "kept_on_this_server": KEPT,
        "trace_kept_days": cfg.keep_traces_days,
        "your_controls": [
            "The agents are off until you switch them on (Settings > AI agents).",
            "A model on another machine is refused unless 'allow_remote' is switched on; the application checks this every time an agent runs.",
            "Every task shows which model answered and whether it was on this server.",
            "Nothing the model writes reaches a person unless the program has checked its numbers against ERPNext's own figures; a person still presses Create order.",
        ],
    }
