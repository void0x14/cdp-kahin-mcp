"""seraph.py — SERAPH tools: CDP validation and error decoding (engine-agnostic)."""

from __future__ import annotations

from typing import Any

import orjson

from kahin._mcp import mcp
from kahin.tools._common import _RO, _get_schema


def _safe_seraph(tool: str, operation: Any) -> str:
    try:
        return orjson.dumps(operation(), option=orjson.OPT_INDENT_2).decode()
    except Exception as exc:  # noqa: BLE001 - public tool boundary
        return orjson.dumps({
            "error": f"CDP validation operation failed: {exc}",
            "code": "schema_unavailable",
            "tool": tool,
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_validate_command", annotations=_RO)
async def validate_command(domain: str, command: str, parameters: dict[str, Any]) -> str:
    """Validate a CDP command and parameters against the schema. Detects typos, missing required params."""
    if not isinstance(domain, str) or not domain or not isinstance(command, str) or not command:
        return _safe_seraph("kahin_validate_command", lambda: {"valid": False, "errors": [{"message": "domain and command must be non-empty strings"}]})
    return _safe_seraph("kahin_validate_command", lambda: _get_schema().validate_command(domain, command, parameters))


@mcp.tool(name="kahin_error_decode", annotations=_RO)
async def error_decode(error_code: int | None = None, error_message: str | None = None) -> str:
    """Decode a CDP error code and message to get explanation, common causes, and solutions"""
    return _safe_seraph("kahin_error_decode", lambda: _get_schema().error_decode(error_code=error_code, error_message=error_message))


@mcp.tool(name="kahin_get_dependencies", annotations=_RO)
async def get_dependencies(domain: str, command: str) -> str:
    """Get prerequisites and required events for a CDP command"""
    if not isinstance(domain, str) or not domain or not isinstance(command, str) or not command:
        return _safe_seraph("kahin_get_dependencies", lambda: {"error": "domain and command must be non-empty strings", "code": "invalid_argument"})
    return _safe_seraph("kahin_get_dependencies", lambda: _get_schema().get_dependencies(domain, command))
