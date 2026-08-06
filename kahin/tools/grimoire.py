"""grimoire.py — GRIMOIRE tools: CDP knowledge-base queries (engine-agnostic)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import orjson

from kahin._mcp import mcp
from kahin.tools._common import _RO, _get_schema


def _safe_schema(tool: str, operation: Callable[[], Any]) -> str:
    """Schema lookup tools must not turn a corrupt/missing bundle into MCP
    transport-level exceptions. The error is structured so the agent can
    repair the installation or inspect the exact operation.
    """
    try:
        return orjson.dumps(operation(), option=orjson.OPT_INDENT_2).decode()
    except Exception as exc:  # noqa: BLE001 - public tool boundary
        return orjson.dumps({
            "error": f"CDP schema operation failed: {exc}",
            "code": "schema_unavailable",
            "tool": tool,
            "hint": "Reinstall the Kahin package or verify the bundled protocol.json.gz.",
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_list_domains", annotations=_RO)
async def list_domains() -> str:
    """List all CDP domains (Page, Network, Runtime, etc.)"""
    return _safe_schema("kahin_list_domains", lambda: _get_schema().list_domains())


@mcp.tool(name="kahin_get_domain", annotations=_RO)
async def get_domain(domain: str) -> str:
    """Get detailed info about a CDP domain: commands, events, types"""
    if not isinstance(domain, str) or not domain:
        return _safe_schema("kahin_get_domain", lambda: {"error": "domain must be a non-empty string", "code": "invalid_argument"})
    def lookup() -> Any:
        result = _get_schema().get_domain(domain)
        return result if result is not None else {"error": f"Domain '{domain}' not found", "code": "not_found"}
    return _safe_schema("kahin_get_domain", lookup)


@mcp.tool(name="kahin_get_command", annotations=_RO)
async def get_command(domain: str, command: str) -> str:
    """Get full details of a CDP command: parameters, returns, deprecation status"""
    if not isinstance(domain, str) or not domain or not isinstance(command, str) or not command:
        return _safe_schema("kahin_get_command", lambda: {"error": "domain and command must be non-empty strings", "code": "invalid_argument"})
    def lookup() -> Any:
        result = _get_schema().get_command(domain, command)
        return result if result is not None else {"error": f"Command '{domain}.{command}' not found", "code": "not_found"}
    return _safe_schema("kahin_get_command", lookup)


@mcp.tool(name="kahin_get_event", annotations=_RO)
async def get_event(domain: str, event: str) -> str:
    """Get details of a CDP event: parameters and deprecation status"""
    if not isinstance(domain, str) or not domain or not isinstance(event, str) or not event:
        return _safe_schema("kahin_get_event", lambda: {"error": "domain and event must be non-empty strings", "code": "invalid_argument"})
    def lookup() -> Any:
        result = _get_schema().get_event(domain, event)
        return result if result is not None else {"error": f"Event '{domain}.{event}' not found", "code": "not_found"}
    return _safe_schema("kahin_get_event", lookup)


@mcp.tool(name="kahin_find_concept", annotations=_RO)
async def find_concept(query: str, max_results: int = 10) -> str:
    """Semantic search across all CDP domains, commands, and events"""
    if not isinstance(query, str):
        return _safe_schema("kahin_find_concept", lambda: {"error": "query must be a string", "code": "invalid_argument"})
    if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results <= 0:
        return _safe_schema("kahin_find_concept", lambda: {"error": "max_results must be a positive integer", "code": "invalid_argument"})
    return _safe_schema("kahin_find_concept", lambda: _get_schema().find_concept(query[:16_384], min(max_results, 100)))


@mcp.tool(name="kahin_list_types", annotations=_RO)
async def list_types(domain: str) -> str:
    """List all CDP types in a domain"""
    if not isinstance(domain, str) or not domain:
        return _safe_schema("kahin_list_types", lambda: {"error": "domain must be a non-empty string", "code": "invalid_argument"})
    return _safe_schema(
        "kahin_list_types",
        lambda: [
            {"name": t.name, "description": t.description, "type": t.type}
            for t in _get_schema().types.values()
            if t.domain == domain
        ],
    )


@mcp.tool(name="kahin_get_type", annotations=_RO)
async def get_type(domain: str, type_name: str) -> str:
    """Get detailed info about a CDP type: properties, enum values"""
    if not isinstance(domain, str) or not domain or not isinstance(type_name, str) or not type_name:
        return _safe_schema("kahin_get_type", lambda: {"error": "domain and type_name must be non-empty strings", "code": "invalid_argument"})
    full = f"{domain}.{type_name}"
    def lookup() -> Any:
        t = _get_schema().types.get(full)
        if t is None:
            return {"error": f"Type '{full}' not found", "code": "not_found"}
        return {
            "name": t.name, "domain": t.domain, "type": t.type,
            "description": t.description, "enum_values": t.enum_values,
            "properties": [{"name": p.name, "type": p.type, "optional": p.optional, "description": p.description, "enum_values": p.enum_values} for p in t.properties],
        }
    return _safe_schema("kahin_get_type", lookup)
