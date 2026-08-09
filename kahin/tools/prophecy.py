"""prophecy.py — PROPHECY tools: CDP pattern database (engine-agnostic)."""

from __future__ import annotations

import orjson

from kahin._mcp import mcp
from kahin.tools._common import _DW, _RO, _RW, _get_fate, _get_schema, _healer_ref


def _valid_limit(value: int, maximum: int = 100) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= maximum


@mcp.tool(name="kahin_pattern_learn", annotations=_RW)
async def pattern_learn(domain: str, command: str, context: str = "") -> str:
    """Teach the Oracle a CDP pattern for future suggestions."""
    async with _healer_ref.safe("kahin_pattern_learn", domain=domain, command=command):
        schema = _get_schema()
        if f"{domain}.{command}" not in schema.commands:
            return orjson.dumps(schema.validate_command(domain, command, {}), option=orjson.OPT_INDENT_2).decode()
        _get_fate().learn(domain, command, {}, context=context)
        return '{"status": "learned"}'


@mcp.tool(name="kahin_pattern_query", annotations=_RO)
async def pattern_query(domain: str | None = None, context: str = "", limit: int = 10) -> str:
    """Query learned CDP patterns. Filter by domain or context."""
    async with _healer_ref.safe("kahin_pattern_query", domain=domain or ""):
        if not _valid_limit(limit):
            return orjson.dumps({"error": "limit must be an integer between 1 and 100", "code": "invalid_argument", "field": "limit"}, option=orjson.OPT_INDENT_2).decode()
        return orjson.dumps(_get_fate().query(domain=domain, context=context, limit=limit), option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_pattern_suggest", annotations=_RO)
async def pattern_suggest(partial: str, limit: int = 5) -> str:
    """Suggest CDP commands matching a partial name (autocomplete)."""
    async with _healer_ref.safe("kahin_pattern_suggest", partial=partial):
        if not _valid_limit(limit, 50):
            return orjson.dumps({"error": "limit must be an integer between 1 and 50", "code": "invalid_argument", "field": "limit"}, option=orjson.OPT_INDENT_2).decode()
        return orjson.dumps(_get_fate().suggest(partial, limit=limit), option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_pattern_forget", annotations=_DW)
async def pattern_forget(domain: str, command: str) -> str:
    """Forget a specific CDP pattern."""
    async with _healer_ref.safe("kahin_pattern_forget", domain=domain, command=command):
        ok = _get_fate().forget(domain, command)
        return orjson.dumps({"status": "forgotten" if ok else "not found"}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_pattern_stats", annotations=_RO)
async def pattern_stats() -> str:
    """Get statistics about learned CDP patterns."""
    async with _healer_ref.safe("kahin_pattern_stats"):
        return orjson.dumps(_get_fate().stats(), option=orjson.OPT_INDENT_2).decode()
