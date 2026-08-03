"""trainman_mirage.py — Mirage (Camoufox/Juggler) TRAINMAN tools.

Faz 9 Task 3: Juggler session lifecycle over Mirage.call():

- tab_new          -> Browser.newPage (+ attachedToTarget session tracking)
- tab_switch       -> Mirage.switch_page (routes page-scoped calls)
- tab_close        -> Page.close on the target's session
- tab_list         -> attachedToTarget-derived target/session table
- tab_bring_front  -> Page.bringToFront
- context_new      -> Browser.createBrowserContext (isolated context)

All real Juggler methods — no fallbacks.
"""

from __future__ import annotations

from typing import Any

import orjson

from kahin._mcp import mcp
from kahin.tools._common import (
    _DW,
    _RO,
    _RW,
    _healer_ref,
    _mirage_call,
    _mirage_engine,
    _require_mirage,
)


@mcp.tool(name="kahin_mirage_tab_new", annotations=_RW)
async def mirage_tab_new(url: str = "about:blank", browser_context_id: str | None = None) -> str:
    """Mirage: open a new tab (Browser.newPage) and make it the current one.
    Returns {targetId, sessionId}."""
    async with _healer_ref.safe("kahin_mirage_tab_new", url=url[:120]):
        err = await _require_mirage()
        if err:
            return err
        try:
            page = await _mirage_engine().create_page(url=url, browser_context_id=browser_context_id)
            return orjson.dumps(page, option=orjson.OPT_INDENT_2).decode()
        except RuntimeError as e:
            return orjson.dumps({"error": f"newPage failed: {e}"}).decode()


@mcp.tool(name="kahin_mirage_tab_switch", annotations=_RW)
async def mirage_tab_switch(target_id: str) -> str:
    """Mirage: route page-scoped calls to an existing tab by targetId."""
    async with _healer_ref.safe("kahin_mirage_tab_switch", target_id=target_id[:80]):
        err = await _require_mirage()
        if err:
            return err
        try:
            result = await _mirage_engine().switch_page(target_id)
            return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()
        except RuntimeError as e:
            return orjson.dumps({"error": f"switch failed: {e}"}).decode()


@mcp.tool(name="kahin_mirage_tab_close", annotations=_DW)
async def mirage_tab_close(target_id: str) -> str:
    """Mirage: close a tab (Page.close on its Juggler session)."""
    async with _healer_ref.safe("kahin_mirage_tab_close", target_id=target_id[:80]):
        err = await _require_mirage()
        if err:
            return err
        try:
            result = await _mirage_engine().close_page(target_id)
            return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()
        except RuntimeError as e:
            return orjson.dumps({"error": f"close failed: {e}"}).decode()


@mcp.tool(name="kahin_mirage_tab_list", annotations=_RO)
async def mirage_tab_list() -> str:
    """Mirage: all open tabs with their Juggler session ids and the current one."""
    async with _healer_ref.safe("kahin_mirage_tab_list"):
        err = await _require_mirage()
        if err:
            return err
        pages: list[dict[str, Any]] = await _mirage_engine().list_pages()
        return orjson.dumps(pages, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_tab_bring_front", annotations=_RW)
async def mirage_tab_bring_front() -> str:
    """Mirage: bring the current tab to the front (Page.bringToFront)."""
    async with _healer_ref.safe("kahin_mirage_tab_bring_front"):
        return await _mirage_call("Page.bringToFront")


@mcp.tool(name="kahin_mirage_context_new", annotations=_RW)
async def mirage_context_new() -> str:
    """Mirage: create an isolated browser context (Browser.createBrowserContext).
    Returns the browserContextId to pass to kahin_mirage_tab_new."""
    async with _healer_ref.safe("kahin_mirage_context_new"):
        return await _mirage_call("Browser.createBrowserContext")