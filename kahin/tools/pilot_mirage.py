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


def _q(selector: str) -> str:
    """Python string -> JS string literal (quotes/unicode safe)."""
    return orjson.dumps(selector).decode()


# --- shared dispatch helpers ----------------------------------------------


async def _dispatch_mouse(
    kind: str, x: float, y: float, *, button: int = 0, modifiers: int = 0,
    click_count: int = 1, buttons: int = 0,
) -> str:
    """Page.dispatchMouseEvent with the full required param set."""
    params: dict[str, Any] = {
        "type": kind, "button": button, "x": x, "y": y,
        "modifiers": modifiers, "clickCount": click_count, "buttons": buttons,
    }
    return await _mirage_call("Page.dispatchMouseEvent", params)


async def _element_point(selector: str, frame_id: str | None = None) -> tuple[float, float] | str:
    """Center point of the element's bounding rect via Runtime.evaluate.
    With frame_id the rect is measured in that frame's world and offset by
    the hosting <iframe>'s position (window.frameElement), so the returned
    coordinates are always parent-viewport absolute — same as the main frame.
    Returns ("error", msg) tuple marker or coordinates."""
    expr = (
        "(() => {"
        f"  const el = document.querySelector({_q(selector)});"
        '  if (!el) return {"error": "not found"};'
        '  el.scrollIntoView({block: "center", inline: "center"});'
        "  const r = el.getBoundingClientRect();"
        "  const fe = window.frameElement;"
        "  const fo = fe ? fe.getBoundingClientRect() : {x: 0, y: 0};"
        "  return {x: fo.x + r.x + r.width / 2, y: fo.y + r.y + r.height / 2};"
        "})()"
    )
    result = await _mirage_eval_result(expr, frame_id)
    if isinstance(result, str):
        return result
    if result.get("exceptionDetails"):
        return orjson.dumps({"error": "evaluate threw", "exception": result["exceptionDetails"]}).decode()
    point = (result.get("result") or {}).get("value")
    if not isinstance(point, dict):
        return '{"error": "element not found"}'
    if "error" in point:
        return orjson.dumps({"error": point["error"], "selector": selector}).decode()
    return float(point["x"]), float(point["y"])


# ============================== DOM (12) ===================================


@mcp.tool(name="kahin_mirage_query", annotations=_RO)
async def mirage_query(selector: str, frame_id: str | None = None) -> str:
    """Mirage: info about the first element matching a CSS selector (tag, id,
    class, text, visibility, rect) via Runtime.evaluate. frame_id: target an
    iframe (kahin_mirage_frame_tree); main frame is the default."""
    async with _healer_ref.safe("kahin_mirage_query", selector=selector[:80]):
        expr = (
            "(() => {"
            f"  const el = document.querySelector({_q(selector)});"
            "  if (!el) return null;"
            "  const r = el.getBoundingClientRect();"
            "  return {"
            "    tag: el.tagName.toLowerCase(),"
            "    id: el.id || null,"
            "    className: (typeof el.className === 'string' ? el.className : ''),"
            "    text: (el.textContent || '').trim().slice(0, 500),"
            "    visible: r.width > 0 && r.height > 0,"
            "    rect: {x: r.x, y: r.y, width: r.width, height: r.height}"
            "  };"
            "})()"
        )
        return await _mirage_evaluate(expr, frame_id)


