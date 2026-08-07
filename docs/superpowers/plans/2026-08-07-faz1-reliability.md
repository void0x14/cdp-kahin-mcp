# Faz 1 — Reliability (Playwright Parity) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Kahin'e Playwright'ın reliability çekirdeğini kazandırmak — auto-wait/actionability, locator engine, retrying assertions, load-state waits ve network route pattern'leri — raw Juggler protokolünü SAF tutarak, hepsi Python katmanında.

**Architecture:** Üç yeni saf modül (`kahin/locators.py`, `kahin/actionability.py`, `kahin/expect.py`) Playwright tarzı davranışı page-JavaScript + poll ile uygular; mevcut tool'lar (`pilot_mirage.py`, `pilot.py`) bu modülleri kullanacak şekilde yükseltilir; yeni tool'lar `kahin/tools/reliability_mirage.py` içinde toplanır. Zig sidecar'a SADECE bir ek: network event sinyali (`wait_for_network_event` — Python tarafında, mevcut event reader'ı üzerinde). Juggler protokolüne hiçbir yeni komut eklenmez.

**Tech Stack:** Python ≥3.12, orjson, asyncio, pytest-asyncio, real Camoufox (e2e), mevcut FastMCP tool katmanı.

## Global Constraints

- Python ≥3.12; ruff (line-length 100, target py314) + pyright geçer; `uv run ruff check .` ve `uv run pyright` temiz bitmeli
- Tool isimleri: `kahin_mirage_*` (Mirage-only). Her yeni tool `@mcp.tool(name=..., annotations=_RO/_RW/_DW)` + `_healer_ref.safe(...)` wrapper kullanır (mevcut konvansiyon, bkz. pilot_mirage.py:436-448)
- Her tool orjson JSON-string döner, asla raise etmez; `{"error", "code"}` taksonomisi mevcut kodlarla aynı (element_not_found, element_not_actionable, invalid_argument, argument_too_large, timeout...)
- Argüman doğrulama: `_text_arg(value, tool=..., field=..., maximum=...)`, `_bounded_float`, `_strict_int` (pilot_mirage.py:59-141). Sabitler: `_MAX_SELECTOR_LENGTH=16_384`, `_MAX_INPUT_TEXT_LENGTH=16_384`, `_MAX_WAIT_TIMEOUT=120.0`, `_MAX_COORDINATE=100_000.0`
- Session pinning: çok adımlı aksiyonlarda `_capture_page_session(tool)` ile sessionId yakalanır ve her çağrıya geçirilir (pilot_mirage.py:254-271)
- Port 9222/9240 REZERVE — kullanılmaz
- Real-e2e testler `_real_available()` + `pytestmark = pytest.mark.skipif(...)` pattern'ini kullanır (test_e2e_mirage.py:48-60); `KAHIN_REQUIRE_REAL_E2E=1` gate'i korunur
- Test URL'leri: data: URL `_doc(body)` (network'süz) ve `http.server` tabanlı (network testleri) — mevcut konvansiyon (test_e2e_mirage.py:63-65, test_e2e_network.py)
- Commit mesajları Conventional Commits, İngilizce (repo geleneği)
- Yeni Juggler komutu YOK. Network interception zaten var (Network.setRequestInterception + resume/abort + fulfill passthrough)

---

### Task 1: Locator engine — `kahin/locators.py`

**Files:**
- Create: `kahin/locators.py`
- Test: `tests/test_locators.py`

**Interfaces:**
- Produces: `parse_locator(selector) -> list[ParsedLocator]`, `selector_js(selector) -> str`, `selector_all_js(selector) -> str`, `ParsedLocator` dataclass. `ParsedLocator` alanları: `engine: str` (`css|text|role|xpath|index`), `value: str`, `exact: bool = False`

- [ ] **Step 1: Write the failing test** — `tests/test_locators.py`

```python
"""Saf tests for the locator engine (no browser needed)."""

from __future__ import annotations

import pytest

from kahin.locators import parse_locator, selector_all_js, selector_js


def test_parse_bare_css() -> None:
    locs = parse_locator("#submit")
    assert [(l.engine, l.value, l.exact) for l in locs] == [("css", "#submit", False)]


def test_parse_prefixed_engines() -> None:
    locs = parse_locator("text=Save")
    assert locs[0].engine == "text" and locs[0].value == "Save" and locs[0].exact is True
    locs = parse_locator("text^=Save")
    assert locs[0].engine == "text" and locs[0].exact is False
    locs = parse_locator("role=button")
    assert locs[0].engine == "role" and locs[0].value == "button"
    locs = parse_locator("xpath=//button[1]")
    assert locs[0].engine == "xpath" and locs[0].value == "//button[1]"


def test_parse_nth_and_chain() -> None:
    locs = parse_locator("css=.list >> text=Buy >> nth=1")
    assert [l.engine for l in locs] == ["css", "text", "index"]
    assert locs[2].value == "1"


def test_selector_js_css_uses_query_selector() -> None:
    js = selector_js("#submit")
    assert "document.querySelector" in js and '"#submit"' in js


def test_selector_js_nth_wraps_all() -> None:
    js = selector_js(".item >> nth=2")
    assert "document.querySelectorAll" in js
    assert "[2]" in js


def test_selector_all_js_returns_array() -> None:
    js = selector_all_js("button")
    assert "querySelectorAll" in js


def test_selector_js_text_exact_and_substring_differ() -> None:
    exact = selector_js("text=Save")
    substring = selector_js("text^=Save")
    assert "===" in exact and "===" not in substring


def test_selector_js_xpath() -> None:
    js = selector_js("xpath=//button[1]")
    assert "document.evaluate" in js


def test_empty_and_unknown_prefix() -> None:
    with pytest.raises(ValueError):
        parse_locator("")
    locs = parse_locator("bogus=foo")
    # unknown prefixes fall back to CSS
    assert locs[0].engine == "css" and locs[0].value == "bogus=foo"


def test_selector_all_rejects_trailing_nth() -> None:
    with pytest.raises(ValueError):
        selector_all_js(".a >> nth=1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_locators.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kahin.locators'`

- [ ] **Step 3: Write the implementation** — `kahin/locators.py`

```python
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
```

Note: `_js_literal` and `_q` are both exported implicitly; `_q` is private. `_js_literal` is used by later tasks.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_locators.py -v`
Expected: PASS (12 passed)

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check kahin/locators.py tests/test_locators.py
git add kahin/locators.py tests/test_locators.py
git commit -m "feat(locators): selector engine — css/text/role/xpath/nth + chaining"
```

---

### Task 2: Actionability engine — `kahin/actionability.py`

**Files:**
- Create: `kahin/actionability.py`
- Test: `tests/test_actionability.py`

**Interfaces:**
- Consumes: `selector_js` from `kahin.locators` (Task 1)
- Produces: `ACTIONABILITY_CHECK_JS` (string), `wait_for_ready(probe, selector, *, timeout=10.0, interval=0.1, stability=2) -> dict`. `probe` is `Callable[[str], Awaitable[dict]]` (JS expression → parsed value dict). Return shape: `{"ok": True, "x": float, "y": float}` or `{"ok": False, "code": str, "reason": str|None, "selector": str, "timeout": float}`

- [ ] **Step 1: Write the failing test** — `tests/test_actionability.py`

```python
"""Actionability engine tests — pure Python, fake probe."""

from __future__ import annotations

import asyncio

import pytest

from kahin.actionability import ACTIONABILITY_CHECK_JS, wait_for_ready


class FakeProbe:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    async def __call__(self, expression: str) -> dict:
        self.calls.append(expression)
        return self.responses.pop(0) if self.responses else {"code": "element_not_found"}


@pytest.mark.asyncio
async def test_returns_immediately_when_ready() -> None:
    probe = FakeProbe([{"ok": True, "x": 10.0, "y": 20.0, "width": 30.0, "height": 10.0}])
    result = await wait_for_ready(probe, "#btn", timeout=5.0)
    assert result["ok"] is True
    assert result["x"] == 10.0 and result["y"] == 20.0
    assert len(probe.calls) == 1


@pytest.mark.asyncio
async def test_waits_for_element_to_appear() -> None:
    probe = FakeProbe([
        {"code": "element_not_found"},
        {"ok": True, "x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0},
    ])
    result = await wait_for_ready(probe, "#late", timeout=5.0, interval=0.01)
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_waits_for_stability_across_ticks() -> None:
    moving = {"ok": True, "x": 10.0, "y": 20.0, "width": 30.0, "height": 10.0}
    settled = {"ok": True, "x": 40.0, "y": 20.0, "width": 30.0, "height": 10.0}
    probe = FakeProbe([moving, moving, settled, settled])
    result = await wait_for_ready(probe, "#anim", timeout=5.0, interval=0.01, stability=2)
    assert result["ok"] is True and result["x"] == 40.0


@pytest.mark.asyncio
async def test_times_out_with_code() -> None:
    probe = FakeProbe([{"code": "element_not_found"}])
    result = await wait_for_ready(probe, "#never", timeout=0.05, interval=0.01)
    assert result["ok"] is False
    assert result["code"] in {"timeout", "element_not_found"}


@pytest.mark.asyncio
async def test_hard_failure_propagates_code() -> None:
    probe = FakeProbe([{"code": "element_not_actionable", "reason": "disabled"}])
    result = await wait_for_ready(probe, "#disabled", timeout=0.05, interval=0.01, stability=0)
    assert result["ok"] is False
    assert result["code"] == "element_not_actionable"
    assert result["reason"] == "disabled"


def test_check_js_contains_expected_guards() -> None:
    assert "elementFromPoint" in ACTIONABILITY_CHECK_JS
    assert "scrollIntoView" in ACTIONABILITY_CHECK_JS
    assert "aria-disabled" in ACTIONABILITY_CHECK_JS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_actionability.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kahin.actionability'`

- [ ] **Step 3: Write the implementation** — `kahin/actionability.py`

