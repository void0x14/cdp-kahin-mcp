"""pilot_mirage.py — Mirage (Camoufox/Juggler) PILOT tools.

Faz 9 Task 3: DOM, Input and PageEx tools, every one backed by a REAL
Juggler surface (verified against microsoft/playwright
``browser_patches/firefox/juggler/protocol/Protocol.js``):

- DOM tools: Runtime.evaluate for queries, Page.dispatchMouseEvent for
  clicks/hover, Page.insertText for typing.
- Input tools: Page.dispatchMouseEvent / Page.dispatchKeyEvent (repeat is a
  REQUIRED bool per the protocol) / Page.dispatchWheelEvent.
- PageEx tools: Page.reload / Page.goBack / Page.goForward (frameId via
  Page.getFrameTree) / window.stop() evaluate / Page.getFrameTree /
  content+size evaluate.

Nothing here is faked: every call maps to a Juggler method or to a real
evaluate+dispatch+buffer chain.
"""

from __future__ import annotations

import asyncio
import math
from numbers import Integral, Real
from typing import Any

import orjson

from kahin._mcp import mcp
from kahin.tools._common import (
    _DW,
    _RO,
    _RW,
    _healer_ref,
    _mirage_call,
    _mirage_eval_result,
    _mirage_evaluate,
    _mirage_engine,
    _require_mirage,
)


_MAX_SELECTOR_LENGTH = 16_384
_MAX_ATTRIBUTE_LENGTH = 1_024
_MAX_INPUT_TEXT_LENGTH = 16_384
_MAX_KEY_TEXT_LENGTH = 4_096
_MAX_QUERY_LIMIT = 1_000
_MAX_WAIT_TIMEOUT = 120.0
_MAX_COORDINATE = 100_000.0
_MAX_WHEEL_DELTA = 100_000.0
_MAX_RETURNED_TEXT = 100_000


def _json_error(tool: str, message: str, code: str = "tool_error", **details: Any) -> str:
    payload: dict[str, Any] = {"error": message, "code": code, "tool": tool}
    payload.update(details)
    return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()


def _text_arg(
    value: Any, *, tool: str, field: str, maximum: int, allow_none: bool = False,
) -> tuple[str | None, str | None]:
    if value is None and allow_none:
        return None, None
    if not isinstance(value, str):
        return None, _json_error(tool, f"{field} must be a string", "invalid_argument", field=field)
    if len(value) > maximum:
        return None, _json_error(
            tool,
            f"{field} exceeds the maximum length of {maximum}",
            "argument_too_large",
            field=field,
            maximum=maximum,
            received=len(value),
        )
    return value, None


def _bounded_float(value: Any, *, minimum: float, maximum: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number):
        return default
    return max(minimum, min(maximum, number))


def _bounded_int(value: Any, *, minimum: int, maximum: int, default: int) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number):
        return default
    return int(max(minimum, min(maximum, number)))


def _strict_float(
    value: Any,
    *,
    tool: str,
    field: str,
    minimum: float,
    maximum: float,
) -> tuple[float, str | None]:
    """Validate a browser coordinate/delta; never turn bad input into a click."""
    if isinstance(value, bool) or not isinstance(value, Real):
        return 0.0, _json_error(tool, f"{field} must be a finite number", "invalid_argument", field=field)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0, _json_error(tool, f"{field} must be a finite number", "invalid_argument", field=field)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return 0.0, _json_error(
            tool,
            f"{field} must be between {minimum:g} and {maximum:g}",
            "invalid_argument",
            field=field,
        )
    return number, None


def _strict_int(
    value: Any,
    *,
    tool: str,
    field: str,
    minimum: int,
    maximum: int,
) -> tuple[int, str | None]:
    if isinstance(value, bool) or not isinstance(value, Integral):
        return 0, _json_error(tool, f"{field} must be an integer", "invalid_argument", field=field)
    number = int(value)
    if not minimum <= number <= maximum:
        return 0, _json_error(
            tool,
            f"{field} must be between {minimum} and {maximum}",
            "invalid_argument",
            field=field,
        )
    return number, None


_KEY_DEFAULTS: dict[str, tuple[str, int]] = {
    "Enter": ("Enter", 13),
    "Tab": ("Tab", 9),
    "Escape": ("Escape", 27),
    "Backspace": ("Backspace", 8),
    "Delete": ("Delete", 46),
    "ArrowUp": ("ArrowUp", 38),
    "ArrowDown": ("ArrowDown", 40),
    "ArrowLeft": ("ArrowLeft", 37),
    "ArrowRight": ("ArrowRight", 39),
    "Home": ("Home", 36),
    "End": ("End", 35),
    "PageUp": ("PageUp", 33),
    "PageDown": ("PageDown", 34),
    "Space": ("Space", 32),
    " ": ("Space", 32),
}


def _key_defaults(key: str) -> tuple[str, int]:
    """Return browser-native code/keyCode for common keys.

    Juggler accepts the fields but does not infer them from ``key``. Sending
    the old ``Unidentified``/0 defaults made Enter and arrows look like a
    key event while failing to activate the page's real handlers.
    """
    if key in _KEY_DEFAULTS:
        return _KEY_DEFAULTS[key]
    if len(key) == 1:
        upper = key.upper()
        if "A" <= upper <= "Z":
            return f"Key{upper}", ord(upper)
        if "0" <= key <= "9":
            return f"Digit{key}", ord(key)
    return "Unidentified", 0


def _is_error_response(raw: str) -> bool:
    try:
        payload = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return True
    return isinstance(payload, dict) and bool(payload.get("error"))


def _ensure_json_response(raw: Any, tool: str) -> str:
    if not isinstance(raw, str):
        try:
            return orjson.dumps(raw, option=orjson.OPT_INDENT_2).decode()
        except (TypeError, ValueError):
            return _json_error(tool, "Browser returned an invalid response", "invalid_engine_response")
    try:
        orjson.loads(raw)
    except orjson.JSONDecodeError:
        return _json_error(tool, raw[:1_000], "tool_failed", raw_response=True)
    return raw