@mcp.tool(name="kahin_mirage_query_all", annotations=_RO)
async def mirage_query_all(selector: str, limit: int = 100, frame_id: str | None = None) -> str:
    """Mirage: list matching elements (tag, id, text, visibility), capped.
    frame_id: target an iframe (kahin_mirage_frame_tree); main frame default."""
    async with _healer_ref.safe("kahin_mirage_query_all", selector=selector[:80], limit=limit):
        expr = (
            "(() => {"
            f"  const els = [...document.querySelectorAll({_q(selector)})].slice(0, {int(limit)});"
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
        return await _mirage_evaluate(expr, frame_id)


@mcp.tool(name="kahin_mirage_click", annotations=_DW)
async def mirage_click(selector: str, frame_id: str | None = None) -> str:
    """Mirage: click an element by CSS selector — real mouse events
    (Page.dispatchMouseEvent mousedown+mouseup at the element center).
    frame_id: target an iframe (kahin_mirage_frame_tree); main frame default."""
    async with _healer_ref.safe("kahin_mirage_click", selector=selector[:80], frame_id=frame_id):
        point = await _element_point(selector, frame_id)
        if not isinstance(point, tuple):
            return point
        x, y = point
        down = await _dispatch_mouse("mousedown", x, y, button=0, buttons=1)
        up = await _dispatch_mouse("mouseup", x, y, button=0, buttons=0)
        result = {"clicked": selector, "x": x, "y": y, "mousedown": down, "mouseup": up}
        return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_type", annotations=_RW)
async def mirage_type(selector: str, text: str, frame_id: str | None = None) -> str:
    """Mirage: focus an element and type text via Page.insertText (Juggler's
    real text-insertion method). frame_id: target an iframe
    (kahin_mirage_frame_tree); main frame default."""
    async with _healer_ref.safe("kahin_mirage_type", selector=selector[:80], text=text[:80], frame_id=frame_id):
        expr = (
            "(() => {"
            f"  const el = document.querySelector({_q(selector)});"
            '  if (!el) return {"error": "not found"};'
            "  el.focus();"
            '  return "focused";'
            "})()"
        )
        result = await _mirage_eval_result(expr, frame_id)
        if isinstance(result, str):
            return result
        if result.get("exceptionDetails"):
            return orjson.dumps({"error": "evaluate threw", "exception": result["exceptionDetails"]}).decode()
        err = await _require_mirage()
        if err:
            return err
        try:
            result = await _mirage_engine().call("Page.insertText", {"text": text})
        except RuntimeError as e:
            return orjson.dumps({"error": f"Juggler call failed: {e}"}).decode()
        return orjson.dumps({"typed": len(text), "selector": selector, "result": result}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_get_text", annotations=_RO)
async def mirage_get_text(selector: str, frame_id: str | None = None) -> str:
    """Mirage: trimmed textContent of the first matching element.
    frame_id: target an iframe (kahin_mirage_frame_tree); main frame default."""
    async with _healer_ref.safe("kahin_mirage_get_text", selector=selector[:80], frame_id=frame_id):
        expr = (
            "(() => {"
            f"  const el = document.querySelector({_q(selector)});"
            "  return el ? (el.textContent || '').trim() : null;"
            "})()"
        )
        return await _mirage_evaluate(expr, frame_id)


@mcp.tool(name="kahin_mirage_get_attribute", annotations=_RO)
async def mirage_get_attribute(selector: str, name: str, frame_id: str | None = None) -> str:
    """Mirage: attribute value of the first matching element.
    frame_id: target an iframe (kahin_mirage_frame_tree); main frame default."""
    async with _healer_ref.safe("kahin_mirage_get_attribute", selector=selector[:80], name=name[:80], frame_id=frame_id):
        expr = (
            "(() => {"
            f"  const el = document.querySelector({_q(selector)});"
            f"  return el ? (el.getAttribute({_q(name)}) ?? null) : null;"
            "})()"
        )
        return await _mirage_evaluate(expr, frame_id)


@mcp.tool(name="kahin_mirage_set_attribute", annotations=_RW)
async def mirage_set_attribute(selector: str, name: str, value: str, frame_id: str | None = None) -> str:
    """Mirage: set an attribute on the first matching element.
    frame_id: target an iframe (kahin_mirage_frame_tree); main frame default."""
    async with _healer_ref.safe("kahin_mirage_set_attribute", selector=selector[:80], name=name[:80], value=value[:80], frame_id=frame_id):
        expr = (
            "(() => {"
            f"  const el = document.querySelector({_q(selector)});"
            '  if (!el) return {"error": "not found"};'
            f"  el.setAttribute({_q(name)}, {_q(value)});"
            '  return "set";'
            "})()"
        )
        return await _mirage_evaluate(expr, frame_id)


@mcp.tool(name="kahin_mirage_focus", annotations=_RW)
async def mirage_focus(selector: str, frame_id: str | None = None) -> str:
    """Mirage: focus the first matching element.
    frame_id: target an iframe (kahin_mirage_frame_tree); main frame default."""
    async with _healer_ref.safe("kahin_mirage_focus", selector=selector[:80], frame_id=frame_id):
        expr = (
            "(() => {"
            f"  const el = document.querySelector({_q(selector)});"
            '  if (!el) return {"error": "not found"};'
            "  el.focus();"
            '  return "focused";'
            "})()"
        )
        return await _mirage_evaluate(expr, frame_id)


@mcp.tool(name="kahin_mirage_hover", annotations=_RW)
async def mirage_hover(selector: str, frame_id: str | None = None) -> str:
    """Mirage: move the mouse over the element center (dispatchMouseEvent
    mousemove). frame_id: target an iframe (kahin_mirage_frame_tree); main
    frame default."""
    async with _healer_ref.safe("kahin_mirage_hover", selector=selector[:80], frame_id=frame_id):
        point = await _element_point(selector, frame_id)
        if not isinstance(point, tuple):
            return point
        x, y = point
        move = await _dispatch_mouse("mousemove", x, y, button=0, buttons=0)
        result = {"hovered": selector, "x": x, "y": y, "mousemove": move}
        return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_get_html", annotations=_RO)
async def mirage_get_html(selector: str | None = None, frame_id: str | None = None) -> str:
    """Mirage: outerHTML of the first matching element, or the whole document.
    frame_id: target an iframe (kahin_mirage_frame_tree); main frame default."""
    async with _healer_ref.safe("kahin_mirage_get_html", selector=selector or "", frame_id=frame_id):
        if selector:
            expr = (
                "(() => {"
                f"  const el = document.querySelector({_q(selector)});"
                "  return el ? el.outerHTML : null;"
                "})()"
            )
        else:
            expr = "document.documentElement.outerHTML"
        return await _mirage_evaluate(expr, frame_id)


@mcp.tool(name="kahin_mirage_wait_selector", annotations=_RO)
async def mirage_wait_selector(selector: str, timeout: float = 10.0, frame_id: str | None = None) -> str:
    """Mirage: poll Runtime.evaluate until the selector matches (or timeout).
    frame_id: target an iframe (kahin_mirage_frame_tree); main frame default."""
    async with _healer_ref.safe("kahin_mirage_wait_selector", selector=selector[:80], timeout=timeout, frame_id=frame_id):
        expr = f"!!document.querySelector({_q(selector)})"
        deadline = asyncio.get_running_loop().time() + float(timeout)
        while True:
            result = await _mirage_eval_result(expr, frame_id)
            if isinstance(result, str):
                return result
            if (result.get("result") or {}).get("value") is True:
                return orjson.dumps({"found": True, "selector": selector, "frame_id": frame_id}).decode()
            if asyncio.get_running_loop().time() >= deadline:
                return orjson.dumps({"found": False, "selector": selector, "frame_id": frame_id, "timeout": timeout}).decode()
            await asyncio.sleep(0.25)


@mcp.tool(name="kahin_mirage_get_value", annotations=_RO)
async def mirage_get_value(selector: str, frame_id: str | None = None) -> str:
    """Mirage: value of an input/select/textarea (or null when missing).
    frame_id: target an iframe (kahin_mirage_frame_tree); main frame default."""
    async with _healer_ref.safe("kahin_mirage_get_value", selector=selector[:80], frame_id=frame_id):
        expr = (
            "(() => {"
            f"  const el = document.querySelector({_q(selector)});"
            "  return el ? (el.value ?? null) : null;"
            "})()"
        )
        return await _mirage_evaluate(expr, frame_id)


# ============================= Input (7) ===================================


@mcp.tool(name="kahin_mirage_mouse_click", annotations=_DW)
async def mirage_mouse_click(x: float, y: float, button: int = 0) -> str:
    """Mirage: raw click at viewport coordinates (mousedown+mouseup)."""
    async with _healer_ref.safe("kahin_mirage_mouse_click", x=x, y=y, button=button):
        down = await _dispatch_mouse("mousedown", x, y, button=button, buttons=1)
        up = await _dispatch_mouse("mouseup", x, y, button=button, buttons=0)
        return orjson.dumps({"x": x, "y": y, "button": button, "mousedown": down, "mouseup": up}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_mouse_move", annotations=_RW)
async def mirage_mouse_move(x: float, y: float) -> str:
    """Mirage: move the mouse to viewport coordinates (mousemove)."""
    async with _healer_ref.safe("kahin_mirage_mouse_move", x=x, y=y):
        return await _dispatch_mouse("mousemove", x, y, buttons=0)


@mcp.tool(name="kahin_mirage_mouse_down", annotations=_RW)
async def mirage_mouse_down(x: float, y: float, button: int = 0) -> str:
    """Mirage: press a mouse button (mousedown)."""
    async with _healer_ref.safe("kahin_mirage_mouse_down", x=x, y=y, button=button):
        return await _dispatch_mouse("mousedown", x, y, button=button, buttons=1)


@mcp.tool(name="kahin_mirage_mouse_up", annotations=_RW)
async def mirage_mouse_up(x: float, y: float, button: int = 0) -> str:
    """Mirage: release a mouse button (mouseup)."""
    async with _healer_ref.safe("kahin_mirage_mouse_up", x=x, y=y, button=button):
        return await _dispatch_mouse("mouseup", x, y, button=button, buttons=0)


@mcp.tool(name="kahin_mirage_key_press", annotations=_RW)
async def mirage_key_press(
    key: str, code: str = "Unidentified", key_code: int = 0, text: str | None = None,
) -> str:
    """Mirage: press one key (dispatchKeyEvent keyDown+keyUp; repeat is the
    REQUIRED protocol bool and is always False here)."""
    async with _healer_ref.safe("kahin_mirage_key_press", key=key[:40], code=code[:40], key_code=key_code):
        base: dict[str, Any] = {
            "type": "keyDown", "key": key, "keyCode": int(key_code),
            "location": 0, "code": code, "repeat": False,
        }
        if text is not None:
            base["text"] = text
        down = await _mirage_call("Page.dispatchKeyEvent", base)
        up = await _mirage_call("Page.dispatchKeyEvent", {
            "type": "keyUp", "key": key, "keyCode": int(key_code),
            "location": 0, "code": code, "repeat": False,
        })
        return orjson.dumps({"key": key, "keyDown": down, "keyUp": up}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_key_text", annotations=_RW)
async def mirage_key_text(text: str) -> str:
    """Mirage: type text one character at a time via dispatchKeyEvent
    (keyDown with text + keyUp per char, repeat=False)."""
    async with _healer_ref.safe("kahin_mirage_key_text", text=text[:80]):
        results: list[dict[str, Any]] = []
        for ch in text:
            down = await _mirage_call("Page.dispatchKeyEvent", {
                "type": "keyDown", "key": ch, "keyCode": ord(ch),
                "location": 0, "code": "Unidentified", "repeat": False, "text": ch,
            })
            up = await _mirage_call("Page.dispatchKeyEvent", {
                "type": "keyUp", "key": ch, "keyCode": ord(ch),
                "location": 0, "code": "Unidentified", "repeat": False,
            })
            results.append({"char": ch, "keyDown": down, "keyUp": up})
        return orjson.dumps({"typed": len(text), "chars": results}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_scroll", annotations=_RW)
async def mirage_scroll(delta_x: float = 0.0, delta_y: float = 100.0, x: float = 0.0, y: float = 0.0) -> str:
    """Mirage: scroll with a wheel event (Page.dispatchWheelEvent)."""
    async with _healer_ref.safe("kahin_mirage_scroll", delta_x=delta_x, delta_y=delta_y):
        return await _mirage_call("Page.dispatchWheelEvent", {
            "x": x, "y": y, "deltaX": delta_x, "deltaY": delta_y, "deltaZ": 0.0, "modifiers": 0,
        })


# ============================= PageEx (6) ==================================


async def _main_frame_id() -> tuple[str | None, str | None]:
    """Main frame id via Page.getFrameTree (driver-derived, real data)."""
    err = await _require_mirage()
    if err:
        return None, err
    try:
        result = await _mirage_engine().call("Page.getFrameTree")
    except RuntimeError as e:
        return None, orjson.dumps({"error": f"Juggler call failed: {e}"}).decode()
    frame = ((result.get("frameTree") or {}).get("frame") or {}).get("id")
    return (frame or None), None


@mcp.tool(name="kahin_mirage_reload", annotations=_RW)
async def mirage_reload() -> str:
    """Mirage: reload the current page (Page.reload with the main frameId —
    Juggler resolves the reload target by frameId, like goBack/goForward)."""
    async with _healer_ref.safe("kahin_mirage_reload"):
        frame_id, err = await _main_frame_id()
        if err:
            return err
        if not frame_id:
            return '{"error": "no main frame available"}'
        return await _mirage_call("Page.reload", {"frameId": frame_id})


@mcp.tool(name="kahin_mirage_go_back", annotations=_RW)
async def mirage_go_back() -> str:
    """Mirage: navigate back in history (Page.goBack with the main frameId)."""
    async with _healer_ref.safe("kahin_mirage_go_back"):
        frame_id, err = await _main_frame_id()
        if err:
            return err
        if not frame_id:
            return '{"error": "no main frame available"}'
        return await _mirage_call("Page.goBack", {"frameId": frame_id})


@mcp.tool(name="kahin_mirage_go_forward", annotations=_RW)
async def mirage_go_forward() -> str:
    """Mirage: navigate forward in history (Page.goForward)."""
    async with _healer_ref.safe("kahin_mirage_go_forward"):
        frame_id, err = await _main_frame_id()
        if err:
            return err
        if not frame_id:
            return '{"error": "no main frame available"}'
        return await _mirage_call("Page.goForward", {"frameId": frame_id})


@mcp.tool(name="kahin_mirage_stop", annotations=_RW)
async def mirage_stop() -> str:
    """Mirage: stop page loading (Runtime.evaluate window.stop())."""
    async with _healer_ref.safe("kahin_mirage_stop"):
        result = await _mirage_evaluate("window.stop(); true")
        try:
            parsed = orjson.loads(result)
        except ValueError:
            return result
        if isinstance(parsed, dict) and "error" in parsed:
            return result
        return '{"stopped": true}'


@mcp.tool(name="kahin_mirage_frame_tree", annotations=_RO)
async def mirage_frame_tree() -> str:
    """Mirage: frame tree of the current page (Page.getFrameTree)."""
    async with _healer_ref.safe("kahin_mirage_frame_tree"):
        return await _mirage_call("Page.getFrameTree")


@mcp.tool(name="kahin_mirage_page_content", annotations=_RO)
async def mirage_page_content() -> str:
    """Mirage: page HTML content + viewport/document sizes in one evaluate."""
    async with _healer_ref.safe("kahin_mirage_page_content"):
        expr = (
            "(() => ({"
            "  html: document.documentElement.outerHTML,"
            "  innerWidth: window.innerWidth,"
            "  innerHeight: window.innerHeight,"
            "  scrollWidth: document.documentElement.scrollWidth,"
            "  scrollHeight: document.documentElement.scrollHeight,"
            "  devicePixelRatio: window.devicePixelRatio"
            "}))()"
        )
        return await _mirage_evaluate(expr)