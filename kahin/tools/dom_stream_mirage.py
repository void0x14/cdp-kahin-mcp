"""Agent-facing real-time DOM stream for Mirage/Juggler.

The browser runs the actual ``MutationObserver``. These tools expose three
bounded surfaces an agent can compose:

* ``dom_start`` installs the observer for current and future documents;
* ``dom_snapshot`` returns a semantic, action-oriented live tree;
* ``dom_events`` returns cursor-based mutation deltas and can wait for a real
  browser mutation; ``dom_action`` executes an allow-listed action against a
  still-live node id rather than a stale CSS guess.

The stream is intentionally not an LLM prompt or hidden instruction. It is
structured browser state with explicit cursors, reset/drop signals and caps.
"""

from __future__ import annotations

import asyncio
from typing import Any

import orjson

from kahin._mcp import mcp
from kahin.dom_stream import DOM_STREAM_INIT_SCRIPT, js_call
from kahin.tools._common import (
    _DW,
    _RO,
    _RW,
    _healer_ref,
    _mirage_engine,
    _mirage_eval_result,
    _require_mirage,
)


def _dump(value: Any) -> str:
    return orjson.dumps(value, option=orjson.OPT_INDENT_2).decode()


def _unwrap(raw: dict[str, Any] | str) -> tuple[Any | None, str | None]:
    if isinstance(raw, str):
        return None, raw
    if raw.get("exceptionDetails"):
        return None, _dump({"error": "DOM stream evaluate threw", "exception": raw["exceptionDetails"]})
    result = raw.get("result") or {}
    if "value" not in result:
        return None, _dump({"error": "DOM stream returned no serializable value", "result": result})
    return result.get("value"), None


async def _prepare(frame_id: str | None, max_events: int = 512) -> tuple[Any | None, str | None]:
    err = await _require_mirage()
    if err:
        return None, err
    engine = _mirage_engine()
    await engine.ensure_page()
    try:
        await engine.install_dom_stream(DOM_STREAM_INIT_SCRIPT)
        raw = await _mirage_eval_result(DOM_STREAM_INIT_SCRIPT, frame_id)
        if isinstance(raw, str):
            return None, raw
        if raw.get("exceptionDetails"):
            return None, _dump({"error": "DOM stream setup threw", "exception": raw["exceptionDetails"]})
        configure = await _mirage_eval_result(
            js_call("configure", {"maxEvents": max_events}), frame_id
        )
        _, error = _unwrap(configure)
        if error:
            return None, error
    except Exception as exc:  # noqa: BLE001 - tool returns structured failure
        return None, _dump({"error": "DOM stream setup failed", "detail": str(exc)})
    return engine, None


async def _call_page(method: str, params: dict[str, Any], frame_id: str | None) -> tuple[Any | None, str | None]:
    raw = await _mirage_eval_result(js_call(method, params), frame_id)
    return _unwrap(raw)


@mcp.tool(name="kahin_mirage_dom_start", annotations=_RW)
async def mirage_dom_start(
    frame_id: str | None = None,
    max_events: int = 512,
) -> str:
    """Start/reuse the real browser DOM stream.

    The observer survives navigations through Juggler's init-script surface.
    ``frame_id`` targets an iframe for the immediate status; future frames are
    covered by the same browser-level init script.
    """
    async with _healer_ref.safe("kahin_mirage_dom_start", frame_id=frame_id, max_events=max_events):
        engine, error = await _prepare(frame_id, max_events=max_events)
        if error:
            return error
        value, error = await _call_page("status", {}, frame_id)
        if error:
            return error
        return _dump({"status": "started", "stream": value, "frame_id": frame_id})


@mcp.tool(name="kahin_mirage_dom_snapshot", annotations=_RO)
async def mirage_dom_snapshot(
    selector: str | None = None,
    max_nodes: int = 800,
    max_depth: int = 12,
    include_hidden: bool = False,
    text_limit: int = 240,
    frame_id: str | None = None,
) -> str:
    """Return a bounded live semantic DOM snapshot.

    Nodes include stable-per-document ``nodeId`` values, role/name/text,
    visibility/geometry, safe identifying attributes and action hints. A
    ``truncated`` result is an instruction to narrow with ``selector`` or
    increase caps; it is never silently presented as a complete page.
    """
    async with _healer_ref.safe(
        "kahin_mirage_dom_snapshot", selector=selector or "", frame_id=frame_id
    ):
        _, error = await _prepare(frame_id)
        if error:
            return error
        value, error = await _call_page("snapshot", {
            "selector": selector or "",
            "maxNodes": max_nodes,
            "maxDepth": max_depth,
            "includeHidden": include_hidden,
            "textLimit": text_limit,
        }, frame_id)
        return error or _dump(value)


