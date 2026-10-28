"""The golden cases: a question, who asks, what the world looks like, what a stand-in model says, and what a correct answer must and must not contain.

Cases are data (tests/evals/cases/*.yaml) so that a person who is not a programmer can read them and add one. A typo in a case fails loudly (unknown keys are refused).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CASES_DIR = Path(__file__).resolve().parents[2] / "tests" / "evals" / "cases"
THRESHOLDS = Path(__file__).resolve().parents[2] / "tests" / "evals" / "thresholds.json"

ENTRIES = ("chat", "agent", "explain", "why")              # chat = what the console, Telegram and WhatsApp call; agent = the AI agent's Ask; explain / why = the checked restatement of facts
OUTCOMES = ("none", "accepted", "repaired", "declined", "fallback", "unavailable")
# none        the model was not asked (the rules answered)
# accepted    the model's first reply passed the checks and is what the person read
# repaired    the first reply was rejected, the second (after being told what was wrong) passed
# declined    the model said the data does not answer the question; the person is told so
# fallback    the reply (and the repair) failed the checks; the person got the rules' answer or "not enough information"
# unavailable the model could not be reached
GROUPS = ("item_stock", "stock_overview", "vendor_compare", "document_status", "pending_approvals", "followup_tools", "ambiguous", "follow_up", "decision_history", "permissions",
          "out_of_scope", "router", "agent_good", "agent_bad", "explain")
SETUP_OPS = ("run", "item", "stock", "open_order", "receipt", "alternative", "count", "decide", "order_request", "setting")
STEP_KEYS = ("tools", "say", "answer", "raw", "error")


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    args: dict[str, Any] = Field(default_factory=dict)            # every argument written here must match; others the call carried are ignored

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, v: Any) -> Any:
        if isinstance(v, str):
            return {"name": v}
        if isinstance(v, dict) and "name" not in v and len(v) == 1:       # `- stock_status: {item_code: BOLT-M8}`
            (name, args), = v.items()
            return {"name": name, "args": args or {}}
        return v


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Optional[str] = None                # chat: the intent the question was understood as
    status: Optional[str] = None                # answered | clarify | help | error | not_enough_information | ...
    source: Optional[str] = None                # data (the rules read ERPNext) | agent | model | rules
    agent: Optional[str] = None                 # agent entry: which agent took the question
    outcome: Optional[str] = None               # see OUTCOMES; default: "none" without a script, "accepted" with one
    tools: list[ToolCall] = Field(default_factory=list)       # these tool calls were made, in this order (others may also have been)
    only_tools: bool = False                    # no tool call other than those listed
    no_tools: list[str] = Field(default_factory=list)         # none of these tools was called
    must_include: list[str] = Field(default_factory=list)     # each must appear in what the person reads (case-insensitive; "re:" starts a regular expression)
    must_not_include: list[str] = Field(default_factory=list)  # none may appear
    prepared: int = 0                           # approval requests the question may create (a person still has to press the button); anything else is a failure
    routed_by_ai: Optional[bool] = None         # the chat asked the model to place the question
    rejected_because: list[str] = Field(default_factory=list)  # a reply was rejected, and the checks' reasons contain each of these (proves the intended check fired, not another)
    error: Optional[str] = None                 # the application refused the question with a message containing this

    @field_validator("outcome")
    @classmethod
    def _outcome(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in OUTCOMES:
            raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
        return v


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    id: str
    group: str
    note: str = ""                              # why this case exists, in a sentence
    world: str = "demo"
    setup: list[dict[str, Any]] = Field(default_factory=list)
    asker: Literal["viewer", "approver"] = Field("approver", alias="as")
    entry: Literal["chat", "agent", "explain", "why"] = "chat"
    question: str
    context: Optional[dict[str, Any]] = None    # chat: what the previous answer was about ({"items": ["BOLT-M8"]})
    approval: Optional[dict[str, str]] = None   # why: which request the question is about ({"kind": "inventory.reorder", "item": "BOLT-M8"})
    ai: Optional[list[Literal["assistant", "agents"]]] = None  # which AI features are switched on for this case (default: what the entry needs; a chat case with a script gets both)
    llm: list[dict[str, Any]] = Field(default_factory=list)   # what the stand-in model says, one entry per reply it is asked for
    reply: Literal["none", "good", "bad"] = "none"            # what kind of reply the script holds (good = meant to be accepted, bad = meant to be caught)
    live: bool = True                           # False = only meaningful with the script (a real model would not say this)
    known_gap: str = ""                         # a real defect this case shows; the case is expected to fail until it is fixed
    expect: Expect

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", v):
            raise ValueError("id: lower-case letters, digits and hyphens only")
        return v

    @field_validator("group")
    @classmethod
    def _group(cls, v: str) -> str:
        if v not in GROUPS:
            raise ValueError(f"group must be one of {', '.join(GROUPS)}")
        return v

    @field_validator("setup")
    @classmethod
    def _setup(cls, ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for op in ops:
            if len(op) != 1 or next(iter(op)) not in SETUP_OPS:
                raise ValueError(f"a setup step is a single key from: {', '.join(SETUP_OPS)} (got {sorted(op)})")
        return ops

    @field_validator("llm")
    @classmethod
    def _llm(cls, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for s in steps:
            keys = [k for k in s if k in STEP_KEYS]
            if len(keys) != 1 or set(s) - set(STEP_KEYS) - {"tokens"}:
                raise ValueError(f"a model reply is exactly one of {', '.join(STEP_KEYS)} (and may add tokens: [in, out]); got {sorted(s)}")
            reply = s.get("answer")
            odd = [k for k in reply if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(k))] if isinstance(reply, dict) else []
            if odd:
                raise ValueError(f"the reply has a field called {odd[0]!r}: a comma inside a value that is not in quotes splits it in two. Put the text in quotes.")
        return steps

    @model_validator(mode="after")
    def _consistent(self) -> "Case":
        if self.entry == "why" and not self.approval:
            raise ValueError("a 'why' case names the request it is about: approval: {kind: ..., item: ...}")
        if self.reply != "none" and not self.llm:
            raise ValueError("reply: good|bad needs an llm script")
        if self.llm and self.reply == "none":
            raise ValueError("a case with an llm script says what it holds: reply: good or reply: bad")
        return self

    @property
    def ai_sections(self) -> list[str]:
        if self.ai is not None:
            return list(self.ai)
        if self.entry == "agent":
            return ["agents"]
        if self.entry in ("explain", "why"):
            return ["assistant"]
        return ["assistant", "agents"] if self.llm else []

    @property
    def wants_outcome(self) -> str:
        return self.expect.outcome or ("accepted" if self.llm else "none")


def load_cases(directory: Optional[Path] = None) -> list[Case]:
    """Every case in the folder, in file order. Raises with the file and the case id when one is wrong, or an id is used twice."""
    out: list[Case] = []
    seen: dict[str, str] = {}
    for path in sorted((directory or CASES_DIR).glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"{path.name}: not valid YAML ({exc}). A value that contains a colon followed by a space must be in quotes.") from exc
        rows = data.get("cases") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValueError(f"{path.name}: expected a list of cases under 'cases:'")
        for i, row in enumerate(rows, start=1):
            try:
                case = Case.model_validate(row)
            except Exception as exc:  # noqa: BLE001 - say which case, then re-raise the reason
                raise ValueError(f"{path.name}, case {i} ({row.get('id') if isinstance(row, dict) else '?'}): {exc}") from exc
            if case.id in seen:
                raise ValueError(f"{path.name}: the id {case.id!r} is already used in {seen[case.id]}")
            seen[case.id] = path.name
            out.append(case)
    return out
