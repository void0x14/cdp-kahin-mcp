"""trainman.py — TRAINMAN tools: session/target management (engine-agnostic)."""

from __future__ import annotations

import orjson

from kahin._mcp import mcp
from kahin.tools._common import _DW, _RO, _RW, _healer_ref, _safe_cdp


@mcp.tool(name="kahin_list_sessions", annotations=_RO)
async def list_sessions() -> str:
    """
    List all CDP targets/sessions.
    """
    async with _healer_ref.safe("kahin_list_sessions"):
        return await _safe_cdp("Target", "getTargets")


@mcp.tool(name="kahin_get_session", annotations=_RO)
async def get_session(session_id: str | None = None) -> str:
    """
    Get session/target info. Omitting session_id returns the default page target.
    """
    if session_id is not None and (not isinstance(session_id, str) or not session_id):
        return orjson.dumps({
            "error": "session_id must be a non-empty string when provided",
            "code": "invalid_argument",
            "field": "session_id",
        }).decode()
    async with _healer_ref.safe("kahin_get_session", session_id=session_id or ""):
        raw = await _safe_cdp("Target", "getTargets")
        try:
            result = orjson.loads(raw)
        except Exception:
            return raw
        if "error" in result:
            return raw
        infos = result.get("targetInfos", [])
        if session_id:
            info = next(
                (
                    t for t in infos
                    if t.get("targetId") == session_id
                    or t.get("sessionId") == session_id
                    or t.get("session_id") == session_id
                ),
                None,
            )
        else:
            info = next((t for t in infos if t["type"] == "page"), infos[0] if infos else None)
        return orjson.dumps(
            info or {"error": "No session found", "code": "session_not_found"},
            option=orjson.OPT_INDENT_2,
        ).decode()


@mcp.tool(name="kahin_create_session", annotations=_RW)
async def create_session(url: str = "about:blank") -> str:
    """
    Create a new page/target.
    """
    if not isinstance(url, str) or not url or len(url) > 16_384:
        return orjson.dumps({
            "error": "url must be a non-empty string of at most 16384 characters",
            "code": "invalid_argument",
            "field": "url",
        }).decode()
    async with _healer_ref.safe("kahin_create_session", url=url[:120]):
        return await _safe_cdp("Target", "createTarget", {"url": url})


@mcp.tool(name="kahin_kill_session", annotations=_DW)
async def kill_session(session_id: str) -> str:
    """
    Close a target by targetId.
    """
    if not isinstance(session_id, str) or not session_id:
        return orjson.dumps({
            "error": "session_id must be a non-empty string",
            "code": "invalid_argument",
            "field": "session_id",
        }).decode()
    async with _healer_ref.safe("kahin_kill_session", session_id=session_id[:120]):
        return await _safe_cdp("Target", "closeTarget", {"targetId": session_id})