@mcp.tool(name="kahin_mirage_dom_events", annotations=_RO)
async def mirage_dom_events(
    after_seq: int = 0,
    stream_id: str | None = None,
    limit: int = 100,
    wait_ms: int = 0,
    frame_id: str | None = None,
) -> str:
    """Read real MutationObserver deltas after a cursor.

    ``wait_ms`` enables bounded long-polling. On navigation or ring overflow,
    ``reset``/``dropped`` tells the agent to request a fresh snapshot before
    continuing; stale deltas are never treated as current truth.
    """
    async with _healer_ref.safe(
        "kahin_mirage_dom_events", after_seq=after_seq, stream_id=stream_id or "", wait_ms=wait_ms
    ):
        engine, error = await _prepare(frame_id)
        if error:
            return error
        deadline = asyncio.get_running_loop().time() + max(0, min(int(wait_ms), 30_000)) / 1000
        while True:
            value, error = await _call_page("drain", {
                "after": after_seq,
                "streamId": stream_id or "",
                "limit": limit,
            }, frame_id)
            if error:
                return error
            if value.get("events") or value.get("reset") or value.get("dropped"):
                return _dump(value)
            if wait_ms <= 0:
                return _dump(value)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return _dump(value)
            await engine.wait_for_dom_signal(min(0.5, remaining))


@mcp.tool(name="kahin_mirage_dom_action", annotations=_DW)
async def mirage_dom_action(
    node_id: str,
    action: str,
    text: str | None = None,
    frame_id: str | None = None,
) -> str:
    """Act on a live snapshot node without trusting a stale selector.

    Allowed actions are ``click``, ``hover``, ``focus``, ``type`` and
    ``scroll``. Click/hover use real Juggler mouse dispatch; type focuses the
    live element and uses the real Juggler ``Page.insertText`` command. A
    removed or navigated node returns ``requiresSnapshot`` instead of acting
    on an accidental replacement.
    """
    async with _healer_ref.safe(
        "kahin_mirage_dom_action", node_id=node_id, action=action, frame_id=frame_id
    ):
        engine, error = await _prepare(frame_id)
        if error:
            return error
        value, error = await _call_page("action", {"nodeId": node_id, "action": action}, frame_id)
        if error:
            return error
        if value.get("error"):
            return _dump(value)

        result: dict[str, Any] = {"target": value, "action": action}
        if action in {"click", "hover"}:
            event_type = "mousemove" if action == "hover" else "mousedown"
            result["down"] = await engine.call("Page.dispatchMouseEvent", {
                "type": event_type,
                "button": 0,
                "x": value["x"],
                "y": value["y"],
                "modifiers": 0,
                "clickCount": 1,
                "buttons": 1 if action == "click" else 0,
            })
            if action == "click":
                result["up"] = await engine.call("Page.dispatchMouseEvent", {
                    "type": "mouseup",
                    "button": 0,
                    "x": value["x"],
                    "y": value["y"],
                    "modifiers": 0,
                    "clickCount": 1,
                    "buttons": 0,
                })
        elif action == "type":
            if text is None:
                return _dump({"error": "text is required for action=type", "nodeId": node_id})
            result["typed"] = await engine.call("Page.insertText", {"text": text})
        return _dump(result)


@mcp.tool(name="kahin_mirage_dom_stop", annotations=_RW)
async def mirage_dom_stop(frame_id: str | None = None) -> str:
    """Disconnect the current document's observer and discard its page ring."""
    async with _healer_ref.safe("kahin_mirage_dom_stop", frame_id=frame_id):
        _, error = await _prepare(frame_id)
        if error:
            return error
        value, error = await _call_page("stop", {}, frame_id)
        return error or _dump({"status": "stopped", "stream": value, "frame_id": frame_id})
