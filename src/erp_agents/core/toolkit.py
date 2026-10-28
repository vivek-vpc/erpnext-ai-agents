"""The tool registry: the questions and actions an AI client or agent may put to the application, as plain Python.

Everything an agent is allowed to know or do goes through here, and nowhere else:

  * a PACK declares its tools in its manifest and registers them here (`Toolkit.register`); the core refuses a tool the manifest does not declare;
  * the MCP server (mcp_server.py) publishes the registered tools to any MCP client, and the agents (agents/) are one such client;
  * a tool either READS, or PREPARES an approval request for a person to answer. No tool orders, approves, pays or changes a setting;
  * every call is checked against the caller's role (the same roles as the REST API), against whether the pack is switched on, and logged: this is the one place those checks
    live, so they apply to every pack and every way in (REST, MCP, an agent);
  * results are small (an agent on a laptop model pays for every word).

No web, HTTP, scheduler or SQL library is imported here (docs/DEVELOPING.md): the layer can move into a Frappe app unchanged.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import metrics, obs
from .common import Actor, untrusted

log = logging.getLogger(__name__)

READ, PREPARE = "read", "prepare"
MAX_RESULT_CHARS = 7000                     # a tool result is cut here: it all goes into a model's context
MAX_TEXT_ARG = 300                          # the longest free-text argument a tool accepts (a field may set its own, shorter maxLength)
MAX_QUOTED = 60                             # how much of a caller's value is quoted back in an error message


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    properties: dict[str, dict[str, Any]]
    required: tuple[str, ...]
    role: str                                # viewer | approver (what ERPNext lets the caller do)
    effect: str                              # read | prepare

    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": self.properties, "required": list(self.required)}


def rounded(x: Any, nd: int = 2) -> Any:
    if x is None or isinstance(x, (str, bool)):
        return x
    try:
        v = round(float(x), nd)
    except (TypeError, ValueError):
        return x
    return int(v) if v == int(v) and abs(v) < 1e15 else v


def clip(text: Any, n: int = 200) -> str:
    """Whitespace tidied and cut at a word (with an ellipsis) when it is too long, never in the middle of one."""
    clean = " ".join(str(text or "").split())
    if len(clean) <= n:
        return clean
    return clean[: n - 1].rsplit(" ", 1)[0].rstrip(" ,;:(") + "\u2026"


def coerce_arguments(spec: ToolSpec, args: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Make the arguments fit the tool: numbers sent as text become numbers (small models do that), unknown names are dropped and reported, a wrong value is an error."""
    out: dict[str, Any] = {}
    ignored: list[str] = []
    for key, val in (args or {}).items():
        prop = spec.properties.get(key)
        if prop is None:
            if len(ignored) < 10:
                ignored.append(untrusted(key, 40))
            continue
        kind = prop.get("type")
        try:
            if kind == "integer":
                out[key] = int(float(val))
            elif kind == "number":
                v = float(str(val).replace(",", "")) if isinstance(val, str) else float(val)
                if v != v or v in (float("inf"), float("-inf")):
                    raise ValueError("not a finite number")
                out[key] = v
            elif kind == "string":
                if val is None:
                    continue
                if not isinstance(val, (str, int, float)):
                    raise TypeError("not text")
                text = untrusted(val, 100_000)                  # control and invisible characters removed; nothing cut yet
                cap = int(prop.get("maxLength") or MAX_TEXT_ARG)
                if len(text) > cap:
                    raise ValueError(f"'{key}' is too long: at most {cap} characters")
                out[key] = text
            else:
                out[key] = val
        except ValueError as exc:
            if "too long" in str(exc):
                raise
            raise ValueError(f"'{key}' must be {'a whole number' if kind == 'integer' else 'a number' if kind == 'number' else 'text'}, not {untrusted(repr(val), MAX_QUOTED)}")
        except TypeError:
            raise ValueError(f"'{key}' must be {'a whole number' if kind == 'integer' else 'a number' if kind == 'number' else 'text'}, not {untrusted(repr(val), MAX_QUOTED)}")
        if "enum" in prop and out.get(key) not in prop["enum"]:
            raise ValueError(f"'{key}' must be one of {', '.join(prop['enum'])}")
    missing = [k for k in spec.required if out.get(k) in (None, "")]
    if missing:
        raise ValueError("missing: " + ", ".join(missing))
    return out, ignored


_r, _clip = rounded, clip                     # the names the first version used


@dataclass(frozen=True)
class _Registered:
    pack: str
    spec: ToolSpec
    handler: Callable[..., dict[str, Any]]


