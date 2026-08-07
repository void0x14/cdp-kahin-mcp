"""locators.py — Kahin selector/locator engine.

Playwright-style locator strings resolved to plain page JavaScript. The
engine produces ONLY page-side expressions (querySelector / XPathResult /
text-role scans) — no CDP calls, no Juggler protocol additions, nothing
injectable by a page's anti-bot script beyond what Runtime.evaluate already
exposes.

Supported engines:

    css=#submit        CSS selector (default when no prefix is present)
    text=Save          first element whose normalized text/value equals "Save"
    text^=Save         substring match (whitespace-normalized)
    role=button        first element with that role (explicit role attr or
                       implicit tag role); a name filter is appended as
                       role=button "Go"
    xpath=//button[1]  XPath via document.evaluate
    nth=2              0-based index over the matches of the previous segment
                       (only valid as the LAST segment of selector_js)

Chaining with ``>>`` resolves each segment inside the previous match.
"""

from __future__ import annotations

from dataclasses import dataclass

import orjson


@dataclass(frozen=True)
class ParsedLocator:
    engine: str  # css | text | role | xpath | index
    value: str
    exact: bool = False


def _q(value: str) -> str:
    """Python string -> JS string literal (quotes/unicode safe)."""
    return orjson.dumps(value).decode()