```python
"""actionability.py — Playwright-style actionability polling.

Checks, in order, on every tick (all inside one page evaluate):

1. attached  — the element exists in the document
2. visible   — has a bounding box and is not display:none / visibility:hidden
3. enabled   — not ``disabled`` and not ``aria-disabled="true"``
4. in-view   — scrolled into view (center)
5. hit-test  — the point under the element center is the element itself
              (elementFromPoint), i.e. not obscured by another layer
6. stable    — the element rect did not change across ``stability``
              consecutive polls (animation/animation-freeze guard)

Only the first element lookup goes through the locator engine; everything
else is a single page-evaluate probe, so a fast page can never block the
poll loop behind multiple round trips.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from kahin.locators import selector_js

_DEFAULT_TIMEOUT = 10.0
_DEFAULT_INTERVAL = 0.1
_DEFAULT_STABILITY = 2

# ${ELEMENT_JS} is replaced with the locator-derived expression. The probe
# returns a plain dict: {ok:true, x, y, width, height} or
# {code, reason?} on failure.
ACTIONABILITY_CHECK_JS = r"""
(() => {
  const el = ${ELEMENT_JS};
  if (!el || !(el instanceof Element)) return {code: "element_not_found"};
  const r = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  if (r.width <= 0 || r.height <= 0 || style.display === "none" || style.visibility === "hidden")
    return {code: "element_not_actionable", reason: "not_visible"};
  if (el.disabled === true || el.getAttribute("aria-disabled") === "true")
    return {code: "element_not_actionable", reason: "disabled"};
  el.scrollIntoView({block: "center", inline: "center"});
  const r2 = el.getBoundingClientRect();
  const x = r2.left + r2.width / 2, y = r2.top + r2.height / 2;
  const hit = document.elementFromPoint(x, y);
  if (hit && hit !== el && !el.contains(hit))
    return {code: "element_not_actionable", reason: "obscured", hit: String(hit.tagName || "").toLowerCase()};
  return {ok: true, x, y, width: r2.width, height: r2.height};
})()
"""


def _build_probe_js(selector: str) -> str:
    """Compose the full actionability probe expression for a locator string."""
    element_js = selector_js(selector)
    return ACTIONABILITY_CHECK_JS.replace("${ELEMENT_JS}", element_js)


async def wait_for_ready(
    probe: Callable[[str], Awaitable[dict[str, Any]]],
    selector: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    interval: float = _DEFAULT_INTERVAL,
    stability: int = _DEFAULT_STABILITY,
) -> dict[str, Any]:
    """Poll until the element passes actionability checks.

    ``probe`` receives the full JS expression and must return the parsed
    value dict (or ``{"error": ...}`` — treated as not-ready).

    Returns ``{"ok": True, "x", "y"}`` on success, or
    ``{"ok": False, "code", "reason", "selector", "timeout"}``.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    expression = _build_probe_js(selector)
    last: dict[str, Any] = {"code": "element_not_found"}
    stable_hits = 0
    last_point: tuple[float, float, float, float] | None = None
    while True:
        try:
            last = await probe(expression)
        except Exception:  # noqa: BLE001 - probe failures degrade to not-ready
            last = {"code": "probe_error"}
        if not isinstance(last, dict):
            last = {"code": "probe_error"}
        if last.get("ok") and isinstance(last.get("x"), (int, float)) and isinstance(last.get("y"), (int, float)):
            point = (float(last["x"]), float(last["y"]), float(last.get("width", 0.0)), float(last.get("height", 0.0)))
            stable_hits = stable_hits + 1 if point == last_point else 1
            last_point = point
            if stability <= 0 or stable_hits >= stability:
                return {"ok": True, "x": point[0], "y": point[1], "selector": selector}
        if loop.time() > deadline:
            break
        await asyncio.sleep(max(0.0, interval))
    return {
        "ok": False,
        "code": str(last.get("code") or "timeout"),
        "reason": last.get("reason"),
        "selector": selector,
        "timeout": timeout,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_actionability.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
uv run ruff check kahin/actionability.py tests/test_actionability.py
git add kahin/actionability.py tests/test_actionability.py
git commit -m "feat(actionability): Playwright-style readiness polling"
```

---

### Task 3: Retrying assertions — `kahin/expect.py`

**Files:**
- Create: `kahin/expect.py`
- Test: `tests/test_expect.py`

**Interfaces:**
- Consumes: `_q` style JS building only (no module deps); probe pattern from Task 2
- Produces: `ExpectationError`, `expect_until(probe, selector, predicate, *, timeout=5.0, interval=0.1, description="") -> dict`, and predicates `visible()`, `enabled()`, `has_text(expected)`, `has_value(expected)`, `has_attribute(name, expected=None)`, plus JS builders `text_js(selector)`, `value_js(selector)`, `attribute_js(selector, name)`

- [ ] **Step 1: Write the failing test** — `tests/test_expect.py`

```python
"""Retrying assertion engine tests — pure Python, fake probe."""

from __future__ import annotations

import asyncio

import pytest

from kahin.expect import (
    ExpectationError,
    enabled,
    expect_until,
    has_text,
    has_value,
    text_js,
    value_js,
    visible,
)


class FakeProbe:
    def __init__(self, states: list[dict]) -> None:
        self.states = list(states)

    async def __call__(self, expression: str) -> dict:
        return self.states.pop(0) if self.states else {"text": "", "value": "", "ok": False}


@pytest.mark.asyncio
async def test_expect_until_returns_first_match() -> None:
    probe = FakeProbe([{"text": "Loading", "value": "", "ok": True}])
    result = await expect_until(probe, "#status", has_text("Loading"), timeout=5.0, interval=0.01)
    assert result["text"] == "Loading"


@pytest.mark.asyncio
async def test_expect_until_retries_until_timeout() -> None:
    probe = FakeProbe([
        {"text": "Loading", "value": "", "ok": True},
        {"text": "Loading", "value": "", "ok": True},
        {"text": "Done", "value": "", "ok": True},
    ])
    result = await expect_until(probe, "#status", has_text("Done"), timeout=5.0, interval=0.01)
    assert result["text"] == "Done"


@pytest.mark.asyncio
async def test_expect_until_raises_on_timeout() -> None:
    probe = FakeProbe([{"text": "Loading", "value": "", "ok": True}])
    with pytest.raises(ExpectationError) as exc:
        await expect_until(probe, "#status", has_text("Never"), timeout=0.05, interval=0.01)
    assert "Never" in str(exc.value)
    assert exc.value.selector == "#status"


@pytest.mark.asyncio
async def test_predicates() -> None:
    assert visible()({"ok": True}) is True
    assert visible()({"ok": False, "code": "element_not_found"}) is False
    assert enabled()({"ok": True}) is True
    assert has_text("Done")({"text": "all Done now"}) is True
    assert has_text("Done")({"text": "Loading"}) is False
    assert has_value("42")({"value": "42"}) is True
    assert has_value("42")({"value": "7"}) is False


def test_js_builders() -> None:
    assert "textContent" in text_js("#x")
    assert "value" in value_js("#x")
    assert "getAttribute" in attribute_js("#x", "href")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_expect.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kahin.expect'`

- [ ] **Step 3: Write the implementation** — `kahin/expect.py`

```python
"""expect.py — retrying (web-first) assertion engine.

Playwright's ``expect`` polls until the assertion holds or the timeout
elapses. The same semantics live here as pure Python: a probe callable
(evaluate-based, provided by the tool layer) plus a predicate. On timeout
an ``ExpectationError`` carries the last observed state so the tool layer
can build an actionable ``{"code": "expectation_failed", ...}`` payload.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from kahin.locators import _q, selector_js

_DEFAULT_TIMEOUT = 5.0
_DEFAULT_INTERVAL = 0.1


class ExpectationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        selector: str,
        description: str,
        actual: dict[str, Any] | None,
        timeout: float,
    ) -> None:
        super().__init__(message)
        self.selector = selector
        self.description = description
        self.actual = actual or {}
        self.timeout = timeout


def text_js(selector: str) -> str:
    """JS expression evaluating to the element's normalized text ("" if gone)."""
    el = selector_js(selector)
    return (
        f"(() => {{ const el = {el}; if (!el) return ''; "
        'return (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim(); })()'
    )


def value_js(selector: str) -> str:
    """JS expression evaluating to the element's value ("" if gone)."""
    el = selector_js(selector)
    return (
        f"(() => {{ const el = {el}; if (!el) return ''; "
        "return el.value !== undefined && el.value !== null ? String(el.value) : ''; })()"
    )


def attribute_js(selector: str, name: str) -> str:
    """JS expression evaluating to an attribute value (null if gone)."""
    el = selector_js(selector)
    return f"(() => {{ const el = {el}; if (!el) return null; return el.getAttribute({_q(name)}); }})()"


def visible() -> Callable[[dict[str, Any]], bool]:
    return lambda state: bool(state.get("ok"))


def enabled() -> Callable[[dict[str, Any]], bool]:
    return lambda state: bool(state.get("ok"))


def has_text(expected: str) -> Callable[[dict[str, Any]], bool]:
    def predicate(state: dict[str, Any]) -> bool:
        return expected in str(state.get("text") or "")
    return predicate


def has_value(expected: str) -> Callable[[dict[str, Any]], bool]:
    def predicate(state: dict[str, Any]) -> bool:
        return str(state.get("value") or "") == expected
    return predicate


def has_attribute(name: str, expected: str | None = None) -> Callable[[dict[str, Any]], bool]:
    def predicate(state: dict[str, Any]) -> bool:
        value = state.get("attributes", {})
        if not isinstance(value, dict):
            return False
        actual = value.get(name)
        if expected is None:
            return actual is not None
        return actual == expected
    return predicate


async def expect_until(
    probe: Callable[[str], Awaitable[dict[str, Any]]],
    selector: str,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    interval: float = _DEFAULT_INTERVAL,
    description: str = "",
) -> dict[str, Any]:
    """Poll ``probe`` until ``predicate`` holds; raise ``ExpectationError`` on timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    last_state: dict[str, Any] = {}
    while True:
        try:
            state = await probe(selector)
        except Exception:  # noqa: BLE001 - probe failures are a state, not a raise
            state = {"error": "probe_failed"}
        if isinstance(state, dict):
            last_state = state
        if predicate(last_state):
            return last_state
        if loop.time() > deadline:
            break
        await asyncio.sleep(max(0.0, interval))
    raise ExpectationError(
        f"expectation '{description or predicate.__name__}' not met within {timeout:.1f}s",
        selector=selector,
        description=description or predicate.__name__,
        actual=last_state,
        timeout=timeout,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_expect.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
uv run ruff check kahin/expect.py tests/test_expect.py
git add kahin/expect.py tests/test_expect.py
git commit -m "feat(expect): retrying web-first assertion engine"
```

---

### Task 4: `kahin_mirage_wait_selector` upgrade + locator engine adoption (query/query_all/type)

**Files:**
- Modify: `kahin/tools/pilot_mirage.py` (wait_selector 743-797, query 373-404, query_all 404-433, type 467-560)
- Test: `tests/test_e2e_reliability.py` (create; runs with real Camoufox)

**Interfaces:**
- Consumes: `selector_js`, `selector_all_js` (Task 1); `wait_for_ready` (Task 2)
- Produces: `mirage_wait_selector(selector, timeout=10, state="visible", frame_id=None)` — state ∈ `attached|visible|enabled`; timeout result keeps the legacy shape `{"found": false, "timeout": N}` plus new `"code": "timeout"` for agent consumption. `mirage_query` / `mirage_query_all` / `mirage_type` accept locator strings transparently.

- [ ] **Step 1: Write the failing test** — `tests/test_e2e_reliability.py` (file header + first scenario)