class Toolkit:
    def __init__(self, app: Any, log_call: Optional[Callable[..., None]] = None) -> None:
        self.app = app
        self.log_call = log_call                        # log_call(actor, via, tool, args, ok, ms, task_id, error)
        self.is_enabled: Callable[[str], bool] = lambda pack: True            # the core binds this to the pack settings: a pack that is switched off answers nothing
        self._tools: dict[str, _Registered] = {}

    # ---------------------------------------------------------------- what a pack registers
    def register(self, pack: str, spec: ToolSpec, handler: Callable[..., dict[str, Any]]) -> None:
        """Add a tool. A name that is taken (by this or another pack) is an error: tool names are shared across packs."""
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} is already registered by the {self._tools[spec.name].pack} pack")
        if spec.role not in ("viewer", "approver", "admin") or spec.effect not in (READ, PREPARE):
            raise ValueError(f"tool {spec.name!r}: role or effect is not one the core understands")
        self._tools[spec.name] = _Registered(pack, spec, handler)

    def registered_for(self, pack: str) -> dict[str, tuple[str, str]]:
        """name -> (role, effect) of what this pack registered: compared with its manifest when it starts."""
        return {n: (t.spec.role, t.spec.effect) for n, t in self._tools.items() if t.pack == pack}

    # ---------------------------------------------------------------- reading the registry
    def spec(self, name: str) -> Optional[ToolSpec]:
        t = self._tools.get(name)
        return t.spec if t is not None and self.is_enabled(t.pack) else None

    def pack_of(self, name: str) -> Optional[str]:
        t = self._tools.get(name)
        return t.pack if t is not None else None

    def specs(self, actor: Optional[Actor] = None) -> list[ToolSpec]:
        """The tools of the packs that are switched on, in the order they were registered; with an actor, only those that actor's role may use."""
        return [t.spec for t in self._tools.values() if self.is_enabled(t.pack) and (actor is None or actor.has_role(t.spec.role))]

    # ---------------------------------------------------------------- the one entry point
    def call(self, name: str, arguments: Optional[dict[str, Any]], actor: Actor, via: str = "api", task_id: Optional[str] = None) -> dict[str, Any]:
        """Run one tool. Never raises: a refusal or a failure comes back as {"error": "..."} so an agent can read it and carry on.
        The checks here apply to every pack and every way in: the tool exists, its pack is switched on, the caller's role allows it, the arguments fit.
        `task_id`: the agent task this call belongs to; everything logged while the call runs (the ERPNext requests the tool makes ...) carries it."""
        with obs.bind(task_id=task_id):
            return self._call(name, arguments, actor, via, task_id)

    def _call(self, name: str, arguments: Optional[dict[str, Any]], actor: Actor, via: str, task_id: Optional[str]) -> dict[str, Any]:
        t0 = time.monotonic()
        entry = self._tools.get(name)
        args: dict[str, Any] = dict(arguments or {})
        error: Optional[str] = None
        refused: Optional[str] = None                        # why it did not run or did not work, as a word (the line in the log never quotes what the caller sent)
        result: dict[str, Any]
        if entry is None:
            available = ", ".join(t.spec.name for t in self._tools.values() if self.is_enabled(t.pack))
            result, error, refused = {"error": f"unknown tool '{untrusted(name, 40)}'. Available: {available}"}, "unknown tool", "unknown_tool"
            args = {}                                        # nothing a stranger sent is kept for an unknown tool
        elif not self.is_enabled(entry.pack):
            result, error, refused = {"error": f"not available: the {entry.pack} pack is switched off"}, "pack off", "pack_off"
            args = {}
        elif not actor.has_role(entry.spec.role):
            result, error, refused = {"error": f"not allowed: '{name}' needs the {entry.spec.role} level in ERPNext"}, "not allowed", "not_allowed"
        else:
            try:
                args, ignored = coerce_arguments(entry.spec, args)
                result = entry.handler(actor, **args)
                if ignored:
                    result["ignored_arguments"] = ignored
                if "error" in result:
                    error, refused = untrusted(result["error"], 200), "tool_error"
            except ValueError as exc:
                result, error, refused = {"error": untrusted(exc, 300)}, untrusted(exc, 200), "invalid_arguments"
                args = {}                                    # arguments that did not fit the tool are not written to the record
            except Exception as exc:  # noqa: BLE001 - a tool must never take the agent down with it
                log.exception("tool %s failed", name)
                result, error, refused = {"error": f"could not do that just now ({type(exc).__name__}: {untrusted(exc, 160)})"}, f"{type(exc).__name__}", type(exc).__name__
        if len(json.dumps(result, default=str)) > MAX_RESULT_CHARS:
            result = {"warning": "result shortened", **_shorten(result)}
        ms = int((time.monotonic() - t0) * 1000)
        shown = name if entry is not None else "unknown"
        obs.event("tool_call", logging.INFO if error is None else logging.WARNING, tool=shown, via=via, ok=error is None, ms=ms, refused=refused)
        metrics.record_tool(shown, via, error is None, refused, ms / 1000)
        if self.log_call is not None:
            try:
                self.log_call(actor.id, via, name, args, error is None, ms, task_id, error)
            except Exception:  # noqa: BLE001
                log.exception("could not log the call of %s", name)
        return result


def _shorten(result: dict[str, Any]) -> dict[str, Any]:
    """A result that is too long loses the tail of its longest list."""
    out = dict(result)
    lists = [k for k, v in out.items() if isinstance(v, list)]
    while lists and len(json.dumps(out, default=str)) > MAX_RESULT_CHARS:
        k = max(lists, key=lambda x: len(out[x]))
        if len(out[k]) <= 1:
            break
        out[k] = out[k][: max(1, len(out[k]) // 2)]
    return out
