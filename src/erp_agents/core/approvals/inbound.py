"""Shared handling of user actions arriving from any channel (webhook or polling). The code is the engine's (ai_munimji_engine.core.approvals.inbound), which a Frappe app uses too;
the service imports it from here."""
from ai_munimji_engine.core.approvals.inbound import _process, process_inbound  # noqa: F401

__all__ = ["process_inbound"]