```python
"""Tool-layer reliability e2e over real Camoufox (Faz 1)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import quote

import pytest
from pytest_asyncio import fixture as async_fixture

from kahin import _state as state
from kahin.the_twins import mirage as mirage_mod
from kahin.tools import pilot, pilot_mirage, trainman_mirage


def _real_available() -> bool:
    try:
        mirage_mod._sidecar_bin()
        mirage_mod._camoufox_bin()
        return True
    except RuntimeError:
        return False


pytestmark = pytest.mark.skipif(
    not _real_available(), reason="sidecar binary or Camoufox missing"
)


def _doc(body: str) -> str:
    return "data:text/html," + quote(body)


def _loads(text: str) -> Any:
    return json.loads(text)


@async_fixture
async def mirage_tools() -> AsyncGenerator[None, None]:
    resp = _loads(await pilot.browser_start(engine="mirage"))
    assert resp["status"] == "started", resp
    try:
        tab = _loads(await trainman_mirage.mirage_tab_new())
        assert tab.get("targetId"), tab
        await asyncio.sleep(0.5)
        yield
    finally:
        await pilot.browser_stop()


async def _navigate(url: str) -> None:
    resp = _loads(await pilot.navigate(url=url))
    assert isinstance(resp, dict) and resp.get("frameId"), resp


@pytest.mark.asyncio
async def test_wait_selector_text_engine_and_state(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><button style='display:none' id='b1'>Hidden</button>"
        "<button id='b2'>Visible</button></body></html>"
    ))
    hidden = _loads(await pilot_mirage.mirage_wait_selector(
        "text=Hidden", timeout=1.0, state="visible",
    ))
    assert hidden.get("found") is False
    visible = _loads(await pilot_mirage.mirage_wait_selector(
        "text=Visible", timeout=3.0, state="visible",
    ))
    assert visible.get("found") is True


@pytest.mark.asyncio
async def test_wait_selector_waits_for_appearance(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><div id='late'></div>"
        "<script>setTimeout(() => { const b = document.createElement('button'); "
        "b.id = 'later'; b.textContent = 'Later'; document.body.appendChild(b); }, 600);</script>"
        "</body></html>"
    ))
    started = time.monotonic()
    result = _loads(await pilot_mirage.mirage_wait_selector("#later", timeout=5.0))
    elapsed = time.monotonic() - started
    assert result.get("found") is True
    assert elapsed >= 0.4, f"did not actually wait: {elapsed:.2f}s"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_e2e_reliability.py -v`
Expected: FAIL — `kahin_mirage_wait_selector` doesn't accept `state` yet (TypeError) or text= never matches (element_not_found)

- [ ] **Step 3: Modify `wait_selector` in `pilot_mirage.py`**

Replace the body of `mirage_wait_selector` (currently at pilot_mirage.py:743-797) with:

```python
_WAIT_STATES = ("attached", "visible", "enabled")
_WAIT_STATE_JS = {
    # Each returns {ok:true} or {code, reason?}. ${ELEMENT_JS} is replaced.
    "attached": "(() => { const el = ${ELEMENT_JS}; return el ? {ok: true} : {code: 'element_not_found'}; })()",
    "visible": """(() => {
        const el = ${ELEMENT_JS};
        if (!el) return {code: "element_not_found"};
        const r = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        if (r.width <= 0 || r.height <= 0 || style.display === "none" || style.visibility === "hidden")
            return {code: "element_not_actionable", reason: "not_visible"};
        return {ok: true};
    })()""",
    "enabled": """(() => {
        const el = ${ELEMENT_JS};
        if (!el) return {code: "element_not_found"};
        if (el.disabled === true || el.getAttribute("aria-disabled") === "true")
            return {code: "element_not_actionable", reason: "disabled"};
        return {ok: true};
    })()""",
}


async def _poll_state(
    tool: str,
    selector: str,
    *,
    timeout: float,
    state_name: str,
    frame_id: str | None,
    session_id: str | None,
    interval: float = 0.1,
) -> dict[str, Any]:
    """Poll the chosen state check until ok or deadline (never raises)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    from kahin.locators import selector_js

    template = _WAIT_STATE_JS[state_name]
    expression = template.replace("${ELEMENT_JS}", selector_js(selector))
    last: dict[str, Any] = {"code": "element_not_found"}
    while True:
        result = await _safe_mirage_eval_result(tool, expression, frame_id, session_id=session_id)
        if isinstance(result, dict):
            value = result.get("result") or {}
            probe = value.get("value") if isinstance(value, dict) else None
            if isinstance(probe, dict):
                last = probe
                if probe.get("ok"):
                    return {"found": True, "state": state_name, "selector": selector}
        if loop.time() > deadline:
            break
        await asyncio.sleep(interval)
    return {"found": False, "timeout": timeout, "state": state_name, "selector": selector, "code": "timeout", "reason": last.get("reason")}
```

Then replace the `@mcp.tool` function:

```python
@mcp.tool(name="kahin_mirage_wait_selector", annotations=_RO)
async def mirage_wait_selector(
    selector: str, timeout: float = 10.0, state: str = "visible", frame_id: str | None = None,
) -> str:
    """Mirage: wait until the selector matches (locator engines: css=,
    text=, role=, xpath=, nth=, >> chaining). state: attached|visible|enabled.
    Returns {found:true} or {found:false, code:"timeout", reason}."""
    selector_value, error = _text_arg(
        selector, tool="kahin_mirage_wait_selector", field="selector", maximum=_MAX_SELECTOR_LENGTH,
    )
    if error:
        return error
    if not selector_value:
        return _json_error("kahin_mirage_wait_selector", "selector must not be empty", "invalid_argument", field="selector")
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=10.0)
    if state not in _WAIT_STATES:
        return _json_error(
            "kahin_mirage_wait_selector",
            f"state must be one of {_WAIT_STATES}",
            "invalid_argument",
            field="state",
            received=state,
        )
    async with _healer_ref.safe(
        "kahin_mirage_wait_selector", selector=selector_value[:80], timeout=timeout_value, state=state, frame_id=frame_id,
    ):
        session_id, capture_error = await _capture_page_session("kahin_mirage_wait_selector")
        if capture_error:
            return capture_error
        assert session_id is not None
        result = await _poll_state(
            "kahin_mirage_wait_selector", selector_value,
            timeout=timeout_value, state_name=state, frame_id=frame_id, session_id=session_id,
        )
        return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()
```

Note: the OLD behavior polled presence with 0.25s interval and returned `{"found": false, "timeout": N}`. The new default `state="visible"` is intentional (docstring says "görünene kadar"); presence is `state="attached"`. CHANGELOG entry required in Step 5.

- [ ] **Step 4: Adopt locator engine in query/query_all/type** — `pilot_mirage.py`

In `mirage_query` (line ~373) the evaluate expression uses `document.querySelector(${_q(selector_value)})`. Replace that literal with `selector_js(selector_value)`. Same for `mirage_query_all` (`document.querySelectorAll(...)` → `selector_all_js(...)`) and the two `document.querySelector(${_q(selector_value)})` occurrences inside `mirage_type` (editable check + value capture). Add the import at the top of the file:

```python
from kahin.locators import selector_all_js, selector_js
```

Exact replacement target (query, around pilot_mirage.py:379-404): find the expression string containing `document.querySelector(${_q(selector_value)})` inside `mirage_query` and replace only that interpolation with `selector_js(selector_value)`. Do the same for the `document.querySelectorAll` interpolation in `mirage_query_all`. For `mirage_type` (lines 491-504), replace both `document.querySelector(${_q(selector_value)})` occurrences with `selector_js(selector_value)`.

- [ ] **Step 5: Run e2e + unit suites**

```bash
uv run pytest tests/test_e2e_reliability.py -v
uv run pytest tests/test_locators.py tests/test_actionability.py -q
```

Expected: new tests PASS; existing unit tests still PASS.

- [ ] **Step 6: Lint + CHANGELOG + commit**

```bash
uv run ruff check kahin/tools/pilot_mirage.py tests/test_e2e_reliability.py
# CHANGELOG.md: add under "Unreleased": "feat(mirage): wait_selector state= attached|visible|enabled (default visible) + locator engines on query/query_all/type/wait_selector"
git add kahin/tools/pilot_mirage.py tests/test_e2e_reliability.py CHANGELOG.md
git commit -m "feat(mirage): wait_selector states + locator engines on DOM tools"
```

---

### Task 5: Actionability integration — click/hover/focus/dblclick wait + retry

**Files:**
- Modify: `kahin/tools/pilot_mirage.py` (`mirage_click` 436-464, `mirage_hover` 673-700, `mirage_focus` 650-672)
- Test: `tests/test_e2e_reliability.py` (extend)

**Interfaces:**
- Consumes: `wait_for_ready` (Task 2), `selector_js` (Task 1)
- Produces: `mirage_click(selector, timeout=10.0, frame_id=None)` — waits for actionability before dispatching; `mirage_hover(selector, timeout=10.0, ...)` same; `mirage_focus(selector, timeout=10.0, ...)` waits for `attached` (a hidden element can still receive focus) then focuses. New shared helper `_action_ready(tool, selector, timeout, frame_id, session_id, require_stable=True)` in pilot_mirage.py returning `(x, y)` or an error JSON string.

- [ ] **Step 1: Write the failing test** — extend `tests/test_e2e_reliability.py`

```python
@pytest.mark.asyncio
async def test_click_waits_for_animation_stability(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><head><style>"
        "#btn { position: absolute; left: 0; top: 0; width: 120px; height: 40px; "
        "transition: left 0.8s ease; }"
        "</style></head><body>"
        "<button id='btn' style='left: 0'>Move</button>"
        "<div id='log'></div>"
        "<script>"
        "const btn = document.getElementById('btn');"
        "setTimeout(() => { btn.style.left = '200px'; }, 50);"
        "btn.addEventListener('click', () => { document.getElementById('log').textContent = 'clicked'; });"
        "</script></body></html>"
    ))
    result = _loads(await pilot_mirage.mirage_click("#btn", timeout=5.0))
    assert not result.get("error"), result
    assert result.get("clicked") == "#btn"
    text = _loads(await pilot_mirage.mirage_get_text("#log"))
    assert "clicked" in text.get("text", "")


@pytest.mark.asyncio
async def test_click_waits_for_enabled(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><button id='b' disabled>Go</button>"
        "<script>setTimeout(() => { document.getElementById('b').disabled = false; }, 500);"
        "</script></body></html>"
    ))
    result = _loads(await pilot_mirage.mirage_click("text=Go", timeout=5.0))
    assert not result.get("error"), result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_e2e_reliability.py::test_click_waits_for_animation_stability tests/test_e2e_reliability.py::test_click_waits_for_enabled -v`
Expected: FAIL — the animated click lands during the transition or the disabled button click errors `element_not_actionable` (reason disabled) immediately.

- [ ] **Step 3: Add `_action_ready` helper and rewire click/hover/focus**

Insert after `_element_point` (pilot_mirage.py:331):

