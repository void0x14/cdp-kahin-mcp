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
            info = next((t for t in infos if t["targetId"] == session_id), None)
        else:
            info = next((t for t in infos if t["type"] == "page"), infos[0] if infos else None)
        return orjson.dumps(info or {"error": "No session found"}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_create_session", annotations=_RW)
async def create_session(url: str = "about:blank") -> str:
    """
    Create a new page/target.
    """
    async with _healer_ref.safe("kahin_create_session", url=url):
        return await _safe_cdp("Target", "createTarget", {"url": url})


@mcp.tool(name="kahin_kill_session", annotations=_DW)
async def kill_session(session_id: str) -> str:
    """
    Close a target by targetId.
    """
    async with _healer_ref.safe("kahin_kill_session", session_id=session_id):
        return await _safe_cdp("Target", "closeTarget", {"targetId": session_id})