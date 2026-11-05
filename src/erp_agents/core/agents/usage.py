"""Tokens used, and what they cost: the arithmetic behind "how much is the hosted model costing me?".

A hosted provider bills per token, input and output separately. The application counts the tokens each reply reports, and, if the owner entered the provider's prices, turns them into an
estimate. The prices are the owner's own entry (they change, and differ by model), so an estimate is only ever shown when they are set, and it is called an estimate.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from ai_munimji_engine.core.policy import AgentSettings


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def estimated_cost(tokens_in: int, tokens_out: int, cfg: AgentSettings) -> Optional[float]:
    """In the owner's currency; None when no price has been entered."""
    if not (cfg.price_input_per_million or cfg.price_output_per_million):
        return None
    return round((tokens_in * cfg.price_input_per_million + tokens_out * cfg.price_output_per_million) / 1_000_000, 4)


def token_summary(tokens_in: int, tokens_out: int, cfg: AgentSettings, hosted: bool) -> dict[str, Any]:
    return {"input": tokens_in, "output": tokens_out, "total": tokens_in + tokens_out, "hosted": hosted, "estimated_cost": estimated_cost(tokens_in, tokens_out, cfg) if hosted else None}


def summarize(this_month: dict[str, Any], recent: dict[str, Any], cfg: AgentSettings, since_month: datetime, days: int) -> dict[str, Any]:
    """The usage screen: this calendar month (against the limit, if one is set), and the last `days` days by model and by day. Local models cost nothing and are listed apart."""
    def totals(rows: list[dict[str, Any]], hosted: bool) -> dict[str, Any]:
        picked = [r for r in rows if bool(r["remote"]) == hosted]
        tin, tout = sum(r["tokens_in"] for r in picked), sum(r["tokens_out"] for r in picked)
        return {"tasks": sum(r["tasks"] for r in picked), **token_summary(tin, tout, cfg, hosted)}

    month_rows = this_month["by_model"]
    hosted_month = totals(month_rows, True)
    limit = cfg.monthly_token_limit
    by_model = [{"provider": r["provider"], "model": r["model"], "hosted": bool(r["remote"]), "tasks": r["tasks"], "input": r["tokens_in"], "output": r["tokens_out"],
                 "total": r["tokens_in"] + r["tokens_out"], "seconds": round(r["seconds"], 1), "estimated_cost": estimated_cost(r["tokens_in"], r["tokens_out"], cfg) if r["remote"] else None}
                for r in recent["by_model"]]
    return {"month_since": since_month.date().isoformat(), "month_hosted": hosted_month, "month_local": totals(month_rows, False),
            "monthly_token_limit": limit, "limit_used_percent": round(100 * hosted_month["total"] / limit, 1) if limit else None,
            "limit_reached": bool(limit and hosted_month["total"] >= limit), "prices_entered": estimated_cost(1, 1, cfg) is not None,
            "days": days, "by_model": by_model,
            "daily": [{"day": d["day"], "hosted": bool(d["remote"]), "input": d["tokens_in"], "output": d["tokens_out"], "tasks": d["tasks"]} for d in recent["daily"]]}
