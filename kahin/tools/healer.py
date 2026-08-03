"""healer.py — self-healing statistics tool (engine-agnostic)."""

from __future__ import annotations

import orjson

from kahin._healer import get_tracker
from kahin.oracle import mcp
from kahin.tools._common import _RO


@mcp.tool(name="kahin_healer_stats", annotations=_RO)
async def healer_stats() -> str:
    """Get error tracking and self-healing statistics."""
    return orjson.dumps(get_tracker().get_stats(), option=orjson.OPT_INDENT_2).decode()