"""Deterministic checks on the model's reply. A reply is shown only if every check passes; otherwise the person is
told there is not enough information. The checks never edit the reply."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from ..prompting import unfence
from .facts import Fact

NOT_ENOUGH = "I do not have enough information in the recorded data to answer that question."

# A number: not glued to letters/digits before it (so "SKU002" or "M8" are not numbers), commas allowed.
_NUMBER = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?")
# Words that signal a guess. Allowed only when the cited data itself uses the word.
HEDGES = ("probably", "likely", "perhaps", "maybe", "i assume", "assuming", "assumption", "i think", "i believe", "i guess",
          "presumably", "it seems", "might be", "could be", "possibly", "typically", "usually", "in general", "i estimate",
          "approximately", "roughly", "around about")
# Phrases that mean the model itself could not answer, even if it set answerable=true.
_SELF_REFUSAL = ("not enough information", "don't have enough", "do not have enough", "cannot answer", "can't answer", "no information")


@dataclass
class Verdict:
    status: str                      # answered | not_enough_information | rejected
    answer: str
    cited: list[Fact] = field(default_factory=list)
    check: str = ""


def numbers(text: str) -> list[float]:
    out = []
    for m in _NUMBER.finditer(text):
        raw = m.group().replace(",", "").rstrip(".")
        try:
            out.append(float(raw))
        except ValueError:
            continue
    return out


def _contains(value: float, pool: list[float]) -> bool:
    return any(abs(value - p) <= 1e-6 * max(1.0, abs(p)) for p in pool)


def verify(raw: str, facts: list[Fact], question: str, max_chars: int) -> Verdict:
    def reject(reason: str) -> Verdict:
        return Verdict("rejected", NOT_ENOUGH, [], reason)

    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return reject("the model's reply was not valid JSON")
    if not isinstance(data, dict) or not isinstance(data.get("answerable"), bool) \
            or not isinstance(data.get("answer"), str) or not isinstance(data.get("facts_used"), list):
        return reject("the model's reply did not have the required fields")

    if not data["answerable"]:
        return Verdict("not_enough_information", NOT_ENOUGH, [], "the model said the recorded facts do not answer the question")

    answer = " ".join(unfence(data["answer"]).split())            # (a model that copies the marks from its prompt must not show them; nothing else is changed)
    if not answer:
        return reject("the answer was empty")
    if len(answer) > max_chars:
        return reject(f"the answer was longer than {max_chars} characters")
    low = answer.lower()
    if any(p in low for p in _SELF_REFUSAL):
        return Verdict("not_enough_information", NOT_ENOUGH, [], "the model's answer says it lacks information")

    by_id = {f.id: f for f in facts}
    ids = [str(i).strip().upper() for i in data["facts_used"]]
    if not ids:
        return reject("the answer did not cite any recorded fact")
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        return reject(f"the answer cited facts that do not exist ({', '.join(unknown)})")
    cited = [by_id[i] for i in dict.fromkeys(ids)]

    all_numbers = numbers(" ".join(f.text for f in facts)) + numbers(question)
    cited_numbers = numbers(" ".join(f.text for f in cited)) + numbers(question)
    in_answer = numbers(re.sub(r"\bF\d+\b", " ", answer))
    invented = [n for n in in_answer if not _contains(n, all_numbers)]
    if invented:
        return reject("the answer contains numbers that are not in the recorded data: " + ", ".join(f"{n:g}" for n in invented))
    if in_answer and not any(_contains(n, cited_numbers) for n in in_answer):
        return reject("the facts the answer cites do not contain the numbers it uses")

    cited_text = " ".join(f.text for f in cited).lower()
    guesses = [h for h in HEDGES if re.search(rf"\b{re.escape(h)}\b", low) and h not in cited_text]
    if guesses:
        return reject("the answer guesses instead of stating recorded facts (" + ", ".join(guesses) + ")")

    return Verdict("answered", answer, cited, "passed all checks")