```python
async def _action_ready(
    tool: str,
    selector: str,
    *,
    timeout: float,
    frame_id: str | None,
    session_id: str | None,
    state: str = "visible",
) -> tuple[float, float] | str:
    """Actionability wait before input dispatch. Returns (x, y) or error str."""
    from kahin.actionability import wait_for_ready

    async def probe(expression: str) -> dict[str, Any]:
        result = await _safe_mirage_eval_result(tool, expression, frame_id, session_id=session_id)
        if isinstance(result, str):
            return {"error": "probe_failed"}
        value = result.get("result") or {}
        probe_value = value.get("value") if isinstance(value, dict) else None
        return probe_value if isinstance(probe_value, dict) else {"error": "probe_failed"}

    if state == "attached":
        # focus-like wait: just presence
        ready = await wait_for_ready(probe, selector, timeout=timeout, stability=1)
        if not ready.get("ok"):
            return _json_error(
                tool, f"element did not become {state}: {ready.get('reason') or ready.get('code')}",
                str(ready.get("code") or "timeout"), selector=selector, timeout=timeout,
            )
        # attached probe returns no coordinates — resolve them via element_point
        point = await _element_point(selector, frame_id, session_id=session_id)
        if not isinstance(point, tuple):
            return point
        return point
    ready = await wait_for_ready(probe, selector, timeout=timeout)
    if not ready.get("ok"):
        return _json_error(
            tool, f"element is not actionable: {ready.get('reason') or ready.get('code')}",
            str(ready.get("code") or "timeout"), selector=selector, timeout=timeout,
        )
    return float(ready["x"]), float(ready["y"])
```

Then rewrite `mirage_click`:

```python
@mcp.tool(name="kahin_mirage_click", annotations=_DW)
async def mirage_click(selector: str, timeout: float = 10.0, frame_id: str | None = None) -> str:
    """Mirage: click an element by selector (css=/text=/role=/xpath=, >> chain).
    Waits for actionability (visible, enabled, stable, unobscured) up to
    ``timeout`` seconds, then dispatches real mousedown+mouseup."""
    selector_value, error = _text_arg(
        selector, tool="kahin_mirage_click", field="selector", maximum=_MAX_SELECTOR_LENGTH,
    )
    if error:
        return error
    if not selector_value:
        return _json_error("kahin_mirage_click", "selector must not be empty", "invalid_argument", field="selector")
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=10.0)
    async with _healer_ref.safe("kahin_mirage_click", selector=selector_value[:80], timeout=timeout_value, frame_id=frame_id):
        session_id, capture_error = await _capture_page_session("kahin_mirage_click")
        if capture_error:
            return capture_error
        assert session_id is not None
        ready = await _action_ready(
            "kahin_mirage_click", selector_value,
            timeout=timeout_value, frame_id=frame_id, session_id=session_id,
        )
        if isinstance(ready, str):
            return ready
        x, y = ready
        down = await _dispatch_mouse("mousedown", x, y, button=0, buttons=1, session_id=session_id)
        if _is_error_response(down):
            return down
        up = await _dispatch_mouse("mouseup", x, y, button=0, buttons=0, session_id=session_id)
        if _is_error_response(up):
            return up
        return orjson.dumps({"clicked": selector_value, "x": x, "y": y, "waited": True}, option=orjson.OPT_INDENT_2).decode()
```

Rewrite `mirage_hover` similarly: same `_action_ready` flow, then `_dispatch_mouse("mousemove", x, y, session_id=session_id)`. Rewrite `mirage_focus`: use `_action_ready(..., state="attached")`, then evaluate `el.focus()` (reuse the existing focus expression but swap `document.querySelector(${_q(...)})` for `selector_js(...)`).

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_e2e_reliability.py -v
```

Expected: both new tests PASS. (If the stability test flakes: the transition is 0.8s; stability=2 with 0.1s interval converges within ~1s, timeout 5s is ample.)

- [ ] **Step 5: Commit**

```bash
uv run ruff check kahin/tools/pilot_mirage.py
git add kahin/tools/pilot_mirage.py tests/test_e2e_reliability.py
git commit -m "feat(mirage): actionability wait on click/hover/focus"
```

---

### Task 6: Navigation upgrade — `wait_until`, `timeout`, `referer`

**Files:**
- Modify: `kahin/tools/pilot.py` (`navigate` at 309-320)
- Test: `tests/test_e2e_reliability.py` (extend)

**Interfaces:**
- Produces: `kahin_navigate(url, wait_until="load", timeout=30.0, referer=None)` — `wait_until ∈ commit|domcontentloaded|load|networkidle`. Returns navigation result + `"wait_until"` + `"timeout"`. On wait timeout: `{"error": ..., "code": "navigation_timeout", "url", "wait_until", "timeout"}`.
- Consumes: `state._network_requests` (deque of network event dicts, `kahin/_state.py`) for `networkidle`.

- [ ] **Step 1: Write the failing test** — extend `tests/test_e2e_reliability.py`

```python
from kahin.tools import pilot


@pytest.mark.asyncio
async def test_navigate_wait_until_domcontentloaded(mirage_tools: None) -> None:
    resp = _loads(await pilot.navigate(
        url=_doc("<html><body><p id='x'>hi</p></body></html>"), wait_until="domcontentloaded",
    ))
    assert resp.get("frameId"), resp
    assert resp.get("wait_until") == "domcontentloaded"


@pytest.mark.asyncio
async def test_navigate_wait_until_timeout(mirage_tools: None) -> None:
    # data: URLs never finish networkidle (no network); with a tiny timeout
    # the tool must report navigation_timeout instead of hanging.
    resp = _loads(await pilot.navigate(
        url=_doc("<html><body>x</body></html>"), wait_until="networkidle", timeout=0.5,
    ))
    assert resp.get("code") == "navigation_timeout", resp
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_e2e_reliability.py::test_navigate_wait_until_domcontentloaded tests/test_e2e_reliability.py::test_navigate_wait_until_timeout -v`
Expected: FAIL — `TypeError: navigate() got an unexpected keyword argument 'wait_until'`

- [ ] **Step 3: Implement** — `pilot.py` navigate

Replace the navigate body (pilot.py:309-320) with:

```python
_NAVIGATE_WAIT_UNTIL = ("commit", "domcontentloaded", "load", "networkidle")
_NAVIGATE_IDLE_QUIET = 0.5  # seconds without a new network event to call it idle
_NAVIGATE_MAX_TIMEOUT = 120.0


async def _navigation_wait(
    tool: str,
    *,
    wait_until: str,
    timeout: float,
    frame_id: str | None,
    session_id: str | None,
) -> dict[str, Any] | None:
    """Wait for the requested load state; return an error dict or None."""
    from kahin.expect import expect_until

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    last_ts = 0.0
    while True:
        now = loop.time()
        if wait_until == "commit":
            return None  # Page.navigate reply already implies commit
        expression = (
            "(() => { const rs = document.readyState; "
            "const evts = performance.getEntriesByType('resource').length; "
            "return {readyState: rs, resources: evts}; })()"
        )
        result = await _safe_mirage_eval_result(
            tool, expression, frame_id, session_id=session_id,
        )
        state: dict[str, Any] = {}
        if isinstance(result, dict):
            value = result.get("result") or {}
            parsed = value.get("value") if isinstance(value, dict) else None
            if isinstance(parsed, dict):
                state = parsed
        ready = str(state.get("readyState") or "")
        if wait_until == "domcontentloaded" and ready in ("interactive", "complete"):
            return None
        if wait_until == "load" and ready == "complete":
            return None
        if wait_until == "networkidle":
            from kahin import _state as kahin_state
            events = kahin_state._network_requests or []
            if events:
                last_event = events[-1]
                last_ts = float(last_event.get("timestamp") or last_ts)
            if ready == "complete" and last_ts and (now - last_ts) >= _NAVIGATE_IDLE_QUIET:
                return None
        if loop.time() > deadline:
            return {
                "error": f"navigation did not reach {wait_until} within {timeout:g}s",
                "code": "navigation_timeout",
                "wait_until": wait_until,
                "timeout": timeout,
                "readyState": ready,
            }
        await asyncio.sleep(0.1)


@mcp.tool(name="kahin_navigate", annotations=_RW)
async def navigate(url: str, wait_until: str = "load", timeout: float = 30.0, referer: str | None = None) -> str:
    """Navigate the current page to a URL. wait_until: commit|domcontentloaded|
    load|networkidle. referer is best-effort (Juggler may ignore it)."""
    url_value, error = _text_arg(url, tool="kahin_navigate", field="url", maximum=8_192)
    if error:
        return error
    if not url_value:
        return _json_error("kahin_navigate", "url must not be empty", "invalid_argument", field="url")
    if wait_until not in _NAVIGATE_WAIT_UNTIL:
        return _json_error(
            "kahin_navigate",
            f"wait_until must be one of {_NAVIGATE_WAIT_UNTIL}",
            "invalid_argument",
            field="wait_until",
            received=wait_until,
        )
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_NAVIGATE_MAX_TIMEOUT, default=30.0)
    async with _healer_ref.safe("kahin_navigate", url=url_value[:120], wait_until=wait_until, timeout=timeout_value):
        err = await _require_engine()
        if err:
            return err
        params: dict[str, Any] = {"url": url_value}
        if referer:
            referer_value, referer_error = _text_arg(referer, tool="kahin_navigate", field="referer", maximum=8_192)
            if referer_error:
                return referer_error
            params["referer"] = referer_value
        engine = state._current_engine
        frame_id: str | None = None
        session_id: str | None = None
        if isinstance(engine, Mirage):
            page = await engine.ensure_page()
            session_id = page.get("sessionId") if isinstance(page, dict) else None
            frame_id = page.get("frameId") if isinstance(page, dict) else None
            result = await engine.call("Page.navigate", params, session_id=session_id)
        else:
            result = await engine.send_cdp("Page", "navigate", params)
        wait_error = await _navigation_wait(
            "kahin_navigate",
            wait_until=wait_until,
            timeout=timeout_value,
            frame_id=frame_id,
            session_id=session_id,
        )
        if wait_error:
            return orjson.dumps(wait_error, option=orjson.OPT_INDENT_2).decode()
        result["wait_until"] = wait_until
        result["timeout"] = timeout_value
        # keep auto-learn (existing behavior)
        try:
            from kahin import _fate  # noqa: PLC0415
        except ImportError:
            pass
        else:
            _fate.learn("Page", "navigate", context="navigate:" + (url_value[:64] or ""))
        return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()
```

Notes:
- The exact auto-learn call in the current navigate is preserved — read the existing body first (pilot.py:309-320) and keep its learning/validation flow; the diff only adds params, the wait, and the result fields.
- `referer`: Juggler's `Page.navigate` may reject it with -32601. If the e2e verify step shows that, keep the param, wrap the call so the error becomes `{"error": ..., "code": "unsupported_on_engine", "hint": "Juggler does not accept referer"}` — do NOT remove the parameter.
- `_safe_mirage_eval_result`, `_safe_mirage_call`, `Mirage`, `_require_engine`, `_json_error`, `_text_arg`, `_bounded_float`, `_healer_ref`, `mcp`, `_RW` must be imported in pilot.py — check existing imports; add `from kahin.the_twins.mirage import Mirage` if absent.

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_e2e_reliability.py -v
uv run pytest tests/test_oracle.py tests/test_e2e_mirage.py -q
```

Expected: new tests PASS; existing navigate-dependent tests still PASS (they pass no new args, defaults apply).

