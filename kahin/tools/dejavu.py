"""dejavu.py — DEJA_VU tools: debug, network and console inspection (engine-agnostic)."""

from __future__ import annotations

import orjson

from kahin import _state as state
from kahin._mcp import mcp
from kahin.tools._common import _RO, _healer_ref, _require_engine, _safe_cdp

_MAX_LIMIT = 1000


def _limit(value: int, default: int = 50) -> int:
    """Validate a bounded history request instead of letting Python slicing
    turn a negative value into an almost-unbounded result.
    """
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(_MAX_LIMIT, result))


@mcp.tool(name="kahin_event_history", annotations=_RO)
async def event_history(event_type: str | None = None, limit: int = 50) -> str:
    """View a bounded CDP event history, optionally filtered by event type."""
    async with _healer_ref.safe("kahin_event_history", event_type=event_type or "", limit=limit):
        err = await _require_engine()
        if err:
            return err
        bounded = _limit(limit)
        if event_type:
            filtered = [e for e in state._current_event_log if e["event"] == event_type][-bounded:]
        else:
            filtered = list(state._current_event_log)[-bounded:]
        return orjson.dumps(filtered, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_list_network_requests", annotations=_RO)
async def list_network_requests(limit: int = 20) -> str:
    """List network requests captured from the current session."""
    async with _healer_ref.safe("kahin_list_network_requests", limit=limit):
        err = await _require_engine()
        if err:
            return err
        return orjson.dumps(
            list(state._network_requests)[-_limit(limit, default=20):],
            option=orjson.OPT_INDENT_2,
        ).decode()


@mcp.tool(name="kahin_get_console", annotations=_RO)
async def get_console(limit: int = 100) -> str:
    """Get a bounded set of console messages from the current session."""
    async with _healer_ref.safe("kahin_get_console", limit=limit):
        err = await _require_engine()
        if err:
            return err
        return orjson.dumps(
            list(state._console_messages)[-_limit(limit, default=100):],
            option=orjson.OPT_INDENT_2,
        ).decode()


@mcp.tool(name="kahin_iframe_tree", annotations=_RO)
async def iframe_tree() -> str:
    """Get the iframe/frame tree of the current page."""
    async with _healer_ref.safe("kahin_iframe_tree"):
        return await _safe_cdp("Page", "getFrameTree")
