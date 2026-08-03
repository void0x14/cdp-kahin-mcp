"""grimoire.py — GRIMOIRE tools: CDP knowledge-base queries (engine-agnostic)."""

from __future__ import annotations

import orjson

from kahin._mcp import mcp
from kahin.tools._common import _RO, _get_schema


@mcp.tool(name="kahin_list_domains", annotations=_RO)
async def list_domains() -> str:
    """List all CDP domains (Page, Network, Runtime, etc.)"""
    return orjson.dumps(_get_schema().list_domains(), option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_get_domain", annotations=_RO)
async def get_domain(domain: str) -> str:
    """Get detailed info about a CDP domain: commands, events, types"""
    result = _get_schema().get_domain(domain)
    if result is None:
        return f"Domain '{domain}' not found"
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_get_command", annotations=_RO)
async def get_command(domain: str, command: str) -> str:
    """Get full details of a CDP command: parameters, returns, deprecation status"""
    result = _get_schema().get_command(domain, command)
    if result is None:
        return f"Command '{domain}.{command}' not found"
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_get_event", annotations=_RO)
async def get_event(domain: str, event: str) -> str:
    """Get details of a CDP event: parameters and deprecation status"""
    result = _get_schema().get_event(domain, event)
    if result is None:
        return f"Event '{domain}.{event}' not found"
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_find_concept", annotations=_RO)
async def find_concept(query: str, max_results: int = 10) -> str:
    """Semantic search across all CDP domains, commands, and events"""
    return orjson.dumps(_get_schema().find_concept(query, max_results), option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_list_types", annotations=_RO)
async def list_types(domain: str) -> str:
    """List all CDP types in a domain"""
    domain_types = [t for t in _get_schema().types.values() if t.domain == domain]
    result = [{"name": t.name, "description": t.description, "type": t.type} for t in domain_types]
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_get_type", annotations=_RO)
async def get_type(domain: str, type_name: str) -> str:
    """Get detailed info about a CDP type: properties, enum values"""
    full = f"{domain}.{type_name}"
    t = _get_schema().types.get(full)
    if t is None:
        return f"Type '{full}' not found"
    result = {
        "name": t.name, "domain": t.domain, "type": t.type,
        "description": t.description, "enum_values": t.enum_values,
        "properties": [{"name": p.name, "type": p.type, "optional": p.optional, "description": p.description, "enum_values": p.enum_values} for p in t.properties],
    }
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()