- [ ] **Step 5: Commit**

```bash
uv run ruff check kahin/tools/pilot.py
git add kahin/tools/pilot.py tests/test_e2e_reliability.py
git commit -m "feat(navigate): wait_until load states + timeout + referer"
```

---

### Task 7: `kahin_mirage_expect` tool

**Files:**
- Create: `kahin/tools/reliability_mirage.py`
- Modify: `kahin/tools/__init__.py` (register import)
- Test: `tests/test_e2e_reliability.py` (extend)

**Interfaces:**
- Consumes: `expect_until`, `text_js`, `value_js`, `attribute_js`, predicates (Task 3); `_capture_page_session`, `_safe_mirage_eval_result`, `_text_arg`, `_bounded_float`, `_json_error` (pilot_mirage)
- Produces: `kahin_mirage_expect(selector, state=None, text=None, value=None, attribute=None, attribute_value=None, timeout=5.0, frame_id=None)` — returns `{"matched": true, "state": {...}}` or `{"error", "code": "expectation_failed", "expected", "actual", "timeout"}`

- [ ] **Step 1: Write the failing test** — extend `tests/test_e2e_reliability.py`

```python
from kahin.tools import reliability_mirage


@pytest.mark.asyncio
async def test_expect_retries_text(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><div id='s'>Loading</div>"
        "<script>setTimeout(() => { document.getElementById('s').textContent = 'Ready now'; }, 500);"
        "</script></body></html>"
    ))
    result = _loads(await reliability_mirage.mirage_expect("#s", text="Ready now", timeout=5.0))
    assert result.get("matched") is True, result
    assert result.get("actual", {}).get("text") == "Ready now"


@pytest.mark.asyncio
async def test_expect_timeout_reports_actual(mirage_tools: None) -> None:
    await _navigate(_doc("<html><body><div id='s'>Loading</div></body></html>"))
    result = _loads(await reliability_mirage.mirage_expect("#s", text="Never", timeout=0.5))
    assert result.get("code") == "expectation_failed", result
    assert "Loading" in str(result.get("actual", {}))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_e2e_reliability.py::test_expect_retries_text tests/test_e2e_reliability.py::test_expect_timeout_reports_actual -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kahin.tools.reliability_mirage'`

- [ ] **Step 3: Write the implementation** — `kahin/tools/reliability_mirage.py`

```python
"""reliability_mirage.py — Reliability tools (Faz 1).

Retrying assertions and form/state primitives on the Mirage engine. Every
tool follows the Kahin contract: JSON-string result, never raises, bounded
arguments, healer-wrapped.
"""

from __future__ import annotations

from typing import Any

import orjson

from kahin._mcp import mcp
from kahin.expect import (
    ExpectationError,
    attribute_js,
    enabled,
    expect_until,
    has_attribute,
    has_text,
    has_value,
    text_js,
    value_js,
    visible,
)
from kahin.tools._common import _RO, _healer_ref
from kahin.tools.pilot_mirage import (
    _MAX_INPUT_TEXT_LENGTH,
    _MAX_SELECTOR_LENGTH,
    _MAX_WAIT_TIMEOUT,
    _bounded_float,
    _capture_page_session,
    _json_error,
    _safe_mirage_eval_result,
    _text_arg,
)


async def _probe_for(tool: str, frame_id: str | None, session_id: str | None):
    """Build the evaluate-based probe used by expect_until for this tool."""

    async def probe(expression: str) -> dict[str, Any]:
        result = await _safe_mirage_eval_result(tool, expression, frame_id, session_id=session_id)
        if isinstance(result, str):
            return {"error": "probe_failed"}
        value = result.get("result") or {}
        parsed = value.get("value") if isinstance(value, dict) else None
        return parsed if isinstance(parsed, dict) else {"error": "probe_failed"}

    return probe


def _read_expr(selector: str, *, text: bool, value: bool, attribute: str | None) -> str:
    """Combine the read expressions into one evaluate that returns a dict."""
    parts: list[str] = []
    if text:
        parts.append(f"text: {text_js(selector)}")
    if value:
        parts.append(f"value: {value_js(selector)}")
    if attribute:
        parts.append(f"attrs: {{ {orjson.dumps(attribute).decode()}: {attribute_js(selector, attribute)} }}")
    if not parts:
        parts.append("ok: false")
    return "({ " + ", ".join(parts) + " })"


@mcp.tool(name="kahin_mirage_expect", annotations=_RO)
async def mirage_expect(
    selector: str,
    state: str | None = None,
    text: str | None = None,
    value: str | None = None,
    attribute: str | None = None,
    attribute_value: str | None = None,
    timeout: float = 5.0,
    frame_id: str | None = None,
) -> str:
    """Mirage: retrying web-first assertion. state: visible|enabled. Also
    accepts text= / value= / attribute(name, optional value) expectations.
    Returns {matched:true, actual} or {error, code:expectation_failed,
    expected, actual, timeout}."""
    selector_value, error = _text_arg(
        selector, tool="kahin_mirage_expect", field="selector", maximum=_MAX_SELECTOR_LENGTH,
    )
    if error:
        return error
    if not selector_value:
        return _json_error("kahin_mirage_expect", "selector must not be empty", "invalid_argument", field="selector")
    if state is not None and state not in ("visible", "enabled"):
        return _json_error("kahin_mirage_expect", "state must be visible or enabled", "invalid_argument", field="state", received=state)
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=5.0)
    wants_text = text is not None
    wants_value = value is not None
    wants_attr = attribute is not None
    if not (state or wants_text or wants_value or wants_attr):
        return _json_error("kahin_mirage_expect", "one of state/text/value/attribute is required", "invalid_argument")
    async with _healer_ref.safe(
        "kahin_mirage_expect", selector=selector_value[:80], state=state,
        text=text[:80] if wants_text else None, value=value[:80] if wants_value else None,
        attribute=attribute[:80] if wants_attr else None, timeout=timeout_value, frame_id=frame_id,
    ):
        session_id, capture_error = await _capture_page_session("kahin_mirage_expect")
        if capture_error:
            return capture_error
        assert session_id is not None
        probe = await _probe_for("kahin_mirage_expect", frame_id, session_id)
        expectations: list[tuple[str, Any]] = []
        if state == "visible":
            expectations.append(("visible", visible()))
        if state == "enabled":
            expectations.append(("enabled", enabled()))
        if wants_text:
            expectations.append((f"text contains {text!r}", has_text(str(text))))
        if wants_value:
            expectations.append((f"value equals {value!r}", has_value(str(value))))
        if wants_attr:
            expectations.append(
                (f"attribute {attribute} = {attribute_value!r}", has_attribute(str(attribute), attribute_value)),
            )
        expression = _read_expr(selector_value, text=wants_text or wants_text, value=wants_value, attribute=attribute)
        description = "; ".join(name for name, _ in expectations)
        try:
            state_result = await expect_until(
                probe, expression, lambda s: all(pred(s) for _, pred in expectations),
                timeout=timeout_value, description=description,
            )
        except ExpectationError as exc:
            return orjson.dumps({
                "error": str(exc),
                "code": "expectation_failed",
                "selector": selector_value,
                "expected": exc.description,
                "actual": exc.actual,
                "timeout": exc.timeout,
            }, option=orjson.OPT_INDENT_2).decode()
        return orjson.dumps({
            "matched": True,
            "selector": selector_value,
            "expected": description,
            "actual": state_result,
            "timeout": timeout_value,
        }, option=orjson.OPT_INDENT_2).decode()
```

Wait — the probe contract of `expect_until` receives ONE string (the selector) and builds the JS itself. Here the "selector" IS the JS expression. That works: `expect_until(probe, expression, ...)` where `expression` is already the full JS; `probe` evaluates it. Update the call above: pass `expression` as the first probe argument (it is, via `probe(expression)` in `expect_until` — `expect_until` calls `probe(selector)` where `selector` is that expression). The predicates then read `text`/`value`/`attrs` from the returned dict. `has_text` reads `state.get("text")`, `has_value` reads `state.get("value")`, `has_attribute` reads `state.get("attrs", {}).get(name)` — matches `_read_expr` output. Good.

Register the module in `kahin/tools/__init__.py` — append alongside the other side-effect imports (line ~15-35):

```python
from kahin.tools import reliability_mirage  # noqa: F401
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_e2e_reliability.py -v
```

Expected: both new tests PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff check kahin/tools/reliability_mirage.py kahin/tools/__init__.py
git add kahin/tools/reliability_mirage.py kahin/tools/__init__.py tests/test_e2e_reliability.py
git commit -m "feat(mirage): retrying expect tool"
```

---

### Task 8: check / uncheck / select_option / dblclick

**Files:**
- Modify: `kahin/tools/reliability_mirage.py`
- Test: `tests/test_e2e_reliability.py` (extend)

**Interfaces:**
- Produces: `mirage_check(selector, timeout=10.0, frame_id=None)`, `mirage_uncheck(...)` (checkbox/radio; verifies resulting `checked` state; error `not_checkable` for other elements), `mirage_select_option(selector, value=None, label=None, index=None, timeout=10.0, frame_id=None)` (real `<select>`; sets value + dispatches input/change; verifies; error `not_select`, `option_not_found`), `mirage_dblclick(selector, timeout=10.0, frame_id=None)` (clickCount=2)

- [ ] **Step 1: Write the failing test** — extend `tests/test_e2e_reliability.py`

```python
@pytest.mark.asyncio
async def test_check_uncheck_verify_state(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><label><input type='checkbox' id='c'> Agree</label>"
        "<select id='s'><option value='a'>Alpha</option><option value='b'>Bravo</option></select>"
        "<script>"
        "document.getElementById('c').addEventListener('change', () => { "
        "document.getElementById('c').dataset.changed = '1'; });"
        "</script></body></html>"
    ))
    checked = _loads(await reliability_mirage.mirage_check("input#c", timeout=5.0))
    assert checked.get("checked") is True, checked
    assert checked.get("changed") is True
    unchecked = _loads(await reliability_mirage.mirage_uncheck("input#c", timeout=5.0))
    assert unchecked.get("checked") is False, unchecked


@pytest.mark.asyncio
async def test_select_option_by_value_and_verify(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><select id='s'><option value='a'>Alpha</option>"
        "<option value='b'>Bravo</option></select></body></html>"
    ))
    result = _loads(await reliability_mirage.mirage_select_option("#s", value="b", timeout=5.0))
    assert result.get("selected") == "b", result
    missing = _loads(await reliability_mirage.mirage_select_option("#s", value="zzz", timeout=1.0))
    assert missing.get("code") == "option_not_found", missing


