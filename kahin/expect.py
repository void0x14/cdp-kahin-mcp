"""expect.py — retrying (web-first) assertion engine.

Playwright's ``expect`` polls until the assertion holds or the timeout
elapses. The same semantics live here as pure Python: a probe callable
(evaluate-based, provided by the tool layer) plus a predicate. On timeout
an ``ExpectationError`` carries the last observed state so the tool layer
can build an actionable ``{"code": "expectation_failed", ...}`` payload.

The JS builders prefer the full locator engine (``kahin.locators``, Faz 1
Task 1) when it is available; until then they fall back to plain CSS
``document.querySelector`` so this module stays dependency-free.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

try:  # full locator engine (css/text/role/xpath/nth chaining)
    from kahin.locators import _q as _quote, selector_js as _selector_js
except ImportError:  # pragma: no cover - active only before Task 1 lands
    import orjson

    def _quote(value: str) -> str:
        """Python string -> JS string literal (quotes/unicode safe)."""
        return orjson.dumps(value).decode()

    def _selector_js(selector: str) -> str:
        """JS expression evaluating to the first CSS match (or null)."""
        return f"document.querySelector({_quote(selector)})"

_DEFAULT_TIMEOUT = 5.0
_DEFAULT_INTERVAL = 0.1


class ExpectationError(RuntimeError):
    """Raised when a retrying assertion does not hold within the timeout."""

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
    el = _selector_js(selector)
    return (
        f"(() => {{ const el = {el}; if (!el) return ''; "
        'return (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim(); })()'
    )


def value_js(selector: str) -> str:
    """JS expression evaluating to the element's value ("" if gone)."""
    el = _selector_js(selector)
    return (
        f"(() => {{ const el = {el}; if (!el) return ''; "
        "return el.value !== undefined && el.value !== null ? String(el.value) : ''; })()"
    )


def attribute_js(selector: str, name: str) -> str:
    """JS expression evaluating to an attribute value (null if gone)."""
    el = _selector_js(selector)
    return f"(() => {{ const el = {el}; if (!el) return null; return el.getAttribute({_quote(name)}); }})()"


def visible() -> Callable[[dict[str, Any]], bool]:
    """Predicate: the element is present, visible and stable (probe ``ok``)."""

    def predicate(state: dict[str, Any]) -> bool:
        return bool(state.get("ok"))

    predicate.__name__ = "visible"  # type: ignore[attr-defined]
    return predicate


def enabled() -> Callable[[dict[str, Any]], bool]:
    """Predicate: the element is enabled (probe ``ok``)."""

    def predicate(state: dict[str, Any]) -> bool:
        return bool(state.get("ok"))

    predicate.__name__ = "enabled"  # type: ignore[attr-defined]
    return predicate


def has_text(expected: str) -> Callable[[dict[str, Any]], bool]:
    """Predicate: the element's normalized text contains ``expected``."""

    def predicate(state: dict[str, Any]) -> bool:
        return expected in str(state.get("text") or "")

    predicate.__name__ = f"has_text({expected!r})"  # type: ignore[attr-defined]
    return predicate


def has_value(expected: str) -> Callable[[dict[str, Any]], bool]:
    """Predicate: the element's value equals ``expected``."""

    def predicate(state: dict[str, Any]) -> bool:
        return str(state.get("value") or "") == expected

    predicate.__name__ = f"has_value({expected!r})"  # type: ignore[attr-defined]
    return predicate


def has_attribute(name: str, expected: str | None = None) -> Callable[[dict[str, Any]], bool]:
    """Predicate: the element has ``name`` (optionally equal to ``expected``)."""

    def predicate(state: dict[str, Any]) -> bool:
        attributes = state.get("attributes")
        if not isinstance(attributes, dict):
            return False
        actual = attributes.get(name)
        if expected is None:
            return actual is not None
        return actual == expected

    predicate.__name__ = f"has_attribute({name!r})"  # type: ignore[attr-defined]
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