def _js_literal(value: object) -> str:
    """Python value -> JS literal (bool/number/string)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return orjson.dumps(value).decode()


def parse_locator(selector: str) -> list[ParsedLocator]:
    """Split a locator string into its engine segments."""
    parts = [part.strip() for part in (selector or "").split(">>")]
    result: list[ParsedLocator] = []
    for part in parts:
        if not part:
            continue
        if part.startswith("css="):
            result.append(ParsedLocator("css", part[4:].strip()))
        elif part.startswith("text^="):
            result.append(ParsedLocator("text", part[6:].strip(), exact=False))
        elif part.startswith("text="):
            result.append(ParsedLocator("text", part[5:].strip(), exact=True))
        elif part.startswith("role="):
            result.append(ParsedLocator("role", part[5:].strip()))
        elif part.startswith("xpath="):
            result.append(ParsedLocator("xpath", part[6:].strip()))
        elif part.startswith("nth="):
            index = part[4:].strip()
            if not index.isdigit():
                raise ValueError(f"invalid nth index: {part!r}")
            result.append(ParsedLocator("index", index))
        else:
            result.append(ParsedLocator("css", part))
    if not result:
        raise ValueError("empty selector")
    return result


_IMPLICIT_ROLES_JS = """
function __kahinRole(el) {
  const explicit = el.getAttribute && el.getAttribute("role");
  if (explicit) return explicit;
  const tag = el.tagName ? el.tagName.toLowerCase() : "";
  if (tag === "a" && el.hasAttribute("href")) return "link";
  if (tag === "button" || tag === "summary") return "button";
  if (tag === "textarea") return "textbox";
  if (tag === "select") return "combobox";
  if (tag === "img") return "img";
  if (tag === "h1" || tag === "h2" || tag === "h3" || tag === "h4" || tag === "h5" || tag === "h6") return "heading";
  if (tag === "input") {
    const t = (el.getAttribute("type") || "text").toLowerCase();
    if (t === "checkbox") return "checkbox";
    if (t === "radio") return "radio";
    if (t === "button" || t === "submit" || t === "reset") return "button";
    return "textbox";
  }
  if (el.isContentEditable) return "textbox";
  return null;
}
function __kahinName(el) {
  const aria = el.getAttribute && el.getAttribute("aria-label");
  if (aria && aria.trim()) return aria.trim();
  const lb = el.getAttribute && el.getAttribute("aria-labelledby");
  if (lb) {
    const parts = lb.split(/\\s+/).map((id) => document.getElementById(id))
      .filter(Boolean).map((n) => (n.innerText || n.textContent || "").trim()).filter(Boolean);
    if (parts.length) return parts.join(" ");
  }
  if (el.labels && el.labels.length) {
    const text = Array.from(el.labels).map((n) => (n.innerText || n.textContent || "").trim()).filter(Boolean).join(" ");
    if (text) return text;
  }
  const t = el.getAttribute && el.getAttribute("title");
  if (t && t.trim()) return t.trim();
  const alt = el.getAttribute && el.getAttribute("alt");
  if (alt && alt.trim()) return alt.trim();
  const ph = el.getAttribute && el.getAttribute("placeholder");
  if (ph && ph.trim()) return ph.trim();
  return (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
}
"""

_TEXT_SCAN_JS = """
(() => {
  const scope = ${SCOPE};
  const nodes = scope.querySelectorAll
    ? scope.querySelectorAll("a,button,input,textarea,select,label,option,summary,[role]")
    : [];
  const wanted = ${WANTED};
  for (const el of nodes) {
    const text = (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
    const value = el.value !== undefined ? String(el.value).trim() : "";
    const match = ${MATCHER};
    if (match) return el;
  }
  return null;
})()
"""

_ROLE_SCAN_JS = """
(() => {
  const scope = ${SCOPE};
  const wanted = ${WANTED};
  const name = ${NAME};
  const nodes = scope.querySelectorAll ? scope.querySelectorAll("*") : [];
  for (const el of nodes) {
    const role = __kahinRole(el);
    if (role !== wanted) continue;
    if (name !== null && name !== "" && __kahinName(el) !== name) continue;
    return el;
  }
  return null;
})()
"""


def _first_js(loc: ParsedLocator, scope_expr: str) -> str:
    """JS expression returning the first match inside ``scope_expr`` or null."""
    if loc.engine == "css":
        return f"{scope_expr}.querySelector({_q(loc.value)})"
    if loc.engine == "xpath":
        return (
            f"(() => {{ const r = document.evaluate({_q(loc.value)}, {scope_expr}, null, "
            "XPathResult.FIRST_ORDERED_NODE_TYPE, null); return r.singleNodeValue; }})()"
        )
    if loc.engine == "text":
        matcher = "text === wanted || value === wanted" if loc.exact else "text.includes(wanted) || value.includes(wanted)"
        return _TEXT_SCAN_JS.replace("${SCOPE}", scope_expr).replace("${WANTED}", _q(loc.value)).replace("${MATCHER}", matcher)
    if loc.engine == "role":
        name = ""
        rest = loc.value
        quote = rest.find('"')
        if quote != -1 and rest.rfind('"') > quote:
            name = rest[quote + 1 : rest.rfind('"')]
            role = rest[:quote].strip()
        else:
            role = rest
        return (
            "(() => { " + _IMPLICIT_ROLES_JS.replace("\n", "\n  ")
            + " " + _ROLE_SCAN_JS.replace("\n", "\n  ")
            .replace("${SCOPE}", scope_expr)
            .replace("${WANTED}", _q(role))
            .replace("${NAME}", _q(name))
            + " })()"
        )
    raise ValueError(f"engine {loc.engine!r} cannot be resolved to a first-element query")


def _all_js(loc: ParsedLocator, scope_expr: str) -> str:
    """JS expression returning an Array of matches inside ``scope_expr``."""
    if loc.engine == "css":
        return f"Array.from({scope_expr}.querySelectorAll({_q(loc.value)}))"
    if loc.engine == "xpath":
        return (
            f"(() => {{ const r = document.evaluate({_q(loc.value)}, {scope_expr}, null, "
            "XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null); const out = []; "
            "for (let i = 0; i < r.snapshotLength; i++) out.push(r.snapshotItem(i)); return out; }})()"
        )
    raise ValueError(f"engine {loc.engine!r} has no all-matches form")


def selector_all_js(selector: str) -> str:
    """JS expression evaluating to an Array of matching elements.

    ``nth=`` is not allowed as the last segment here (it addresses a single
    element); use ``selector_js`` for that.
    """
    locs = parse_locator(selector)
    if locs[-1].engine == "index":
        raise ValueError("nth= cannot be the last segment of an all-query")
    expr = "document"
    for loc in locs[:-1]:
        expr = _first_js(loc, expr)
    return _all_js(locs[-1], expr)


def selector_js(selector: str) -> str:
    """JS expression evaluating to the first matching element (or null)."""
    locs = parse_locator(selector)
    if locs[-1].engine == "index":
        base = ">>".join(
            "=".join(filter(None, [loc.engine, loc.value])) for loc in locs[:-1]
        )
        index = int(locs[-1].value)
        all_expr = selector_all_js(base) if base else "Array.from(document.querySelectorAll('*'))"
        return f"(() => {{ const all = {all_expr}; return all[{index}] ?? null; }})()"
    expr = "document"
    for loc in locs:
        expr = _first_js(loc, expr)
    return expr
