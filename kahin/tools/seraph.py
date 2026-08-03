"""seraph.py — SERAPH tools: CDP validation and error decoding (engine-agnostic)."""

from __future__ import annotations

from typing import Any

import orjson

from kahin._mcp import mcp
from kahin.tools._common import _RO, _get_schema


@mcp.tool(name="kahin_validate_command", annotations=_RO)
async def validate_command(domain: str, command: str, parameters: dict[str, Any]) -> str:
    """Validate a CDP command and parameters against the schema. Detects typos, missing required params."""
    return orjson.dumps(_get_schema().validate_command(domain, command, parameters), option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_error_decode", annotations=_RO)
async def error_decode(error_code: int | None = None, error_message: str | None = None) -> str:
    """Decode a CDP error code and message to get explanation, common causes, and solutions"""
    return orjson.dumps(_get_schema().error_decode(error_code=error_code, error_message=error_message), option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_get_dependencies", annotations=_RO)
async def get_dependencies(domain: str, command: str) -> str:
    """Get prerequisites and required events for a CDP command"""
    return orjson.dumps(_get_schema().get_dependencies(domain, command), option=orjson.OPT_INDENT_2).decode()