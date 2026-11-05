"""The owner's message templates (the vendor note on an order, the delivery reminder, the request for a price), filled WITHOUT `str.format`.

`str.format_map` follows `{vendor.__class__}`, `{vendor[0]}`, `{vendor!r}` and `{qty:>10}`, so a template saved through the settings could reach into the objects it is
filled from (finding F-10, review of 25 Oct 2026). Here the only things a template may contain are:

  * `{name}` for a name the template documents (the lists in settings.py);
  * `{{` and `}}`, which print a literal brace.

Anything else is refused when the setting is saved (`check_template`) and again when the text is filled (`fill_template`). A value is turned into text and put in as it is:
it is never scanned again, so a vendor or item name that happens to contain braces stays text.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

_TOKEN = re.compile(r"\{\{|\}\}|\{([^{}]*)\}|[{}]")


class TemplateError(ValueError):
    """The template uses something other than {name} for a name it documents."""


def _names(allowed: Iterable[str]) -> tuple[str, ...]:
    return tuple(allowed)


def _listed(names: tuple[str, ...]) -> str:
    return ", ".join("{" + n + "}" for n in names)


def check_template(template: str, allowed: Iterable[str]) -> None:
    """Raise TemplateError, in plain words, unless every placeholder is `{name}` with a name in `allowed`."""
    names = _names(allowed)
    for match in _TOKEN.finditer(template):
        whole, name = match.group(0), match.group(1)
        if whole in ("{{", "}}"):
            continue
        if name is None:
            raise TemplateError("a single { or } is not allowed (write {{ or }} to print a brace)")
        if name not in names:
            shown = name if len(name) <= 30 else name[:30] + "..."
            raise TemplateError(f"{{{shown}}} is not allowed: only {_listed(names)} can be used, written exactly like that")


def fill_template(template: str, values: Mapping[str, Any], allowed: Iterable[str] | None = None) -> str:
    """The template with each {name} replaced by the text of its value. `allowed` defaults to the names in `values`."""
    names = _names(values if allowed is None else allowed)
    check_template(template, names)

    def put(match: "re.Match[str]") -> str:
        whole, name = match.group(0), match.group(1)
        if whole == "{{":
            return "{"
        if whole == "}}":
            return "}"
        if name not in values:
            raise TemplateError(f"there is no value for {{{name}}} here")
        return str(values[name])

    return _TOKEN.sub(put, template)