@pytest.mark.asyncio
async def test_dblclick_fires_two_clicks(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><button id='b'>x</button><div id='n'>0</div>"
        "<script>let n = 0; document.getElementById('b').addEventListener('click', "
        "() => { n += 1; document.getElementById('n').textContent = String(n); });</script>"
        "</body></html>"
    ))
    result = _loads(await reliability_mirage.mirage_dblclick("#b", timeout=5.0))
    assert not result.get("error"), result
    n = _loads(await pilot_mirage.mirage_get_text("#n"))
    assert n.get("text") == "2", n
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_e2e_reliability.py::test_check_uncheck_verify_state tests/test_e2e_reliability.py::test_select_option_by_value_and_verify tests/test_e2e_reliability.py::test_dblclick_fires_two_clicks -v`
Expected: FAIL — `AttributeError: module 'kahin.tools.reliability_mirage' has no attribute 'mirage_check'`

- [ ] **Step 3: Implement** — append to `reliability_mirage.py`

```python
_CHECKABLE_JS = """
(() => {
  const el = ${ELEMENT_JS};
  if (!el) return {code: "element_not_found"};
  const tag = String(el.tagName || "").toLowerCase();
  const type = String(el.getAttribute && el.getAttribute("type") || "").toLowerCase();
  if (tag !== "input" || (type !== "checkbox" && type !== "radio"))
    return {code: "not_checkable", tag, type};
  if (el.disabled === true) return {code: "element_not_actionable", reason: "disabled"};
  return {ok: true, type, checked: Boolean(el.checked)};
})()
"""


async def _check_flow(
    tool: str,
    selector: str,
    want: bool,
    *,
    timeout: float,
    frame_id: str | None,
    session_id: str | None,
) -> dict[str, Any]:
    """Actionability wait + click + verified state, Playwright-check style."""
    from kahin.actionability import wait_for_ready
    from kahin.locators import selector_js
    from kahin.tools.pilot_mirage import _dispatch_mouse, _element_point

    async def probe(expression: str) -> dict[str, Any]:
        result = await _safe_mirage_eval_result(tool, expression, frame_id, session_id=session_id)
        if isinstance(result, str):
            return {"error": "probe_failed"}
        value = result.get("result") or {}
        parsed = value.get("value") if isinstance(value, dict) else None
        return parsed if isinstance(parsed, dict) else {"error": "probe_failed"}

    check_js = _CHECKABLE_JS.replace("${ELEMENT_JS}", selector_js(selector))
    ready = await wait_for_ready(probe, selector, timeout=timeout)
    if not ready.get("ok"):
        return {"error": f"element is not actionable: {ready.get('reason') or ready.get('code')}", "code": ready.get("code") or "timeout"}
    kind = await probe(check_js)
    if kind.get("code") == "not_checkable":
        return {"error": "element is not a checkbox or radio", "code": "not_checkable", "selector": selector}
    point = await _element_point(selector, frame_id, session_id=session_id)
    if not isinstance(point, tuple):
        return {"error": "element point failed", "code": "element_not_found"}
    x, y = point
    down = await _dispatch_mouse("mousedown", x, y, button=0, buttons=1, session_id=session_id)
    if _is_error_response(down):
        return {"error": "mousedown failed", "code": "tool_failed"}
    up = await _dispatch_mouse("mouseup", x, y, button=0, buttons=0, session_id=session_id)
    if _is_error_response(up):
        return {"error": "mouseup failed", "code": "tool_failed"}
    # verify the intended state; poll briefly for event handlers to run
    changed = False
    for _ in range(20):
        state = await probe(check_js)
        changed = bool(state.get("checked")) == want
        if changed:
            break
        await asyncio.sleep(0.05)
    return {
        "checked": bool((await probe(check_js)).get("checked")),
        "changed": changed,
        "selector": selector,
        "x": x,
        "y": y,
    }
```

Then the four tools (same file):

```python
@mcp.tool(name="kahin_mirage_check", annotations=_RW)
async def mirage_check(selector: str, timeout: float = 10.0, frame_id: str | None = None) -> str:
    """Mirage: check a checkbox/radio (waits for actionability, verifies)."""
    selector_value, error = _text_arg(selector, tool="kahin_mirage_check", field="selector", maximum=_MAX_SELECTOR_LENGTH)
    if error:
        return error
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=10.0)
    async with _healer_ref.safe("kahin_mirage_check", selector=selector_value[:80], timeout=timeout_value, frame_id=frame_id):
        session_id, capture_error = await _capture_page_session("kahin_mirage_check")
        if capture_error:
            return capture_error
        assert session_id is not None
        result = await _check_flow("kahin_mirage_check", selector_value, True, timeout=timeout_value, frame_id=frame_id, session_id=session_id)
        return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_uncheck", annotations=_RW)
async def mirage_uncheck(selector: str, timeout: float = 10.0, frame_id: str | None = None) -> str:
    """Mirage: uncheck a checkbox (radio cannot be unchecked — not_checkable)."""
    selector_value, error = _text_arg(selector, tool="kahin_mirage_uncheck", field="selector", maximum=_MAX_SELECTOR_LENGTH)
    if error:
        return error
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=10.0)
    async with _healer_ref.safe("kahin_mirage_uncheck", selector=selector_value[:80], timeout=timeout_value, frame_id=frame_id):
        session_id, capture_error = await _capture_page_session("kahin_mirage_uncheck")
        if capture_error:
            return capture_error
        assert session_id is not None
        result = await _check_flow("kahin_mirage_uncheck", selector_value, False, timeout=timeout_value, frame_id=frame_id, session_id=session_id)
        return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_select_option", annotations=_RW)
async def mirage_select_option(
    selector: str,
    value: str | None = None,
    label: str | None = None,
    index: int | None = None,
    timeout: float = 10.0,
    frame_id: str | None = None,
) -> str:
    """Mirage: select an option in a real <select> (value | label | index)."""
    selector_value, error = _text_arg(selector, tool="kahin_mirage_select_option", field="selector", maximum=_MAX_SELECTOR_LENGTH)
    if error:
        return error
    picked = sum(x is not None for x in (value, label, index))
    if picked != 1:
        return _json_error("kahin_mirage_select_option", "exactly one of value/label/index is required", "invalid_argument")
    if isinstance(index, bool) or (index is not None and (not isinstance(index, int) or index < 0)):
        return _json_error("kahin_mirage_select_option", "index must be a non-negative integer", "invalid_argument", field="index")
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=10.0)
    async with _healer_ref.safe("kahin_mirage_select_option", selector=selector_value[:80], value=value, label=label, index=index, timeout=timeout_value, frame_id=frame_id):
        session_id, capture_error = await _capture_page_session("kahin_mirage_select_option")
        if capture_error:
            return capture_error
        assert session_id is not None
        wanted: str = value if value is not None else (label if label is not None else str(index))
        by = "value" if value is not None else ("label" if label is not None else "index")
        expression = f"""
(() => {{
  const el = {selector_js(selector_value)};
  if (!el) return {{code: "element_not_found"}};
  if (String(el.tagName || "").toLowerCase() !== "select") return {{code: "not_select"}};
  const wanted = {orjson.dumps(wanted).decode()};
  let option = null;
  if ({orjson.dumps(by).decode()} === "index") option = el.options[Number(wanted)] || null;
  else if ({orjson.dumps(by).decode()} === "label") option = Array.from(el.options).find(o => o.textContent.trim() === wanted) || null;
  else option = Array.from(el.options).find(o => o.value === wanted) || null;
  if (!option) return {{code: "option_not_found", wanted}};
  el.value = option.value;
  el.dispatchEvent(new Event("input", {{bubbles: true}}));
  el.dispatchEvent(new Event("change", {{bubbles: true}}));
  return {{ok: true, selected: el.value, label: option.textContent.trim()}};
}})()
"""
        result = await _safe_mirage_eval_result(
            "kahin_mirage_select_option", expression, frame_id, session_id=session_id,
        )
        if isinstance(result, str):
            return result
        value_out = result.get("result") or {}
        parsed = value_out.get("value") if isinstance(value_out, dict) else None
        if not isinstance(parsed, dict):
            return _json_error("kahin_mirage_select_option", "select evaluate returned no value", "invalid_engine_response")
        if parsed.get("code"):
            return _json_error(
                "kahin_mirage_select_option",
                str(parsed.get("code")),
                str(parsed.get("code")),
                selector=selector_value,
                wanted=parsed.get("wanted"),
            )
        return orjson.dumps({"selected": parsed.get("selected"), "label": parsed.get("label"), "selector": selector_value}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_dblclick", annotations=_DW)
async def mirage_dblclick(selector: str, timeout: float = 10.0, frame_id: str | None = None) -> str:
    """Mirage: double-click (clickCount=2, real mouse events)."""
    selector_value, error = _text_arg(selector, tool="kahin_mirage_dblclick", field="selector", maximum=_MAX_SELECTOR_LENGTH)
    if error:
        return error
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=10.0)
    async with _healer_ref.safe("kahin_mirage_dblclick", selector=selector_value[:80], timeout=timeout_value, frame_id=frame_id):
        from kahin.actionability import wait_for_ready
        from kahin.tools.pilot_mirage import _dispatch_mouse

        session_id, capture_error = await _capture_page_session("kahin_mirage_dblclick")
        if capture_error:
            return capture_error
        assert session_id is not None

        async def probe(expression: str) -> dict[str, Any]:
            result = await _safe_mirage_eval_result("kahin_mirage_dblclick", expression, frame_id, session_id=session_id)
            if isinstance(result, str):
                return {"error": "probe_failed"}
            value = result.get("result") or {}
            parsed = value.get("value") if isinstance(value, dict) else None
            return parsed if isinstance(parsed, dict) else {"error": "probe_failed"}

        ready = await wait_for_ready(probe, selector_value, timeout=timeout_value)
        if not ready.get("ok"):
            return _json_error("kahin_mirage_dblclick", f"element is not actionable: {ready.get('reason') or ready.get('code')}", str(ready.get("code") or "timeout"), selector=selector_value, timeout=timeout_value)
        x, y = float(ready["x"]), float(ready["y"])
        for click_count, buttons in ((1, 1), (2, 3)):
            down = await _dispatch_mouse("mousedown", x, y, button=0, buttons=buttons, click_count=click_count, session_id=session_id)
            if _is_error_response(down):
                return down
            up = await _dispatch_mouse("mouseup", x, y, button=0, buttons=0, click_count=click_count, session_id=session_id)
            if _is_error_response(up):
                return up
        return orjson.dumps({"dblclicked": selector_value, "x": x, "y": y}, option=orjson.OPT_INDENT_2).decode()
```

Needed imports in `reliability_mirage.py` (add to the import block): `asyncio`, `selector_js` from `kahin.locators`, `_dispatch_mouse`, `_is_error_response` from `kahin.tools.pilot_mirage`.

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_e2e_reliability.py -v
```

