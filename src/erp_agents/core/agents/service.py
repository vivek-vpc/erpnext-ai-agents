"""AgentService: the one object the rest of the application talks to for the AI agents.

It turns a request ("write the brief", "review this order request", "answer this question") into an agent job, runs it through the MCP tool server with the configured
model, and returns either the verified result (source "model") or the plain-rules result (source "rules"). Every task is recorded (agents/store.py). One task runs at a time:
a laptop has one model, and two agents talking over each other only make both slow.

With the agents switched off nothing is sent to any model; the brief and the order-request review still work from rules alone.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import socket
import threading
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from .. import metrics, obs
from ..common import Actor
from ..netguard import blocked_target
from ..policy import AGENT_PROVIDERS, PROVIDER_LABELS, PROVIDER_MODELS, PROVIDER_URLS, AgentSettings
from .llm import KEY_ENV, LLMError, ToolChatClient, describe, make_client
from .runtime import AgentJob, AgentRuntime, Evidence, Outcome, ToolRun
from .usage import month_start, summarize, token_summary

log = logging.getLogger(__name__)

INSTALL_HINT = 'The AI agents need an extra package: pip install "erpnext-ai-agents[agents]".'
BUSY = "The assistant is busy with another task. Please try again in a minute."


@dataclass
class Plan:
    job: AgentJob
    actor: Actor
    finish: Callable[[Outcome], dict[str, Any]]            # the result from a verified model answer
    fallback: Callable[[Outcome], dict[str, Any]]          # the result from rules alone (the model was unreachable or its answer was rejected)
    request_text: str = ""
    approval_id: Optional[str] = None
    asked_by: str = ""


def fence_prepare_tools(job: AgentJob, toolkit: Any, actor: Actor) -> AgentJob:
    """The order gate, for every pack. A tool whose effect is "prepare" (it sends an approval request to people) is the only kind of tool that does anything, so the core fences it
    whatever a pack's own gate says: (1) a job that offers one must carry a gate of its own, or it does not run; (2) it is offered only to a caller whose role the tool needs;
    (3) a call is refused unless that role is held, and at most one such call is made per task, even if the pack's gate would allow it. The pack's gate decides the rest (that the
    person asked for it, for what) and speaks first."""
    prepare = [name for name in job.tools if (toolkit.spec(name) is not None and toolkit.spec(name).effect == "prepare")]
    if not prepare:
        return job
    if job.gate is None:
        raise ValueError(f"this job offers {', '.join(prepare)}, which prepares a request for a person to approve, but it carries no gate that decides when that is allowed, so it was not run")
    offered = tuple(name for name in job.tools if name not in prepare or actor.has_role(toolkit.spec(name).role))
    inner = job.gate

    def gate(name: str, args: dict[str, Any], ev: Any) -> Optional[str]:
        refusal = inner(name, args, ev)                     # the pack's own gate speaks first (it knows what to tell the model) ...
        if refusal:
            return refusal
        spec = toolkit.spec(name)
        if spec is not None and spec.effect == "prepare":   # ... and the core is the backstop for what it lets through
            if not actor.has_role(spec.role):
                return f"{name} needs the {spec.role} level, which this person does not have"
            if any(run.name == name for run in ev.runs):
                return f"only one {name} call can be made per question"
        return None
    return replace(job, tools=offered, gate=gate)


class AgentService:
    def __init__(self, app: Any, client_factory: Optional[Callable[[Any], ToolChatClient]] = None) -> None:
        self.app = app
        self.client_factory = client_factory or (lambda cfg: make_client(cfg, self._keys()))
        self._lock = threading.Lock()
        self._providers: list[Any] = []                 # what the packs add: their tasks (brief, review, ask ...), see add_tasks()
        self._last_tasks: list[str] = []                # "agent:task" pairs whose latest run status() shows
        self.resolve: Callable[..., list] = socket.getaddrinfo          # how check() looks up a host name (tests replace it: no network needed)
        try:
            app.agent_log.fail_stale()
        except Exception:  # noqa: BLE001
            log.exception("could not tidy the record of agent tasks")

    # ---------------------------------------------------------------- what a pack adds
    def add_tasks(self, provider: Any, last: tuple[str, ...] = ()) -> None:
        """A pack's agent tasks (its brief, its reviews, its answers to questions): an object whose methods this service then offers as its own (`app.agents.brief(...)`).
        `last`: "agent:task" pairs whose latest run `status()` should show. Everything a task needs to run (the model, the tools, the checks, the record) stays here."""
        self._providers.append(provider)
        self._last_tasks.extend(k for k in last if k not in self._last_tasks)

    def __getattr__(self, name: str) -> Any:
        # only reached when the service itself has no such attribute: look in what the packs added (their CLASS attributes, so two objects can never ask each other forever)
        if not name.startswith("__"):
            for provider in self.__dict__.get("_providers", []):
                if hasattr(type(provider), name):
                    return getattr(provider, name)
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    # ---------------------------------------------------------------- state
    @property
    def settings(self) -> Any:
        return self.app.settings.agents()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled)

    @property
    def log(self) -> Any:
        return self.app.agent_log

    def today(self) -> str:
        tz = ZoneInfo(self.app.settings.system().timezone)
        return self.app.clock().astimezone(tz).date().isoformat()

    def _keys(self) -> dict[str, str]:
        """The hosted-model keys config.py found in the environment. They are never settings, and never leave this object except as a request header."""
        return self.app.config.get("llm_keys") or {}

    def describe(self) -> dict[str, Any]:
        return describe(self.settings, self._keys())

    def status(self, check_model: bool = False) -> dict[str, Any]:
        cfg = self.settings
        try:
            import mcp  # noqa: F401
            installed = True
        except ImportError:
            installed = False
        info = self.describe()
        out: dict[str, Any] = {"enabled": cfg.enabled, "model": info["model"], "model_server": info["base_url"], "provider": info["provider"], "provider_label": info["provider_label"],
                               "hosted": info["hosted"], "key_configured": info["key_configured"], "key_variable": info["key_variable"], "leaves_this_server": info["remote"],
                               "package_installed": installed, "busy": self._lock.locked(), "tools": cfg.tools_url or "built-in", "install_hint": None if installed else INSTALL_HINT}
        if check_model and cfg.enabled:
            try:
                client = self.client_factory(cfg)
                out["model_check"] = client.status() if hasattr(client, "status") else {"reachable": True, "model_installed": True}
            except LLMError as exc:                         # for a hosted provider: no key yet
                out["model_check"] = {"reachable": False, "model_installed": False, "error": str(exc)}
        keys = self._keys()
        sources = self.app.config.get("llm_key_sources") or {}
        out["providers"] = [{"id": p, "label": PROVIDER_LABELS[p], "hosted": p != "ollama", "base_url": PROVIDER_URLS[p], "model": PROVIDER_MODELS[p], "key_variable": KEY_ENV.get(p),
                             "key_configured": bool(keys.get(p)) if p != "ollama" else None, "key_source": sources.get(p), "own_variable": f"ERP_AGENTS_{p.upper()}_API_KEY" if p != "ollama" else None}
                            for p in AGENT_PROVIDERS]
        out["key_source"] = sources.get(cfg.provider)
        out["last"] = {k: self._brief_row(self.log.latest(*k.split(":"))) for k in self._last_tasks}
        return out

    def check(self, provider: str, base_url: str = "", model: str = "") -> dict[str, Any]:
        """Does this choice work? Asks the provider which models the key can use (no tokens are spent, and nothing from ERPNext is sent). Works on a choice that is not saved yet.
        A hosted provider is always checked at its own address, whatever address the caller sends, so the key cannot be pointed anywhere else; and this server is never
        made to contact its own machine or private network for a hosted provider, nor a link-local address (a cloud metadata service) for any."""
        if provider in PROVIDER_URLS and provider != "ollama":
            base_url = ""
        try:
            probe = AgentSettings(provider=provider, base_url=base_url, model=model, allow_remote=True)      # allow_remote: only the key is sent, to learn which models it can use
        except ValidationError as exc:
            raise ValueError(exc.errors()[0]["msg"].removeprefix("Value error, "))
        refuse = ("link-local", "reserved") if provider == "ollama" else ("loopback", "link-local", "private", "reserved")
        problem = blocked_target(probe.effective_base_url(), refuse, resolve=self.resolve)
        if problem:
            raise ValueError(f"the model server cannot be checked: {problem}")
        try:
            client = make_client(probe, self._keys())
        except LLMError as exc:
            return {"provider": provider, "model": probe.effective_model(), "reachable": False, "model_installed": False, "error": str(exc)}
        return {"provider": provider, "model": probe.effective_model(), **(client.status() if hasattr(client, "status") else {"reachable": True, "model_installed": True})}

    @staticmethod
    def _brief_row(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        return {k: row[k] for k in ("id", "status", "started_at", "model", "llm_seconds", "note")} if row else None

    # ---------------------------------------------------------------- running a plan
    def run(self, plan: Plan, wait: float = 1800) -> dict[str, Any]:
        return self._complete(self._start(plan), plan, wait)

    def submit(self, plan: Plan) -> str:
        """Start the plan in the background; the caller polls `task(id)`."""
        task_id = self._start(plan)
        context = contextvars.copy_context()                # a new thread starts with an empty context; the task binds its own id, and whatever else is bound (a request id) is kept
        threading.Thread(target=context.run, args=(self._complete, task_id, plan, 1800), name=f"agent-{plan.job.agent}-{plan.job.task}", daemon=True).start()
        return task_id

    def task(self, task_id: str) -> Optional[dict[str, Any]]:
        return self.log.get(task_id)

    def _start(self, plan: Plan) -> str:
        cfg, info = self.settings, self.describe()
        model, remote = info["model"], info["remote"]
        try:                                                # a client says what it really is (a test double, or a custom address); a missing key does not stop the task being recorded
            client = self.client_factory(cfg)
            model, remote = client.model, bool(client.remote)
        except LLMError:
            pass
        return self.log.start_task(plan.job.agent, plan.job.task, model, remote, plan.request_text, plan.approval_id, plan.asked_by or plan.actor.id, info["provider"])

    def _over_limit(self, cfg: Any) -> bool:
        return bool(cfg.monthly_token_limit and self.log.tokens_since(month_start(self.app.clock()), hosted_only=True) >= cfg.monthly_token_limit)

    def _complete(self, task_id: str, plan: Plan, wait: float) -> dict[str, Any]:
        """Run the task and record it. Everything logged from here on, in this thread and in the ones it starts (the tool calls, the model calls, the ERPNext requests), carries the task's id."""
        with obs.bind(task_id=task_id):
            obs.event("agent_task_start", agent=plan.job.agent, task=plan.job.task)
            return self._complete_bound(task_id, plan, wait)

    def _complete_bound(self, task_id: str, plan: Plan, wait: float) -> dict[str, Any]:
        cfg = self.settings
        outcome = Outcome()
        client: Optional[ToolChatClient] = None
        try:
            client = self.client_factory(cfg)
        except LLMError as exc:                             # a hosted provider without its key: the plain rules answer, and the note says what to add
            outcome.note = str(exc)
        hosted = bool(client is not None and getattr(client, "hosted", client.remote))
        if client is not None:
            if not self._lock.acquire(timeout=wait):
                outcome.note = "busy"
            else:
                try:
                    if client.remote and not cfg.allow_remote:
                        outcome.note = "the model server is not on this machine and 'allow_remote' is off"
                    elif hosted and self._over_limit(cfg):
                        outcome.note = f"the monthly limit of {cfg.monthly_token_limit:,} tokens for hosted models has been reached, so the plain rules answered (Settings > AI agents)"
                    else:
                        outcome = self._execute(plan, client, task_id)
                finally:
                    self._lock.release()
        result: Optional[dict[str, Any]] = None
        status, source = "ok", "model"
        if outcome.status == "ok" and outcome.data is not None:
            try:
                result = plan.finish(outcome)
            except Exception:  # noqa: BLE001 - an answer that passed the checks but cannot be put to use is treated like one that failed them
                log.exception("could not use the model's answer")
                outcome.note = "the model's answer could not be used"
        if result is None:
            try:
                result = plan.fallback(outcome)
            except Exception as exc:  # noqa: BLE001
                log.exception("the plain-rules fallback failed")
                result = {"error": f"{type(exc).__name__}"}
            status, source = ("fallback" if outcome.note != "busy" else "failed"), "rules"
            metrics.FALLBACKS.inc(agent=plan.job.agent, task=plan.job.task, reason="busy" if outcome.note == "busy" else ("rejected" if outcome.status == "rejected" else "unavailable"))
        note = outcome.note or "; ".join(outcome.problems)
        stored = {**result, "source": source}
        if outcome.prompt_tokens or outcome.output_tokens:                       # kept with the result, so whoever polls the task can show what it cost
            stored["tokens"] = token_summary(outcome.prompt_tokens, outcome.output_tokens, cfg, hosted)
        self.log.finish_task(task_id, status, stored, note, outcome.steps, outcome.tool_calls, outcome.llm_seconds, outcome.prompt_tokens, outcome.output_tokens, outcome.trace)
        obs.event("agent_task", logging.INFO if status == "ok" else logging.WARNING, agent=plan.job.agent, task=plan.job.task, status=status, source=source, steps=outcome.steps,
                  tool_calls=outcome.tool_calls, llm_seconds=round(outcome.llm_seconds, 1), tokens_in=outcome.prompt_tokens, tokens_out=outcome.output_tokens)
        out = {"task_id": task_id, "status": status, "source": source, "model": (client.model if client is not None else self.describe()["model"]) if source == "model" else None, "note": note,
               "seconds": round(outcome.llm_seconds, 1), "provider": self.describe()["provider"], "hosted": hosted, **result}
        if outcome.prompt_tokens or outcome.output_tokens:
            out["tokens"] = token_summary(outcome.prompt_tokens, outcome.output_tokens, cfg, hosted)
        return out

    def _execute(self, plan: Plan, client: ToolChatClient, task_id: str) -> Outcome:
        try:
            from ..mcp_server import build_mcp_server
            from .bridge import ToolBridge
        except ImportError:
            return Outcome(status="failed", note=INSTALL_HINT)
        cfg = self.settings
        runtime = AgentRuntime(client, cfg.max_steps, cfg.max_tool_calls)
        try:
            job = fence_prepare_tools(plan.job, self.app.toolkit, plan.actor)
        except ValueError as exc:                           # a pack's job offers a tool with an effect and no gate: it does not run
            return Outcome(status="failed", note=str(exc))

        async def go() -> Outcome:
            if cfg.tools_url:
                bridge = ToolBridge(url=cfg.tools_url, token=(self.app.config.get("mcp") or {}).get("client_token", ""))
            else:
                bridge = ToolBridge(server=build_mcp_server(self.app.toolkit, default_actor=plan.actor, via="agent", task_id=lambda: task_id))
            async with bridge:
                return await runtime.run(job, bridge)
        try:
            return asyncio.run(go())
        except Exception as exc:  # noqa: BLE001 - whatever goes wrong, the person gets the plain-rules answer and the record says why
            log.exception("agent task failed")
            return Outcome(status="failed", note=f"the agent could not run ({type(exc).__name__}: {str(exc)[:160]})")

    def usage(self, days: int = 30) -> dict[str, Any]:
        """Tokens and tasks this calendar month (against the monthly limit) and over the last `days` days, by provider and model."""
        now = self.app.clock()
        since_month = month_start(now)
        return summarize(self.log.usage(since_month), self.log.usage(now - timedelta(days=days)), self.settings, since_month, days)

    def _evidence_without_model(self, job: AgentJob, actor: Actor) -> Evidence:
        """What the warm start would have fetched, fetched directly (no model, no MCP): the base of every plain-rules result."""
        ev = Evidence(job.question)
        for name, args in job.warm:
            res = self.app.toolkit.call(name, args, actor, via="agent")
            ev.add(ToolRun(name, args, res, "error" not in res))
        return ev

    # ---------------------------------------------------------------- helpers a pack's tasks use
    def _running(self, agent: str, task: str, approval_id: Optional[str] = None) -> Optional[str]:
        for row in self.log.list(agent=agent, task=task, approval_id=approval_id, limit=3):
            if row["status"] == "running":
                return row["id"]
        return None

    def tell_approvers(self, text: str) -> int:
        """A plain message to everyone who approves orders, on every channel they have (WhatsApp delivers plain messages only while its 24-hour window is open)."""
        reached = 0
        for recipient, channels in self.app.router.targets():
            for rc in channels:
                try:
                    res = self.app.router.channels[rc.type].send_text(rc.address, text)
                    reached += int(res.ok)
                    if not res.ok:
                        log.warning("could not send the brief to %s on %s: %s", recipient.id, rc.type, res.error)
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not send the brief to %s on %s: %s", recipient.id, rc.type, exc)
        return reached
