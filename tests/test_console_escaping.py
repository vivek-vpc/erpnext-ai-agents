"""The console builds its pages by putting text into HTML (docs/PENTEST_REPORT.md: 'Script injection'). Item names, vendor names, a person's full name and the chat all arrive from outside, so every
place that puts a value into markup must pass it through esc(). This reads the console's script and checks four things without a browser (tests/test_console_dom.py does the same with one):
the escape function escapes the five characters; the script uses none of the constructs that run text as code; every `${...}` that puts a field of the server's data into markup is wrapped in esc();
and the check itself notices a field that is not (it is shown failing on a line that omits esc()).
What it cannot see: a value first put in a variable and then into markup. The DOM test and review are the defence for that."""
import re
from pathlib import Path

import pytest

HTML = Path(__file__).resolve().parent.parent / "src" / "erp_agents" / "packs" / "stock_procurement" / "web" / "index.html"

# Fields of the server's data that hold text somebody typed (an item's name, a vendor, a note, a person's name ...): never put raw into markup.
FREE_TEXT = re.compile(r"(?:^|\.)(?:name|item_name|supplier_name|supplier|vendor|vendor_note|title|headline|note|notes|why|reason|description|text|question|answer|full_name|message|explanation|"
                       r"po_name|warehouse|group|customer|comment|summary|detail|prompt|user|actor|asked_by|requested_by|decided_by|city|country|address|email|phone)$")


def script_of(text):
    found = re.search(r"<script>(.*)</script>", text, re.S)
    assert found, "the console should have one inline script"
    return found.group(1)


def skip_string(s, i, quote):
    i += 1
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == quote:
            return i + 1
        i += 1
    return i


def read_template(s, i):
    """s[i] is a backtick. Returns (index after the template, literal parts, the expressions inside ${...})."""
    parts, exprs, cur = [], [], []
    i += 1
    while i < len(s):
        c = s[i]
        if c == "\\":
            cur.append(s[i:i + 2])
            i += 2
            continue
        if c == "`":
            parts.append("".join(cur))
            return i + 1, parts, exprs
        if c == "$" and s[i + 1:i + 2] == "{":
            parts.append("".join(cur))
            cur = []
            depth, j = 1, i + 2
            start = j
            while j < len(s) and depth:
                ch = s[j]
                if ch in "'\"":
                    j = skip_string(s, j, ch)
                    continue
                if ch == "`":
                    j, _, _ = read_template(s, j)
                    continue
                depth += (ch == "{") - (ch == "}")
                j += 1
            exprs.append(s[start:j - 1])
            i = j
            continue
        cur.append(c)
        i += 1
    return i, parts, exprs


def templates(s):
    """Every template literal of the script, nested ones too: (position, literal parts, expressions)."""
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c in "'\"":
            i = skip_string(s, i, c)
        elif c == "/" and s[i + 1:i + 2] == "/":
            i = s.find("\n", i)
            if i < 0:
                break
        elif c == "/" and s[i + 1:i + 2] == "*":
            i = s.find("*/", i) + 2
        elif c == "`":
            end, parts, exprs = read_template(s, i)
            out.append((i, parts, exprs))
            for e in exprs:
                out += [(i, p, x) for _, p, x in templates(e)]
            i = end
        else:
            i += 1
    return out


def operands(expr):
    """The parts of an expression that can be its value: split at top-level ||, &&, ??, +, and a ternary's two branches (its condition is dropped)."""
    parts, cur, depth, i = [], [], 0, 0
    while i < len(expr):
        c = expr[i]
        if c in "'\"":
            j = skip_string(expr, i, c)
            cur.append(expr[i:j])
            i = j
            continue
        if c == "`":
            j, _, _ = read_template(expr, i)
            cur.append(expr[i:j])
            i = j
            continue
        depth += (c in "([{") - (c in ")]}")
        two = expr[i:i + 2]
        if depth == 0 and two in ("||", "&&", "??"):
            parts.append("".join(cur))
            cur = []
            i += 2
            continue
        if depth == 0 and c == "?" and two != "?.":
            cur = []                                           # what came before the ? was the condition
            i += 1
            continue
        if depth == 0 and c in ":+":
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