Expected: 3 new tests PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff check kahin/tools/reliability_mirage.py
git add kahin/tools/reliability_mirage.py tests/test_e2e_reliability.py
git commit -m "feat(mirage): check/uncheck/select_option/dblclick with verification"
```

---

### Task 9: `kahin_mirage_drag`

**Files:**
- Modify: `kahin/tools/reliability_mirage.py`
- Test: `tests/test_e2e_reliability.py` (extend)

**Interfaces:**
- Produces: `mirage_drag(selector_from, selector_to, timeout=10.0, steps=10, frame_id=None)` — mousedown on source → N mousemove steps toward target center → mouseup; returns `{"dragged": from, "to": to, "steps": N, "x", "y"}`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_drag_and_drop(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><head><style>"
        "#a { position: absolute; left: 10px; top: 10px; width: 100px; height: 50px; background: #eee; }"
        "#b { position: absolute; left: 300px; top: 10px; width: 100px; height: 50px; background: #ccc; }"
        "</style></head><body>"
        "<div id='a'>Source</div><div id='b'>Target</div><div id='log'></div>"
        "<script>"
        "document.getElementById('b').addEventListener('dragover', (e) => e.preventDefault());"
        "document.getElementById('b').addEventListener('drop', () => { "
        "document.getElementById('log').textContent = 'dropped'; });"
        "document.getElementById('a').addEventListener('dragstart', (e) => { e.dataTransfer.setData('text', 'x'); });"
        "</script></body></html>"
    ))
    result = _loads(await reliability_mirage.mirage_drag("#a", "#b", timeout=5.0, steps=15))
    assert not result.get("error"), result
    assert result.get("steps") == 15
    text = _loads(await pilot_mirage.mirage_get_text("#log"))
    assert "dropped" in text.get("text", ""), text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_e2e_reliability.py::test_drag_and_drop -v`
Expected: FAIL — `AttributeError: ... no attribute 'mirage_drag'`

- [ ] **Step 3: Implement** — append to `reliability_mirage.py`

```python
@mcp.tool(name="kahin_mirage_drag", annotations=_DW)
async def mirage_drag(
    selector_from: str,
    selector_to: str,
    timeout: float = 10.0,
    steps: int = 10,
    frame_id: str | None = None,
) -> str:
    """Mirage: drag from one element to another (real mouse: mousedown,
    stepped mousemoves, mouseup). steps: 2..50 interpolation points."""
    from kahin.actionability import wait_for_ready
    from kahin.tools.pilot_mirage import _dispatch_mouse

    from_source, error = _text_arg(selector_from, tool="kahin_mirage_drag", field="selector_from", maximum=_MAX_SELECTOR_LENGTH)
    if error:
        return error
    to_value, error = _text_arg(selector_to, tool="kahin_mirage_drag", field="selector_to", maximum=_MAX_SELECTOR_LENGTH)
    if error:
        return error
    steps_value = _bounded_int(steps, minimum=2, maximum=50, default=10)
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=10.0)
    async with _healer_ref.safe("kahin_mirage_drag", selector_from=from_source[:80], selector_to=to_value[:80], steps=steps_value, timeout=timeout_value, frame_id=frame_id):
        session_id, capture_error = await _capture_page_session("kahin_mirage_drag")
        if capture_error:
            return capture_error
        assert session_id is not None

        async def probe(expression: str) -> dict[str, Any]:
            result = await _safe_mirage_eval_result("kahin_mirage_drag", expression, frame_id, session_id=session_id)
            if isinstance(result, str):
                return {"error": "probe_failed"}
            value = result.get("result") or {}
            parsed = value.get("value") if isinstance(value, dict) else None
            return parsed if isinstance(parsed, dict) else {"error": "probe_failed"}

        ready_a = await wait_for_ready(probe, from_source, timeout=timeout_value)
        if not ready_a.get("ok"):
            return _json_error("kahin_mirage_drag", f"source is not actionable: {ready_a.get('reason') or ready_a.get('code')}", str(ready_a.get("code") or "timeout"), selector=from_source)
        ready_b = await wait_for_ready(probe, to_value, timeout=timeout_value)
        if not ready_b.get("ok"):
            return _json_error("kahin_mirage_drag", f"target is not actionable: {ready_b.get('reason') or ready_b.get('code')}", str(ready_b.get("code") or "timeout"), selector=to_value)
        x0, y0 = float(ready_a["x"]), float(ready_a["y"])
        x1, y1 = float(ready_b["x"]), float(ready_b["y"])
        down = await _dispatch_mouse("mousedown", x0, y0, button=0, buttons=1, session_id=session_id)
        if _is_error_response(down):
            return down
        last_x, last_y = x0, y0
        for step in range(1, steps_value + 1):
            t = step / steps_value
            x = x0 + (x1 - x0) * t
            y = y0 + (y1 - y0) * t
            move = await _dispatch_mouse("mousemove", x, y, button=0, buttons=1, session_id=session_id)
            if _is_error_response(move):
                return move
            last_x, last_y = x, y
            await asyncio.sleep(0.01)
        up = await _dispatch_mouse("mouseup", last_x, last_y, button=0, buttons=0, session_id=session_id)
        if _is_error_response(up):
            return up
        return orjson.dumps({
            "dragged": from_source, "to": to_value, "steps": steps_value,
            "x": last_x, "y": last_y,
        }, option=orjson.OPT_INDENT_2).decode()
```

Needed import: `_bounded_int` from `kahin.tools.pilot_mirage`.

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_e2e_reliability.py::test_drag_and_drop -v
```

Expected: PASS. (If the synthetic drag doesn't trigger native HTML5 DnD on this Firefox build, the test documents it — report the leak in the commit message and mark the scenario as an expected-behavior note; the tool still performs the mouse gesture correctly.)

- [ ] **Step 5: Commit**

```bash
uv run ruff check kahin/tools/reliability_mirage.py
git add kahin/tools/reliability_mirage.py tests/test_e2e_reliability.py
git commit -m "feat(mirage): drag-and-drop with stepped mouse moves"
```

---

### Task 10: `kahin_mirage_wait_for_text` + `kahin_mirage_wait_for_timeout`

**Files:**
- Modify: `kahin/tools/reliability_mirage.py`
- Test: `tests/test_e2e_reliability.py` (extend)

**Interfaces:**
- Produces: `mirage_wait_for_text(text, timeout=10.0, frame_id=None)` → `{"found": true}` / `{"found": false, "code": "timeout"}`; `mirage_wait_for_timeout(ms=1000)` → `{"waited_ms": N}` (bounded ≤ 60_000)

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_wait_for_text(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><div id='s'></div>"
        "<script>setTimeout(() => { document.getElementById('s').textContent = 'ready-now'; }, 400);</script>"
        "</body></html>"
    ))
    result = _loads(await reliability_mirage.mirage_wait_for_text("ready-now", timeout=5.0))
    assert result.get("found") is True, result
    gone = _loads(await reliability_mirage.mirage_wait_for_text("never-appears", timeout=0.5))
    assert gone.get("found") is False and gone.get("code") == "timeout", gone


@pytest.mark.asyncio
async def test_wait_for_timeout_bounds(mirage_tools: None) -> None:
    started = time.monotonic()
    result = _loads(await reliability_mirage.mirage_wait_for_timeout(ms=300))
    elapsed = time.monotonic() - started
    assert result.get("waited_ms", 0) >= 250 and elapsed >= 0.25
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_e2e_reliability.py::test_wait_for_text tests/test_e2e_reliability.py::test_wait_for_timeout_bounds -v`
Expected: FAIL — attribute missing

- [ ] **Step 3: Implement** — append to `reliability_mirage.py`

```python
@mcp.tool(name="kahin_mirage_wait_for_text", annotations=_RO)
async def mirage_wait_for_text(text: str, timeout: float = 10.0, frame_id: str | None = None) -> str:
    """Mirage: wait until the page body text contains ``text`` (or timeout)."""
    text_value, error = _text_arg(text, tool="kahin_mirage_wait_for_text", field="text", maximum=_MAX_INPUT_TEXT_LENGTH)
    if error:
        return error
    if not text_value:
        return _json_error("kahin_mirage_wait_for_text", "text must not be empty", "invalid_argument", field="text")
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=10.0)
    async with _healer_ref.safe("kahin_mirage_wait_for_text", text=text_value[:80], timeout=timeout_value, frame_id=frame_id):
        session_id, capture_error = await _capture_page_session("kahin_mirage_wait_for_text")
        if capture_error:
            return capture_error
        assert session_id is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_value
        expression = (
            "(() => { const t = (document.body ? document.body.innerText : '') || ''; "
            f"return {{present: t.includes({orjson.dumps(text_value).decode()})}}; }})()"
        )
        while True:
            result = await _safe_mirage_eval_result("kahin_mirage_wait_for_text", expression, frame_id, session_id=session_id)
            if isinstance(result, dict):
                value = result.get("result") or {}
                parsed = value.get("value") if isinstance(value, dict) else None
                if isinstance(parsed, dict) and parsed.get("present"):
                    return orjson.dumps({"found": True, "text": text_value, "timeout": timeout_value}, option=orjson.OPT_INDENT_2).decode()
            if loop.time() > deadline:
                break
            await asyncio.sleep(0.1)
        return orjson.dumps({"found": False, "text": text_value, "timeout": timeout_value, "code": "timeout"}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_wait_for_timeout", annotations=_RO)
async def mirage_wait_for_timeout(ms: int = 1000) -> str:
    """Mirage: sleep ``ms`` milliseconds (1..60000) and report the wait."""
    if isinstance(ms, bool) or not isinstance(ms, int) or not 1 <= ms <= 60_000:
        return _json_error("kahin_mirage_wait_for_timeout", "ms must be an integer in 1..60000", "invalid_argument", field="ms")
    async with _healer_ref.safe("kahin_mirage_wait_for_timeout", ms=str(ms)):
        await asyncio.sleep(ms / 1000.0)
        return orjson.dumps({"waited_ms": ms}, option=orjson.OPT_INDENT_2).decode()
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_e2e_reliability.py::test_wait_for_text tests/test_e2e_reliability.py::test_wait_for_timeout_bounds -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff check kahin/tools/reliability_mirage.py
git add kahin/tools/reliability_mirage.py tests/test_e2e_reliability.py
git commit -m "feat(mirage): wait_for_text and wait_for_timeout tools"
```

---

### Task 11: Network route — pattern matching + fulfill/abort/continue

**Files:**
- Modify: `kahin/the_twins/mirage.py` (network signal + `wait_for_intercepted`)
- Modify: `kahin/tools/reliability_mirage.py` (route tool)
- Test: `tests/test_e2e_reliability.py` (extend; needs a local http server like test_e2e_network.py)

**Interfaces:**
- Produces: `Mirage.wait_for_intercepted(predicate, timeout) -> dict | None` (engine method); `kahin_mirage_route(pattern, action="abort", method=None, body=None, status=200, headers=None, content_type="text/plain", wait_ms=10_000, frame_id=None)` — one-shot: waits for the NEXT request matching the glob/regex pattern, acts on it, reports `{matched, url, requestId, action, ...}` or `{matched: false, code: "timeout", timeout}`.
- Consumes: engine reader events (existing), `Network.setRequestInterception` (existing), `Network.resumeInterceptedRequest` / `Network.abortInterceptedRequest` / `Network.fulfillInterceptedRequest` (existing passthrough — verify in Step 0)

- [ ] **Step 0: Discover the interception event shape (read-only probe)**