def _evaluate_value(result: dict[str, Any]) -> tuple[Any, bool]:
    inner = result.get("result")
    if not isinstance(inner, dict):
        return None, False
    return inner.get("value"), "value" in inner


async def _safe_mirage_call(
    tool: str,
    method: str,
    params: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> str:
    try:
        return _ensure_json_response(await _mirage_call(method, params, session_id=session_id), tool)
    except Exception as exc:  # noqa: BLE001 - public tools must return JSON errors
        return _json_error(tool, f"{method} failed: {exc}", "tool_failed")


async def _safe_mirage_evaluate(
    tool: str,
    expression: str,
    frame_id: str | None = None,
    session_id: str | None = None,
) -> str:
    try:
        return _ensure_json_response(
            await _mirage_evaluate(expression, frame_id, session_id=session_id), tool,
        )
    except Exception as exc:  # noqa: BLE001 - public tools must return JSON errors
        return _json_error(tool, f"JavaScript evaluation failed: {exc}", "tool_failed")


async def _safe_mirage_eval_result(
    tool: str,
    expression: str,
    frame_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any] | str:
    try:
        result = await _mirage_eval_result(expression, frame_id, session_id=session_id)
    except Exception as exc:  # noqa: BLE001 - public tools must return JSON errors
        return _json_error(tool, f"JavaScript evaluation failed: {exc}", "tool_failed")
    if isinstance(result, str):
        return _ensure_json_response(result, tool)
    if not isinstance(result, dict):
        return _json_error(tool, "Browser returned an invalid evaluate response", "invalid_engine_response")
    if "error" in result:
        return _ensure_json_response(result, tool)
    return result


async def _capture_page_session(tool: str) -> tuple[str | None, str | None]:
    """Capture a live page session before a multi-step action.

    Returning the session alongside the first page lookup lets every
    evaluate/input call in the action use the same target even if another
    request switches the selected tab concurrently.
    """
    err = await _require_mirage()
    if err:
        return None, _ensure_json_response(err, tool)
    try:
        page = await _mirage_engine().ensure_page()
    except Exception as exc:  # noqa: BLE001 - public tool returns JSON
        return None, _json_error(tool, f"could not select a live page: {exc}", "session_unavailable")
    session_id = page.get("sessionId") if isinstance(page, dict) else None
    if not isinstance(session_id, str) or not session_id:
        return None, _json_error(tool, "selected page has no live session", "session_unavailable")
    return session_id, None


def _q(selector: str) -> str:
    """Python string -> JS string literal (quotes/unicode safe)."""
    return orjson.dumps(selector).decode()


# --- shared dispatch helpers ----------------------------------------------


async def _dispatch_mouse(
    kind: str, x: float, y: float, *, button: int = 0, modifiers: int = 0,
    click_count: int = 1, buttons: int = 0, session_id: str | None = None,
) -> str:
    """Page.dispatchMouseEvent with the full required param set."""
    x = _bounded_float(x, minimum=0.0, maximum=_MAX_COORDINATE, default=0.0)
    y = _bounded_float(y, minimum=0.0, maximum=_MAX_COORDINATE, default=0.0)
    button = _bounded_int(button, minimum=0, maximum=2, default=0)
    modifiers = _bounded_int(modifiers, minimum=0, maximum=255, default=0)
    click_count = _bounded_int(click_count, minimum=1, maximum=10, default=1)
    buttons = _bounded_int(buttons, minimum=0, maximum=7, default=0)
    params: dict[str, Any] = {
        "type": kind, "button": button, "x": x, "y": y,
        "modifiers": modifiers, "clickCount": click_count, "buttons": buttons,
    }
    return await _safe_mirage_call(
        "kahin_mirage_input", "Page.dispatchMouseEvent", params, session_id=session_id,
    )


async def _element_point(
    selector: str,
    frame_id: str | None = None,
    session_id: str | None = None,
) -> tuple[float, float] | str:
    """Center point of the element's bounding rect via Runtime.evaluate.
    With frame_id the rect is measured in that frame's world and offset by
    the hosting <iframe>'s position (window.frameElement), so the returned
    coordinates are always parent-viewport absolute — same as the main frame.
    Returns ("error", msg) tuple marker or coordinates."""
    expr = (
        "(() => {"
        f"  const el = document.querySelector({_q(selector)});"
        '  if (!el) return {"error": "not found", "code": "element_not_found"};'
        '  el.scrollIntoView({block: "center", inline: "center"});'
        "  const r = el.getBoundingClientRect();"
        "  const style = getComputedStyle(el);"
        '  if (r.width <= 0 || r.height <= 0 || style.display === "none" || style.visibility === "hidden" || style.pointerEvents === "none") return {"error": "element is not actionable", "code": "element_not_actionable", "reason": "not_visible"};'
        '  if (el.disabled === true || el.getAttribute("aria-disabled") === "true") return {"error": "element is disabled", "code": "element_not_actionable", "reason": "disabled"};'
        "  const localX = r.left + r.width / 2;"
        "  const localY = r.top + r.height / 2;"
        "  const hit = document.elementFromPoint(localX, localY);"
        '  if (hit && hit !== el && !el.contains(hit)) return {"error": "element is obscured", "code": "element_not_actionable", "reason": "obscured", "hit": String(hit.tagName || "").toLowerCase()};'
        "  let x = localX, y = localY, owner = window;"
        "  for (let depth = 0; depth < 20 && owner.frameElement; depth++) {"
        "    const fr = owner.frameElement.getBoundingClientRect();"
        "    x += fr.left; y += fr.top; owner = owner.parent;"
        "  }"
        "  return {x, y};"
        "})()"
    )
    result = await _safe_mirage_eval_result(
        "kahin_mirage_click", expr, frame_id, session_id=session_id,
    )
    if isinstance(result, str):
        return result
    if result.get("exceptionDetails"):
        return _json_error(
            "kahin_mirage_click",
            "JavaScript evaluation failed",
            "javascript_error",
            exception=result["exceptionDetails"],
        )
    result_value = result.get("result") or {}
    point = result_value.get("value") if isinstance(result_value, dict) else None
    if not isinstance(point, dict):
        return _json_error("kahin_mirage_click", "element not found", "element_not_found")
    if "error" in point:
        return _json_error(
            "kahin_mirage_click",
            str(point["error"]),
            str(point.get("code") or "element_not_actionable"),
            selector=selector,
            reason=point.get("reason"),
        )
    try:
        x = float(point["x"])
        y = float(point["y"])
    except (KeyError, TypeError, ValueError):
        return _json_error("kahin_mirage_click", "element returned invalid coordinates", "invalid_dom_result")
    if not math.isfinite(x) or not math.isfinite(y):
        return _json_error("kahin_mirage_click", "element returned non-finite coordinates", "invalid_dom_result")
    return (
        _bounded_float(x, minimum=0.0, maximum=_MAX_COORDINATE, default=0.0),
        _bounded_float(y, minimum=0.0, maximum=_MAX_COORDINATE, default=0.0),
    )


# ============================== DOM (12) ===================================


@mcp.tool(name="kahin_mirage_query", annotations=_RO)
async def mirage_query(selector: str, frame_id: str | None = None) -> str:
    """Mirage: info about the first element matching a CSS selector (tag, id,
    class, text, visibility, rect) via Runtime.evaluate. frame_id: target an
    iframe (kahin_mirage_frame_tree); main frame is the default."""
    selector_value, error = _text_arg(
        selector, tool="kahin_mirage_query", field="selector", maximum=_MAX_SELECTOR_LENGTH,
    )
    if error:
        return error
    if not selector_value:
        return _json_error("kahin_mirage_query", "selector must not be empty", "invalid_argument", field="selector")
    async with _healer_ref.safe("kahin_mirage_query", selector=selector_value[:80]):
        expr = (
            "(() => {"
            f"  const el = document.querySelector({_q(selector_value)});"
            "  if (!el) return null;"
            "  const r = el.getBoundingClientRect();"
            "  return {"
            "    tag: el.tagName.toLowerCase(),"
            "    id: el.id || null,"
            f"    className: (typeof el.className === 'string' ? el.className.slice(0, {_MAX_RETURNED_TEXT}) : ''),"
            "    text: (el.textContent || '').trim().slice(0, 500),"
            "    visible: r.width > 0 && r.height > 0,"
            "    rect: {x: r.x, y: r.y, width: r.width, height: r.height}"
            "  };"
            "})()"
        )
        return await _safe_mirage_evaluate("kahin_mirage_query", expr, frame_id)


@mcp.tool(name="kahin_mirage_query_all", annotations=_RO)
async def mirage_query_all(selector: str, limit: int = 100, frame_id: str | None = None) -> str:
    """Mirage: list matching elements (tag, id, text, visibility), capped.
    frame_id: target an iframe (kahin_mirage_frame_tree); main frame default."""
    selector_value, error = _text_arg(
        selector, tool="kahin_mirage_query_all", field="selector", maximum=_MAX_SELECTOR_LENGTH,
    )
    if error:
        return error
    if not selector_value:
        return _json_error("kahin_mirage_query_all", "selector must not be empty", "invalid_argument", field="selector")
    limit_value = _bounded_int(limit, minimum=0, maximum=_MAX_QUERY_LIMIT, default=100)
    async with _healer_ref.safe(
        "kahin_mirage_query_all", selector=selector_value[:80], limit=limit_value,
    ):
        expr = (
            "(() => {"
            f"  const els = [...document.querySelectorAll({_q(selector_value)})].slice(0, {limit_value});"
            "  return els.map(el => {"
            "    const r = el.getBoundingClientRect();"
            "    return {"
            "      tag: el.tagName.toLowerCase(),"
            "      id: el.id || null,"
            "      text: (el.textContent || '').trim().slice(0, 200),"
            "      visible: r.width > 0 && r.height > 0"
            "    };"
            "  });"
            "})()"
        )
        return await _safe_mirage_evaluate("kahin_mirage_query_all", expr, frame_id)


@mcp.tool(name="kahin_mirage_click", annotations=_DW)
async def mirage_click(selector: str, frame_id: str | None = None) -> str:
    """Mirage: click an element by CSS selector — real mouse events
    (Page.dispatchMouseEvent mousedown+mouseup at the element center).
    frame_id: target an iframe (kahin_mirage_frame_tree); main frame default."""
    selector_value, error = _text_arg(
        selector, tool="kahin_mirage_click", field="selector", maximum=_MAX_SELECTOR_LENGTH,
    )
    if error:
        return error
    if not selector_value:
        return _json_error("kahin_mirage_click", "selector must not be empty", "invalid_argument", field="selector")
    async with _healer_ref.safe("kahin_mirage_click", selector=selector_value[:80], frame_id=frame_id):
        session_id, capture_error = await _capture_page_session("kahin_mirage_click")
        if capture_error:
            return capture_error
        assert session_id is not None
        point = await _element_point(selector_value, frame_id, session_id=session_id)
        if not isinstance(point, tuple):
            return point
        x, y = point
        down = await _dispatch_mouse("mousedown", x, y, button=0, buttons=1, session_id=session_id)
        if _is_error_response(down):
            return down
        up = await _dispatch_mouse("mouseup", x, y, button=0, buttons=0, session_id=session_id)
        if _is_error_response(up):
            return up
        result = {"clicked": selector_value, "x": x, "y": y, "mousedown": down, "mouseup": up}
        return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_type", annotations=_RW)
async def mirage_type(selector: str, text: str, frame_id: str | None = None) -> str:
    """Mirage: focus an element and type text via Page.insertText (Juggler's
    real text-insertion method). frame_id: target an iframe
    (kahin_mirage_frame_tree); main frame default."""
    selector_value, error = _text_arg(
        selector, tool="kahin_mirage_type", field="selector", maximum=_MAX_SELECTOR_LENGTH,
    )
    if error:
        return error
    if not selector_value:
        return _json_error("kahin_mirage_type", "selector must not be empty", "invalid_argument", field="selector")
    text_value, error = _text_arg(
        text, tool="kahin_mirage_type", field="text", maximum=_MAX_INPUT_TEXT_LENGTH,
    )
    if error:
        return error
    async with _healer_ref.safe(
        "kahin_mirage_type", selector=selector_value[:80], text_length=len(text_value or ""), frame_id=frame_id,
    ):
        session_id, capture_error = await _capture_page_session("kahin_mirage_type")
        if capture_error:
            return capture_error
        assert session_id is not None
        expr = (
            "(() => {"
            f"  const el = document.querySelector({_q(selector_value)});"
            '  if (!el) return {"error": "not found", "code": "element_not_found"};'
            "  const tag = String(el.tagName || '').toLowerCase();"
            "  const inputType = String(el.type || '').toLowerCase();"
            "  const editable = el.isContentEditable || tag === 'textarea' || (tag === 'input' && !['button','checkbox','file','hidden','image','radio','range','reset','submit'].includes(inputType));"
            '  if (!editable) return {"error": "element is not editable", "code": "not_editable"};'
            '  if (el.disabled || el.readOnly || el.getAttribute("aria-disabled") === "true") return {"error": "element is disabled or read-only", "code": "not_editable"};'
            "  const before = el.isContentEditable ? String(el.textContent || '') : String(el.value || '');"
            "  el.focus();"
            "  return {ready: document.activeElement === el, beforeLength: before.length, beforePrefix: before.slice(0, 4096), password: inputType === 'password'};"
            "})()"
        )
        result = await _safe_mirage_eval_result(
            "kahin_mirage_type", expr, frame_id, session_id=session_id,
        )
        if isinstance(result, str):
            return result
        if result.get("exceptionDetails"):
            return _json_error(
                "kahin_mirage_type",
                "JavaScript evaluation failed",
                "javascript_error",
                exception=result["exceptionDetails"],
            )
        focused, has_value = _evaluate_value(result)
        if not has_value:
            return _json_error("kahin_mirage_type", "Browser returned an invalid focus result", "invalid_dom_result")
        if isinstance(focused, dict) and focused.get("error"):
            return _json_error(
                "kahin_mirage_type", str(focused["error"]), str(focused.get("code") or "element_not_found"),
                selector=selector_value,
            )
        if not isinstance(focused, dict) or focused.get("ready") is not True:
            return _json_error("kahin_mirage_type", "element could not be focused", "element_not_actionable")
        native = await _safe_mirage_call(
            "kahin_mirage_type", "Page.insertText", {"text": text_value}, session_id=session_id,
        )
        if _is_error_response(native):
            return native
        native_value = orjson.loads(native)
        verify = await _safe_mirage_eval_result(
            "kahin_mirage_type",
            "(() => { const el = document.querySelector(" + _q(selector_value) + "); if (!el) return {error: 'not found'}; const value = el.isContentEditable ? String(el.textContent || '') : String(el.value || ''); const beforeLength = " + str(focused.get("beforeLength", 0)) + "; const beforePrefix = " + _q(str(focused.get("beforePrefix", ""))) + "; return {afterLength: value.length, changed: value.length !== beforeLength || value.slice(0, 4096) !== beforePrefix}; })()",
            frame_id,
            session_id=session_id,
        )
        if isinstance(verify, str):
            return verify
        verified, verified_value = _evaluate_value(verify)
        if not verified_value or not isinstance(verified, dict):
            return _json_error("kahin_mirage_type", "could not verify inserted text", "invalid_dom_result")
        if text_value and verified.get("changed") is not True:
            return _json_error(
                "kahin_mirage_type", "browser did not change the editable element", "input_not_changed",
                selector=selector_value,
            )
        return orjson.dumps(
            {
                "typed": len(text_value),
                "selector": selector_value,
                "changed": bool(verified.get("changed")),
                "valueLength": verified.get("afterLength"),
                "result": native_value,
            },
            option=orjson.OPT_INDENT_2,
        ).decode()


@mcp.tool(name="kahin_mirage_get_text", annotations=_RO)
async def mirage_get_text(selector: str, frame_id: str | None = None) -> str:
    """Mirage: trimmed textContent of the first matching element.
    frame_id: target an iframe (kahin_mirage_frame_tree); main frame default."""
    selector_value, error = _text_arg(
        selector, tool="kahin_mirage_get_text", field="selector", maximum=_MAX_SELECTOR_LENGTH,
    )
    if error:
        return error
    if not selector_value:
        return _json_error("kahin_mirage_get_text", "selector must not be empty", "invalid_argument", field="selector")
    async with _healer_ref.safe("kahin_mirage_get_text", selector=selector_value[:80], frame_id=frame_id):
        expr = (
            "(() => {"
            f"  const el = document.querySelector({_q(selector_value)});"
            f"  return el ? (el.textContent || '').trim().slice(0, {_MAX_RETURNED_TEXT}) : null;"
            "})()"
        )
        return await _safe_mirage_evaluate("kahin_mirage_get_text", expr, frame_id)


@mcp.tool(name="kahin_mirage_get_attribute", annotations=_RO)
async def mirage_get_attribute(selector: str, name: str, frame_id: str | None = None) -> str:
    """Mirage: attribute value of the first matching element.
    frame_id: target an iframe (kahin_mirage_frame_tree); main frame default."""
    selector_value, error = _text_arg(
        selector, tool="kahin_mirage_get_attribute", field="selector", maximum=_MAX_SELECTOR_LENGTH,
    )
    if error:
        return error
    name_value, error = _text_arg(
        name, tool="kahin_mirage_get_attribute", field="name", maximum=_MAX_ATTRIBUTE_LENGTH,
    )
    if error:
        return error
    if not selector_value or not name_value:
        return _json_error("kahin_mirage_get_attribute", "selector and name must not be empty", "invalid_argument")
    async with _healer_ref.safe(
        "kahin_mirage_get_attribute", selector=selector_value[:80], name=name_value[:80], frame_id=frame_id,
    ):
        expr = (
            "(() => {"
            f"  const el = document.querySelector({_q(selector_value)});"
            "  if (!el) return null;"
            f"  const name = {_q(name_value)};"
            "  const type = String(el.getAttribute('type') || el.type || '').toLowerCase();"
            "  if (type === 'password' && name.toLowerCase() === 'value') return '[redacted]';"
            f"  const value = el.getAttribute(name); return value == null ? null : String(value).slice(0, {_MAX_RETURNED_TEXT});"
            "})()"
        )
        return await _safe_mirage_evaluate("kahin_mirage_get_attribute", expr, frame_id)


@mcp.tool(name="kahin_mirage_set_attribute", annotations=_RW)
async def mirage_set_attribute(selector: str, name: str, value: str, frame_id: str | None = None) -> str:
    """Mirage: set an attribute on the first matching element.
    frame_id: target an iframe (kahin_mirage_frame_tree); main frame default."""
    selector_value, error = _text_arg(
        selector, tool="kahin_mirage_set_attribute", field="selector", maximum=_MAX_SELECTOR_LENGTH,
    )
    if error:
        return error
    name_value, error = _text_arg(
        name, tool="kahin_mirage_set_attribute", field="name", maximum=_MAX_ATTRIBUTE_LENGTH,
    )
    if error:
        return error
    value_value, error = _text_arg(
        value, tool="kahin_mirage_set_attribute", field="value", maximum=_MAX_INPUT_TEXT_LENGTH,
    )
    if error:
        return error
    if not selector_value or not name_value:
        return _json_error("kahin_mirage_set_attribute", "selector and name must not be empty", "invalid_argument")
    async with _healer_ref.safe(
        "kahin_mirage_set_attribute",
        selector=selector_value[:80], name=name_value[:80], value_length=len(value_value or ""), frame_id=frame_id,
    ):
        expr = (
            "(() => {"
            f"  const el = document.querySelector({_q(selector_value)});"
            '  if (!el) return {"error": "not found"};'
            f"  el.setAttribute({_q(name_value)}, {_q(value_value or '')});"
            '  return "set";'
            "})()"
        )
        return await _safe_mirage_evaluate("kahin_mirage_set_attribute", expr, frame_id)


@mcp.tool(name="kahin_mirage_focus", annotations=_RW)
async def mirage_focus(selector: str, frame_id: str | None = None) -> str:
    """Mirage: focus the first matching element.
    frame_id: target an iframe (kahin_mirage_frame_tree); main frame default."""
    selector_value, error = _text_arg(
        selector, tool="kahin_mirage_focus", field="selector", maximum=_MAX_SELECTOR_LENGTH,
    )
    if error:
        return error
    if not selector_value:
        return _json_error("kahin_mirage_focus", "selector must not be empty", "invalid_argument", field="selector")
    async with _healer_ref.safe("kahin_mirage_focus", selector=selector_value[:80], frame_id=frame_id):
        expr = (
            "(() => {"
            f"  const el = document.querySelector({_q(selector_value)});"
            '  if (!el) return {"error": "not found"};'
            "  el.focus();"
            '  return "focused";'
            "})()"
        )
        return await _safe_mirage_evaluate("kahin_mirage_focus", expr, frame_id)


@mcp.tool(name="kahin_mirage_hover", annotations=_RW)
async def mirage_hover(selector: str, frame_id: str | None = None) -> str:
    """Mirage: move the mouse over the element center (dispatchMouseEvent
    mousemove). frame_id: target an iframe (kahin_mirage_frame_tree); main
    frame default."""
    selector_value, error = _text_arg(
        selector, tool="kahin_mirage_hover", field="selector", maximum=_MAX_SELECTOR_LENGTH,
    )
    if error:
        return error
    if not selector_value:
        return _json_error("kahin_mirage_hover", "selector must not be empty", "invalid_argument", field="selector")
    async with _healer_ref.safe("kahin_mirage_hover", selector=selector_value[:80], frame_id=frame_id):
        session_id, capture_error = await _capture_page_session("kahin_mirage_hover")
        if capture_error:
            return capture_error
        assert session_id is not None
        point = await _element_point(selector_value, frame_id, session_id=session_id)
        if not isinstance(point, tuple):
            return point
        x, y = point
        move = await _dispatch_mouse("mousemove", x, y, button=0, buttons=0, session_id=session_id)
        if _is_error_response(move):
            return move
        result = {"hovered": selector_value, "x": x, "y": y, "mousemove": move}
        return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_get_html", annotations=_RO)
async def mirage_get_html(selector: str | None = None, frame_id: str | None = None) -> str:
    """Mirage: outerHTML of the first matching element, or the whole document.
    frame_id: target an iframe (kahin_mirage_frame_tree); main frame default."""
    selector_value, error = _text_arg(
        selector,
        tool="kahin_mirage_get_html",
        field="selector",
        maximum=_MAX_SELECTOR_LENGTH,
        allow_none=True,
    )
    if error:
        return error
    async with _healer_ref.safe(
        "kahin_mirage_get_html", selector=selector_value or "", frame_id=frame_id,
    ):
        if selector_value:
            expr = (
                "(() => {"
                f"  const el = document.querySelector({_q(selector_value)});"
                "  if (!el) return null;"
                "  const copy = el.cloneNode(true);"
                "  if (String(copy.getAttribute('type') || '').toLowerCase() === 'password') copy.removeAttribute('value');"
                "  copy.querySelectorAll('input').forEach(input => {"
                "    if (String(input.getAttribute('type') || '').toLowerCase() === 'password') input.removeAttribute('value');"
                "  });"
                f"  return copy.outerHTML.slice(0, {_MAX_RETURNED_TEXT});"
                "})()"
            )
        else:
            expr = (
                "(() => {"
                "  const copy = document.documentElement.cloneNode(true);"
                "  copy.querySelectorAll('input').forEach(input => {"
                "    if (String(input.getAttribute('type') || '').toLowerCase() === 'password') input.removeAttribute('value');"
                "  });"
                f"  return copy.outerHTML.slice(0, {_MAX_RETURNED_TEXT});"
                "})()"
            )
        return await _safe_mirage_evaluate("kahin_mirage_get_html", expr, frame_id)


@mcp.tool(name="kahin_mirage_wait_selector", annotations=_RO)
async def mirage_wait_selector(selector: str, timeout: float = 10.0, frame_id: str | None = None) -> str:
    """Mirage: poll Runtime.evaluate until the selector matches (or timeout).
    frame_id: target an iframe (kahin_mirage_frame_tree); main frame default."""
    selector_value, error = _text_arg(
        selector, tool="kahin_mirage_wait_selector", field="selector", maximum=_MAX_SELECTOR_LENGTH,
    )
    if error:
        return error
    if not selector_value:
        return _json_error("kahin_mirage_wait_selector", "selector must not be empty", "invalid_argument", field="selector")
    timeout_value = _bounded_float(
        timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=10.0,
    )
    async with _healer_ref.safe(
        "kahin_mirage_wait_selector", selector=selector_value[:80], timeout=timeout_value, frame_id=frame_id,
    ):
        expr = f"!!document.querySelector({_q(selector_value)})"
        deadline = asyncio.get_running_loop().time() + timeout_value
        while True:
            try:
                result = await asyncio.wait_for(
                    _safe_mirage_eval_result("kahin_mirage_wait_selector", expr, frame_id),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                return _json_error(
                    "kahin_mirage_wait_selector", "Selector evaluation timed out", "engine_timeout",
                )
            if isinstance(result, str):
                return result
            if result.get("exceptionDetails"):
                return _json_error(
                    "kahin_mirage_wait_selector",
                    "JavaScript evaluation failed",
                    "javascript_error",
                    exception=result["exceptionDetails"],
                )
            value, has_value = _evaluate_value(result)
            if not has_value:
                return _json_error(
                    "kahin_mirage_wait_selector",
                    "Browser returned an invalid selector result",
                    "invalid_dom_result",
                )
            if value is True:
                return orjson.dumps({"found": True, "selector": selector_value, "frame_id": frame_id}).decode()
            if asyncio.get_running_loop().time() >= deadline:
                return orjson.dumps({
                    "found": False,
                    "selector": selector_value,
                    "frame_id": frame_id,
                    "timeout": timeout_value,
                }).decode()
            await asyncio.sleep(0.25)


@mcp.tool(name="kahin_mirage_get_value", annotations=_RO)
async def mirage_get_value(selector: str, frame_id: str | None = None) -> str:
    """Mirage: value of an input/select/textarea (or null when missing).
    frame_id: target an iframe (kahin_mirage_frame_tree); main frame default."""
    selector_value, error = _text_arg(
        selector, tool="kahin_mirage_get_value", field="selector", maximum=_MAX_SELECTOR_LENGTH,
    )
    if error:
        return error
    if not selector_value:
        return _json_error("kahin_mirage_get_value", "selector must not be empty", "invalid_argument", field="selector")
    async with _healer_ref.safe("kahin_mirage_get_value", selector=selector_value[:80], frame_id=frame_id):
        expr = (
            "(() => {"
            f"  const el = document.querySelector({_q(selector_value)});"
            "  if (!el) return null;"
            "  const type = String(el.getAttribute('type') || el.type || '').toLowerCase();"
            "  if (type === 'password') return '[redacted]';"
            f"  const value = el.value; return value == null ? null : String(value).slice(0, {_MAX_RETURNED_TEXT});"
            "})()"
        )
        return await _safe_mirage_evaluate("kahin_mirage_get_value", expr, frame_id)


# ============================= Input (7) ===================================


@mcp.tool(name="kahin_mirage_mouse_click", annotations=_DW)
async def mirage_mouse_click(x: float, y: float, button: int = 0) -> str:
    """Mirage: raw click at viewport coordinates (mousedown+mouseup)."""
    tool = "kahin_mirage_mouse_click"
    x_value, error = _strict_float(x, tool=tool, field="x", minimum=0.0, maximum=_MAX_COORDINATE)
    if error:
        return error
    y_value, error = _strict_float(y, tool=tool, field="y", minimum=0.0, maximum=_MAX_COORDINATE)
    if error:
        return error
    button_value, error = _strict_int(button, tool=tool, field="button", minimum=0, maximum=2)
    if error:
        return error
    session_id, capture_error = await _capture_page_session(tool)
    if capture_error:
        return capture_error
    assert session_id is not None
    async with _healer_ref.safe(
        tool, x=x_value, y=y_value, button=button_value,
    ):
        down = await _dispatch_mouse("mousedown", x_value, y_value, button=button_value, buttons=1, session_id=session_id)
        if _is_error_response(down):
            return down
        up = await _dispatch_mouse("mouseup", x_value, y_value, button=button_value, buttons=0, session_id=session_id)
        if _is_error_response(up):
            return up
        return orjson.dumps({
            "x": x_value, "y": y_value, "button": button_value, "mousedown": down, "mouseup": up,
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_mouse_move", annotations=_RW)
async def mirage_mouse_move(x: float, y: float) -> str:
    """Mirage: move the mouse to viewport coordinates (mousemove)."""
    tool = "kahin_mirage_mouse_move"
    x_value, error = _strict_float(x, tool=tool, field="x", minimum=0.0, maximum=_MAX_COORDINATE)
    if error:
        return error
    y_value, error = _strict_float(y, tool=tool, field="y", minimum=0.0, maximum=_MAX_COORDINATE)
    if error:
        return error
    session_id, capture_error = await _capture_page_session(tool)
    if capture_error:
        return capture_error
    assert session_id is not None
    async with _healer_ref.safe(tool, x=x_value, y=y_value):
        return await _dispatch_mouse("mousemove", x_value, y_value, buttons=0, session_id=session_id)


@mcp.tool(name="kahin_mirage_mouse_down", annotations=_RW)
async def mirage_mouse_down(x: float, y: float, button: int = 0) -> str:
    """Mirage: press a mouse button (mousedown)."""
    tool = "kahin_mirage_mouse_down"
    x_value, error = _strict_float(x, tool=tool, field="x", minimum=0.0, maximum=_MAX_COORDINATE)
    if error:
        return error
    y_value, error = _strict_float(y, tool=tool, field="y", minimum=0.0, maximum=_MAX_COORDINATE)
    if error:
        return error
    button_value, error = _strict_int(button, tool=tool, field="button", minimum=0, maximum=2)
    if error:
        return error
    session_id, capture_error = await _capture_page_session(tool)
    if capture_error:
        return capture_error
    assert session_id is not None
    async with _healer_ref.safe(tool, x=x_value, y=y_value, button=button_value):
        return await _dispatch_mouse("mousedown", x_value, y_value, button=button_value, buttons=1, session_id=session_id)


@mcp.tool(name="kahin_mirage_mouse_up", annotations=_RW)
async def mirage_mouse_up(x: float, y: float, button: int = 0) -> str:
    """Mirage: release a mouse button (mouseup)."""
    tool = "kahin_mirage_mouse_up"
    x_value, error = _strict_float(x, tool=tool, field="x", minimum=0.0, maximum=_MAX_COORDINATE)
    if error:
        return error
    y_value, error = _strict_float(y, tool=tool, field="y", minimum=0.0, maximum=_MAX_COORDINATE)
    if error:
        return error
    button_value, error = _strict_int(button, tool=tool, field="button", minimum=0, maximum=2)
    if error:
        return error
    session_id, capture_error = await _capture_page_session(tool)
    if capture_error:
        return capture_error
    assert session_id is not None
    async with _healer_ref.safe(tool, x=x_value, y=y_value, button=button_value):
        return await _dispatch_mouse("mouseup", x_value, y_value, button=button_value, buttons=0, session_id=session_id)


@mcp.tool(name="kahin_mirage_key_press", annotations=_RW)
async def mirage_key_press(
    key: str, code: str = "Unidentified", key_code: int = 0, text: str | None = None,
) -> str:
    """Mirage: press one key (dispatchKeyEvent keyDown+keyUp; repeat is the
    REQUIRED protocol bool and is always False here)."""
    key_value, error = _text_arg(
        key, tool="kahin_mirage_key_press", field="key", maximum=128,
    )
    if error:
        return error
    code_value, error = _text_arg(
        code, tool="kahin_mirage_key_press", field="code", maximum=128,
    )
    if error:
        return error
    text_value, error = _text_arg(
        text, tool="kahin_mirage_key_press", field="text", maximum=_MAX_INPUT_TEXT_LENGTH, allow_none=True,
    )
    if error:
        return error
    default_code, default_key_code = _key_defaults(key_value)
    key_code_value, error = _strict_int(
        key_code,
        tool="kahin_mirage_key_press",
        field="key_code",
        minimum=0,
        maximum=65_535,
    )
    if error:
        return error
    # The public default is ``key_code=0``.  _bounded_int intentionally keeps
    # an explicitly supplied zero, so comparing against the inferred default
    # would leave Enter/arrows as Unidentified (13 != 0, etc.).  Infer both
    # fields only when the caller left the protocol defaults untouched; a
    # caller-provided code/keyCode pair remains authoritative.
    if code_value == "Unidentified" and key_code == 0:
        code_value = default_code
        key_code_value = default_key_code
    async with _healer_ref.safe(
        "kahin_mirage_key_press",
        key=key_value[:40], code=code_value[:40], key_code=key_code_value,
        text_length=len(text_value or ""),
    ):
        session_id, capture_error = await _capture_page_session("kahin_mirage_key_press")
        if capture_error:
            return capture_error
        assert session_id is not None
        base: dict[str, Any] = {
            "type": "keyDown", "key": key_value, "keyCode": key_code_value,
            "location": 0, "code": code_value, "repeat": False,
        }
        if text_value is not None:
            base["text"] = text_value
        down = await _safe_mirage_call(
            "kahin_mirage_key_press", "Page.dispatchKeyEvent", base, session_id=session_id,
        )
        if _is_error_response(down):
            return down
        up = await _safe_mirage_call("kahin_mirage_key_press", "Page.dispatchKeyEvent", {
            "type": "keyUp", "key": key_value, "keyCode": key_code_value,
            "location": 0, "code": code_value, "repeat": False,
        }, session_id=session_id)
        if _is_error_response(up):
            return up
        return orjson.dumps({"key": key_value, "keyDown": down, "keyUp": up}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_key_text", annotations=_RW)
async def mirage_key_text(text: str) -> str:
    """Mirage: type text one character at a time via dispatchKeyEvent
    (keyDown with text + keyUp per char, repeat=False)."""
    text_value, error = _text_arg(
        text, tool="kahin_mirage_key_text", field="text", maximum=_MAX_KEY_TEXT_LENGTH,
    )
    if error:
        return error
    async with _healer_ref.safe("kahin_mirage_key_text", text_length=len(text_value or "")):
        session_id, capture_error = await _capture_page_session("kahin_mirage_key_text")
        if capture_error:
            return capture_error
        assert session_id is not None
        results: list[dict[str, Any]] = []
        for ch in text_value:
            code, key_code = _key_defaults(ch)
            key_code = _bounded_int(key_code, minimum=0, maximum=65_535, default=0)
            down = await _safe_mirage_call("kahin_mirage_key_text", "Page.dispatchKeyEvent", {
                "type": "keyDown", "key": ch, "keyCode": key_code,
                "location": 0, "code": code, "repeat": False, "text": ch,
            }, session_id=session_id)
            if _is_error_response(down):
                return down
            up = await _safe_mirage_call("kahin_mirage_key_text", "Page.dispatchKeyEvent", {
                "type": "keyUp", "key": ch, "keyCode": key_code,
                "location": 0, "code": code, "repeat": False,
            }, session_id=session_id)
            if _is_error_response(up):
                return up
            results.append({"char": ch, "keyDown": down, "keyUp": up})
        return orjson.dumps({"typed": len(text_value), "chars": results}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_scroll", annotations=_RW)
async def mirage_scroll(delta_x: float = 0.0, delta_y: float = 100.0, x: float = 0.0, y: float = 0.0) -> str:
    """Mirage: scroll with a wheel event (Page.dispatchWheelEvent)."""
    tool = "kahin_mirage_scroll"
    x_value, error = _strict_float(x, tool=tool, field="x", minimum=0.0, maximum=_MAX_COORDINATE)
    if error:
        return error
    y_value, error = _strict_float(y, tool=tool, field="y", minimum=0.0, maximum=_MAX_COORDINATE)
    if error:
        return error
    delta_x_value, error = _strict_float(
        delta_x, tool=tool, field="delta_x", minimum=-_MAX_WHEEL_DELTA, maximum=_MAX_WHEEL_DELTA,
    )
    if error:
        return error
    delta_y_value, error = _strict_float(
        delta_y, tool=tool, field="delta_y", minimum=-_MAX_WHEEL_DELTA, maximum=_MAX_WHEEL_DELTA,
    )
    if error:
        return error
    session_id, capture_error = await _capture_page_session(tool)
    if capture_error:
        return capture_error
    assert session_id is not None
    async with _healer_ref.safe(
        tool, delta_x=delta_x_value, delta_y=delta_y_value,
    ):
        return await _safe_mirage_call(tool, "Page.dispatchWheelEvent", {
            "x": x_value, "y": y_value, "deltaX": delta_x_value,
            "deltaY": delta_y_value, "deltaZ": 0.0, "modifiers": 0,
        }, session_id=session_id)


# ============================= PageEx (6) ==================================


async def _main_frame_id() -> tuple[str | None, str | None, str | None]:
    """Main frame id via Page.getFrameTree (driver-derived, real data)."""
    try:
        err = await _require_mirage()
        if err:
            return None, None, err
        engine = _mirage_engine()
        page = await engine.ensure_page()
        session_id = page.get("sessionId")
        result = await engine.call("Page.getFrameTree", session_id=session_id)
    except Exception as e:  # noqa: BLE001 - PageEx tools must return JSON errors
        return None, None, _json_error("kahin_mirage_frame_tree", f"Page.getFrameTree failed: {e}", "tool_failed")
    if not isinstance(result, dict):
        return None, None, _json_error(
            "kahin_mirage_frame_tree", "Browser returned an invalid frame tree", "invalid_engine_response",
        )
    frame_tree = result.get("frameTree")
    frame = (frame_tree.get("frame") if isinstance(frame_tree, dict) else None) or {}
    frame_id = frame.get("id") if isinstance(frame, dict) else None
    if not isinstance(session_id, str) or not session_id:
        return None, None, _json_error(
            "kahin_mirage_frame_tree", "Browser returned no live page session", "session_unavailable",
        )
    return (frame_id or None), session_id, None


@mcp.tool(name="kahin_mirage_reload", annotations=_RW)
async def mirage_reload() -> str:
    """Mirage: reload the current page through Juggler's no-argument method."""
    async with _healer_ref.safe("kahin_mirage_reload"):
        return await _safe_mirage_call("kahin_mirage_reload", "Page.reload")


@mcp.tool(name="kahin_mirage_go_back", annotations=_RW)
async def mirage_go_back() -> str:
    """Mirage: navigate back in history (Page.goBack with the main frameId)."""
    async with _healer_ref.safe("kahin_mirage_go_back"):
        frame_id, session_id, err = await _main_frame_id()
        if err:
            return err
        if not frame_id:
            return _json_error("kahin_mirage_go_back", "no main frame available", "frame_unavailable")
        return await _safe_mirage_call(
            "kahin_mirage_go_back", "Page.goBack", {"frameId": frame_id}, session_id=session_id,
        )


@mcp.tool(name="kahin_mirage_go_forward", annotations=_RW)
async def mirage_go_forward() -> str:
    """Mirage: navigate forward in history (Page.goForward)."""
    async with _healer_ref.safe("kahin_mirage_go_forward"):
        frame_id, session_id, err = await _main_frame_id()
        if err:
            return err
        if not frame_id:
            return _json_error("kahin_mirage_go_forward", "no main frame available", "frame_unavailable")
        return await _safe_mirage_call(
            "kahin_mirage_go_forward", "Page.goForward", {"frameId": frame_id}, session_id=session_id,
        )


@mcp.tool(name="kahin_mirage_stop", annotations=_RW)
async def mirage_stop() -> str:
    """Mirage: stop page loading (Runtime.evaluate window.stop())."""
    async with _healer_ref.safe("kahin_mirage_stop"):
        result = await _safe_mirage_evaluate("kahin_mirage_stop", "window.stop(); true")
        try:
            parsed = orjson.loads(result)
        except orjson.JSONDecodeError:
            return _json_error("kahin_mirage_stop", "Browser returned an invalid stop response", "invalid_engine_response")
        if isinstance(parsed, dict) and "error" in parsed:
            return result
        if parsed is not True:
            return _json_error("kahin_mirage_stop", "window.stop() did not complete", "invalid_engine_response")
        return '{"stopped": true}'


@mcp.tool(name="kahin_mirage_frame_tree", annotations=_RO)
async def mirage_frame_tree() -> str:
    """Mirage: frame tree of the current page (Page.getFrameTree)."""
    async with _healer_ref.safe("kahin_mirage_frame_tree"):
        return await _safe_mirage_call("kahin_mirage_frame_tree", "Page.getFrameTree")


@mcp.tool(name="kahin_mirage_page_content", annotations=_RO)
async def mirage_page_content() -> str:
    """Mirage: page HTML content + viewport/document sizes in one evaluate."""
    async with _healer_ref.safe("kahin_mirage_page_content"):
        expr = (
            "(() => ({"
            "  html: (() => {"
            "    const copy = document.documentElement.cloneNode(true);"
            "    copy.querySelectorAll('input').forEach(input => {"
            "      if (String(input.getAttribute('type') || '').toLowerCase() === 'password') input.removeAttribute('value');"
            "    });"
            f"    return copy.outerHTML.slice(0, {_MAX_RETURNED_TEXT});"
            "  })(),"
            "  innerWidth: window.innerWidth,"
            "  innerHeight: window.innerHeight,"
            "  scrollWidth: document.documentElement.scrollWidth,"
            "  scrollHeight: document.documentElement.scrollHeight,"
            "  devicePixelRatio: window.devicePixelRatio"
            "}))()"
        )
        return await _safe_mirage_evaluate("kahin_mirage_page_content", expr)