BARE = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+$")                      # a property chain such as p.supplier_name (a lone name is a variable or a parameter)


def unescaped_free_text(js):
    """(expression, template start) for every property chain that names a free-text field and is put into markup without esc()."""
    bad = []
    for pos, parts, exprs in templates(js):
        if not any(re.search(r"<[A-Za-z/]", p) for p in parts):
            continue
        for e in exprs:
            for o in operands(e):
                while o.startswith("(") and o.endswith(")"):
                    o = o[1:-1].strip()
                if BARE.match(o) and FREE_TEXT.search(o):
                    bad.append((o, js.count("\n", 0, pos) + 1))
    return bad


JS = script_of(HTML.read_text(encoding="utf-8"))


def test_the_escape_function_escapes_the_five_characters_that_matter():
    found = re.search(r"function esc\(s\)\s*\{(.*?)\}\s*\n", JS, re.S)
    assert found, "the console should define esc(s)"
    body = found.group(1)
    for ch, entity in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"), ("'", "&#39;")):
        assert entity in body, f"esc() does not turn {ch} into {entity}"
    assert "String(s ?? \"\")" in body, "esc() should turn null into an empty string and everything else into text"


@pytest.mark.parametrize("construct", [r"document\.write\s*\(", r"\beval\s*\(", r"new\s+Function\b", r"setTimeout\s*\(\s*[\"'`]", r"setInterval\s*\(\s*[\"'`]", r"javascript:", r"\bsrcdoc\b",
                                       r"\.outerHTML\s*=", r"\bimportScripts\b", r"\.insertAdjacentHTML\s*\(\s*[\"'`](?!beforeend|afterbegin|beforebegin|afterend)"])
def test_the_script_uses_nothing_that_runs_text_as_code(construct):
    assert not re.search(construct, JS), f"the console's script contains {construct}"


def test_no_free_text_field_is_put_into_markup_without_esc():
    bad = unescaped_free_text(JS)
    assert not bad, "these values are put into HTML without esc() (script injection): " + "; ".join(f"${{{e}}} near line {n} of the script" for e, n in bad[:8])


def test_the_check_would_notice_a_field_that_is_not_escaped():
    wrong = "function f(p) { return `<td>${p.supplier_name}</td><td>${esc(p.item_name)}</td><td>${p.ok ? p.item_name : \"\"}</td><td>${p.note || \"\"}</td>`; }"
    assert [e for e, _ in unescaped_free_text(wrong)] == ["p.supplier_name", "p.item_name", "p.note"]
    right = "function f(p) { return `<td>${esc(p.supplier_name)}</td><td>${p.qty}</td><td>${p.ok ? esc(p.item_name) : \"\"}</td><td>${esc(p.note || \"\")}</td>`; }"
    assert unescaped_free_text(right) == []
    assert unescaped_free_text("const label = `${p.note}`;") == [], "a template with no markup in it is not HTML"


def test_the_scanner_reads_the_whole_console():
    found = templates(JS)
    with_markup = [t for t in found if any(re.search(r"<[A-Za-z/]", p) for p in t[1]) and t[2]]
    assert len(found) > 150 and len(with_markup) > 100, (len(found), len(with_markup))
    assert sum(1 for _, _, exprs in with_markup for e in exprs if e.strip().startswith("esc(")) > 100


def test_the_status_word_of_an_answer_is_escaped_when_it_has_no_label_of_its_own():
    """answerHtml shows the server's status word as the pill's text when it is not one it knows. The word is the program's own today; it is escaped anyway, so a new status or a hostile server cannot inject."""
    found = re.findall(r'<span class="pill \$\{cls\}">(.*?)</span>', JS)
    assert len(found) >= 2 and set(found) == {"${esc(label)}"}, found
