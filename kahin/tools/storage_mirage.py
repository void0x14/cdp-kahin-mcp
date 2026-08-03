"""storage_mirage.py — Mirage (Camoufox/Juggler) storage tools.

Faz 9 Task 3: cookies via the real Juggler Browser domain
(Browser.getCookies / Browser.setCookies / Browser.clearCookies — the
protocol has NO dedicated Storage domain) and localStorage/sessionStorage
through Runtime.evaluate (web platform APIs, not fakes).
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
    _mirage_evaluate,
)


@mcp.tool(name="kahin_mirage_cookie_get", annotations=_RO)
async def mirage_cookie_get() -> str:
    """Mirage: all cookies of the current context (Browser.getCookies)."""
    async with _healer_ref.safe("kahin_mirage_cookie_get"):
        return await _mirage_call("Browser.getCookies")


@mcp.tool(name="kahin_mirage_cookie_set", annotations=_RW)
async def mirage_cookie_set(cookies: list[dict[str, Any]]) -> str:
    """Mirage: set cookies (Browser.setCookies). Each cookie: name, value,
    and optional url/domain/path/secure/httpOnly/sameSite/expires."""
    async with _healer_ref.safe("kahin_mirage_cookie_set"):
        return await _mirage_call("Browser.setCookies", {"cookies": cookies})


@mcp.tool(name="kahin_mirage_cookie_clear", annotations=_DW)
async def mirage_cookie_clear() -> str:
    """Mirage: clear all cookies of the current context (Browser.clearCookies)."""
    async with _healer_ref.safe("kahin_mirage_cookie_clear"):
        return await _mirage_call("Browser.clearCookies")


@mcp.tool(name="kahin_mirage_storage_local_get", annotations=_RO)
async def mirage_storage_local_get() -> str:
    """Mirage: all localStorage entries of the current origin."""
    async with _healer_ref.safe("kahin_mirage_storage_local_get"):
        return await _mirage_evaluate(
            "Object.entries(localStorage).map(([k, v]) => ({key: k, value: v}))"
        )


@mcp.tool(name="kahin_mirage_storage_local_set", annotations=_RW)
async def mirage_storage_local_set(key: str, value: str) -> str:
    """Mirage: write one localStorage entry for the current origin."""
    async with _healer_ref.safe("kahin_mirage_storage_local_set", key=key[:120], value=value[:200]):
        k = orjson.dumps(key).decode()
        v = orjson.dumps(value).decode()
        return await _mirage_evaluate(f"(() => {{ localStorage.setItem({k}, {v}); return 'set'; }})()")


@mcp.tool(name="kahin_mirage_storage_session_get", annotations=_RO)
async def mirage_storage_session_get() -> str:
    """Mirage: all sessionStorage entries of the current origin."""
    async with _healer_ref.safe("kahin_mirage_storage_session_get"):
        return await _mirage_evaluate(
            "Object.entries(sessionStorage).map(([k, v]) => ({key: k, value: v}))"
        )