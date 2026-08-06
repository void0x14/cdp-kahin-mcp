"""dejavu.py — DEJA_VU tools: debug, network and console inspection (engine-agnostic)."""

from __future__ import annotations

import orjson

from kahin import _state as state
from kahin._mcp import mcp
from kahin.tools._common import _RO, _healer_ref, _require_engine, _safe_cdp


@mcp.tool(name="kahin_event_history", annotations=_RO)
async def event_history(event_type: str | None = None) -> str:
    """View accumulated CDP event history. Optionally filter by event type."""
    async with _healer_ref.safe("kahin_event_history", event_type=event_type or ""):
        err = await _require_engine()
        if err:
            return err
        if event_type:
            filtered = [e for e in state._current_event_log if e["event"] == event_type]
        else:
            filtered = list(state._current_event_log)[-50:]
        return orjson.dumps(filtered, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_list_network_requests", annotations=_RO)
async def list_network_requests(limit: int = 20) -> str:
    """List network requests captured from the current session."""
    async with _healer_ref.safe("kahin_list_network_requests", limit=limit):
        err = await _require_engine()
        if err:
            return err
        return orjson.dumps(list(state._network_requests)[-limit:], option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_get_console", annotations=_RO)
async def get_console() -> str:
    """Get accumulated console messages from the current session."""
    async with _healer_ref.safe("kahin_get_console"):
        err = await _require_engine()
        if err:
            return err
        return orjson.dumps(list(state._console_messages), option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_iframe_tree", annotations=_RO)
async def iframe_tree() -> str:
    """Get the iframe/frame tree of the current page."""
    async with _healer_ref.safe("kahin_iframe_tree"):
        return await _safe_cdp("Page", "getFrameTree")
