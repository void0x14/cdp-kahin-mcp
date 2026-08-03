"""_common.py — shared plumbing for the kahin tool modules.

Engine-agnostic helpers moved out of kahin/oracle.py so tool category
modules only carry their own ``@mcp.tool`` registrations.

NOTES
- The ``mcp`` instance does NOT live here — it lives in ``kahin/oracle.py``;
  tool modules do ``from kahin.oracle import mcp``.
- Mutable runtime state (``_current_engine``, event/network/console buffers)
  lives in ``kahin._state``; tools reach it through the ``state`` module
  reference so assignments and appends share one object set across modules.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import orjson

from kahin import _state as state
from kahin._healer import get_healer
from kahin.residual_self.fate import FateDB
from kahin.the_source.architect import SchemaEngine

logger = logging.getLogger(__name__)

_healer_ref = get_healer()

_schema: SchemaEngine | None = None
_fate: FateDB | None = None


def _get_schema() -> SchemaEngine:
    global _schema
    if _schema is None:
        s = SchemaEngine()
        s.load()
        _schema = s
    return _schema


def _get_fate() -> FateDB:
    global _fate
    if _fate is None:
        _fate = FateDB()
    return _fate


async def _auto_learn(domain: str, command: str, params: dict[str, Any] | None = None) -> None:
    """Auto-record a CDP pattern to FateDB."""
    try:
        url = (params or {}).get("url", "")
        ctx = ""
        if url:
            parsed = urlparse(url)
            ctx = parsed.hostname or "unknown"
        _get_fate().learn(domain, command, params or {}, context=ctx)
    except Exception as e:
        logger.warning("auto_learn failed for %s.%s: %s", domain, command, e)


_RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_RW = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
_DW = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}


async def _safe_cdp(domain: str, command: str, params: dict[str, Any] | None = None) -> str:
    """Execute CDP with error handling + auto-education. Returns JSON string."""
    err = await _require_engine()
    if err:
        return err
    engine = state._current_engine
    try:
        result = await engine.send_cdp(domain, command, params or {})  # type: ignore[union-attr]
        return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()
    except RuntimeError as e:
        msg = str(e)
        correction = _get_schema().error_decode(error_code=-32601, error_message=f"'{domain}.{command}' not found")
        return orjson.dumps({
            "error": f"CDP error: {msg}",
            "hint": correction.get("common_causes", []) + correction.get("solutions", []),
        }, option=orjson.OPT_INDENT_2).decode()
    except Exception as e:
        msg = str(e)
        return orjson.dumps({
            "error": f"Connection lost: {msg}",
            "hint": "Browser engine may have crashed. Use kahin_browser_stop then kahin_browser_start to restart.",
        }).decode()


async def _require_engine() -> str | None:
    """Ensure engine is running. Returns error message or None."""
    if state._current_engine is None:
        return "No browser engine running. Use kahin_browser_start first."
    return None