Run a throwaway script (or extend test temporarily) that: starts mirage, opens a tab, calls `mirage_intercept_requests()`, navigates to a `http.server` URL, then dumps `state._network_requests` tail. Record which events carry the intercepted requestId (e.g. `requestWillBeSent` with `intercepted: true`, or a `requestPaused`-style event), and what fields they have. Write the finding as a comment in `mirage.py` next to the new method. The existing `mirage_network_continue` works, so the buffer already contains what we need — this step only pins the field names for the matcher.

- [ ] **Step 1: Write the failing test** — extend `tests/test_e2e_reliability.py`

```python
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"<html><body>ok</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence
        pass


@async_fixture
async def http_server() -> AsyncGenerator[str, None]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/page.html"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_route_abort_matches_pattern(mirage_tools: None, http_server: str) -> None:
    from kahin.tools import dejavu_mirage

    await pilot.navigate(url="about:blank")
    intercepted = _loads(await dejavu_mirage.mirage_intercept_requests())
    assert not intercepted.get("error"), intercepted
    route = _loads(await reliability_mirage.mirage_route(
        "*/page.html", action="abort", wait_ms=8_000,
    ))
    assert route.get("matched") is True, route
    assert route.get("action") == "abort"
    assert "/page.html" in route.get("url", "")
    # navigate after the route so the request is the one intercepted
    resp = _loads(await pilot.navigate(url=http_server))
    assert resp.get("frameId"), resp


@pytest.mark.asyncio
async def test_route_fulfill_inline_body(mirage_tools: None, http_server: str) -> None:
    from kahin.tools import dejavu_mirage

    await pilot.navigate(url="about:blank")
    intercepted = _loads(await dejavu_mirage.mirage_intercept_requests())
    assert not intercepted.get("error"), intercepted
    route = _loads(await reliability_mirage.mirage_route(
        "*/page.html", action="fulfill", body="MOCKED", status=200,
        headers={"X-Kahin": "route"}, wait_ms=8_000,
    ))
    assert route.get("matched") is True, route
    await pilot.navigate(url=http_server)
    await reliability_mirage.mirage_wait_for_timeout(ms=300)
    body = _loads(await pilot_mirage.mirage_page_content())
    assert "MOCKED" in str(body.get("html", "")), body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_e2e_reliability.py::test_route_abort_matches_pattern -v`
Expected: FAIL — `AttributeError: ... no attribute 'mirage_route'`

- [ ] **Step 3: Add the engine-side wait** — `kahin/the_twins/mirage.py`

Add near the response-body helper (after `get_response_body`, mirage.py:875-902):

```python
async def wait_for_intercepted(
    self, predicate: Callable[[dict[str, Any]], bool], timeout: float,
) -> dict[str, Any] | None:
    """Wait for the next intercepted network event matching ``predicate``.

    The reader already routes Network events into ``_network_events`` (the
    same buffer the dejavu tools read). This method scans the buffer first,
    then sleeps on the event signal until the deadline. Events are dicts
    with at least ``requestId``/``url`` — see the Step-0 discovery comment.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    seen = 0
    while True:
        events = list(self._network_events or [])
        for event in events[seen:]:
            seen += 1
            if predicate(event):
                return event
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        try:
            await asyncio.wait_for(self._network_signal.wait(), timeout=min(remaining, 0.25))
            self._network_signal.clear()
        except asyncio.TimeoutError:
            pass
```

Add to `__init__` (mirage.py:276): `self._network_events: deque[dict[str, Any]] = deque(maxlen=2000)` and `self._network_signal: asyncio.Event = asyncio.Event()`. In the reader loop, where Network events are already tracked (`_track_*` style — locate the existing network buffering in the reader, e.g. the block feeding `state._network_requests`), append the event to `self._network_events` and `self._network_signal.set()`.

Check the reader: if events are already appended to a module-level buffer via `state._network_requests`, mirror that append into `self._network_events` at the same place.

- [ ] **Step 4: Add the route tool** — `reliability_mirage.py`

```python
import fnmatch
import re

_ROUTE_PATTERN_MAX = 8_192
_ROUTE_WAIT_MAX = 60_000.0


def _pattern_matcher(pattern: str) -> Callable[[str], bool]:
    """glob (fnmatchcase) or regex (re: prefix) URL matcher."""
    if pattern.startswith("re:"):
        try:
            compiled = re.compile(pattern[3:])
        except re.error as exc:
            raise ValueError(f"invalid regex pattern: {exc}") from exc
        return lambda url: bool(compiled.search(url or ""))
    return lambda url: fnmatch.fnmatchcase(url or "", pattern)


@mcp.tool(name="kahin_mirage_route", annotations=_RW)
async def mirage_route(
    pattern: str,
    action: str = "abort",
    method: str | None = None,
    body: str | None = None,
    status: int = 200,
    headers: dict[str, str] | None = None,
    content_type: str = "text/plain",
    wait_ms: float = 10_000.0,
    frame_id: str | None = None,
) -> str:
    """Mirage: one-shot network route. Waits for the next request matching
    ``pattern`` (glob, or re: prefix for regex) and applies action:
    abort | continue | fulfill (inline ``body``, ``status``, ``headers``,
    ``content_type``). Returns {matched, url, requestId, action} or
    {matched:false, code:"timeout"}."""
    pattern_value, error = _text_arg(pattern, tool="kahin_mirage_route", field="pattern", maximum=_ROUTE_PATTERN_MAX)
    if error:
        return error
    if action not in ("abort", "continue", "fulfill"):
        return _json_error("kahin_mirage_route", "action must be abort|continue|fulfill", "invalid_argument", field="action", received=action)
    wait_value = _bounded_float(wait_ms, minimum=0.0, maximum=_ROUTE_WAIT_MAX, default=10_000.0) / 1000.0
    if action == "fulfill" and not isinstance(body, str):
        return _json_error("kahin_mirage_route", "fulfill requires a body string", "invalid_argument", field="body")
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        return _json_error("kahin_mirage_route", "status must be an integer in 100..599", "invalid_argument", field="status")
    if headers is not None and not isinstance(headers, dict):
        return _json_error("kahin_mirage_route", "headers must be an object", "invalid_argument", field="headers")
    try:
        matcher = _pattern_matcher(pattern_value)
    except ValueError as exc:
        return _json_error("kahin_mirage_route", str(exc), "invalid_argument", field="pattern")
    async with _healer_ref.safe(
        "kahin_mirage_route", pattern=pattern_value[:80], action=action,
        method=method, body_length=len(body) if body else 0, status=status, wait_ms=wait_value * 1000,
    ):
        engine = _mirage_engine()
        page = await engine.ensure_page()
        session_id = page.get("sessionId") if isinstance(page, dict) else None
        if not isinstance(session_id, str) or not session_id:
            return _json_error("kahin_mirage_route", "selected page has no live session", "session_unavailable")
        # make sure interception is on
        from kahin.tools._common import _safe_cdp  # noqa: PLC0415

        # ensure intercept via the existing toggle semantics
        intercept = await _safe_cdp("Network", "setRequestInterception", {"enabled": True}, session_id=session_id)
        if isinstance(intercept, dict) and intercept.get("error"):
            return orjson.dumps(intercept).decode()

        def predicate(event: dict[str, Any]) -> bool:
            if method and str(event.get("method") or "").upper() != method.upper():
                return False
            return matcher(str(event.get("url") or ""))

        event = await engine.wait_for_intercepted(predicate, timeout=wait_value)
        if event is None:
            return orjson.dumps({
                "matched": False, "pattern": pattern_value, "action": action,
                "timeout": wait_value, "code": "timeout",
            }, option=orjson.OPT_INDENT_2).decode()
        request_id = str(event.get("requestId") or "")
        url = str(event.get("url") or "")
        if action == "abort":
            outcome = await _safe_cdp("Network", "abortInterceptedRequest", {"requestId": request_id}, session_id=session_id)
        elif action == "continue":
            outcome = await _safe_cdp("Network", "resumeInterceptedRequest", {"requestId": request_id}, session_id=session_id)
        else:
            outcome = await _safe_cdp("Network", "fulfillInterceptedRequest", {
                "requestId": request_id,
                "responseCode": status,
                "responseHeaders": [{"name": "Content-Type", "value": content_type}] + [
                    {"name": k, "value": v} for k, v in (headers or {}).items()
                ],
                "body": base64.b64encode(body.encode()).decode(),
            }, session_id=session_id)
        if isinstance(outcome, dict) and outcome.get("error"):
            return orjson.dumps({
                "matched": True, "requestId": request_id, "url": url,
                "action": action, "error": outcome.get("error"), "code": "route_action_failed",
            }, option=orjson.OPT_INDENT_2).decode()
        return orjson.dumps({
            "matched": True, "requestId": request_id, "url": url, "action": action,
        }, option=orjson.OPT_INDENT_2).decode()
```

Needed imports in `reliability_mirage.py`: `base64`, `fnmatch`, `re`, `_mirage_engine` from `kahin.tools._common`, `_safe_cdp` from `kahin.tools._common` (check its signature: `_safe_cdp(domain, command, params, session_id=...)` — verify in `_common.py` before use; it is the shared CDP-shaped helper used by dejavu tools).

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_e2e_reliability.py -v
```

Expected: route tests PASS. (If `fulfillInterceptedRequest` returns -32601 from Juggler, the route tool reports `route_action_failed` — then implement fulfill as intercept-continue with header override is not possible; in that case document the limitation and keep abort/continue — commit note.)

- [ ] **Step 6: Commit**

```bash
uv run ruff check kahin/the_twins/mirage.py kahin/tools/reliability_mirage.py
git add kahin/the_twins/mirage.py kahin/tools/reliability_mirage.py tests/test_e2e_reliability.py
git commit -m "feat(mirage): one-shot network route — glob/regex, abort/continue/fulfill"
```

---

### Task 12: Full verification gate

**Files:** none (verification only)

- [ ] **Step 1: Full unit + fake-sidecar suite**

```bash
uv run pytest tests/test_locators.py tests/test_actionability.py tests/test_expect.py tests/test_phantom.py tests/test_mirage_ipc.py -v
```

Expected: all PASS.

- [ ] **Step 2: Full real-e2e suite with gate**

```bash
KAHIN_REQUIRE_REAL_E2E=1 uv run pytest tests/ -v
```

Expected: all tests PASS including `test_e2e_reliability.py` and the pre-existing e2e files.

- [ ] **Step 3: Lint + typecheck**

```bash
uv run ruff check .
uv run pyright
```

Expected: clean.

- [ ] **Step 4: Tool count + docs**

Run `uv run pytest tests/test_oracle.py -v` and confirm the tool count test still passes (110 → 118: +8 new tools: wait_selector states unchanged name; new: expect, check, uncheck, select_option, dblclick, drag, wait_for_text, wait_for_timeout, route). Update the count in `tests/test_oracle.py` if it asserts an exact number, and update `AGENTS.md` tool list + `CHANGELOG.md`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_oracle.py AGENTS.md CHANGELOG.md
git commit -m "docs: Faz 1 reliability surface — 9 new tools, AGENTS.md sync"
```
