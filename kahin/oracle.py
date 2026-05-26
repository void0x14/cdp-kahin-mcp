"""oracle.py — MCP Server (Kahin'in Sesi)."""

from __future__ import annotations

import orjson
from mcp.server.fastmcp import FastMCP

from kahin.the_source.architect import SchemaEngine

schema = SchemaEngine()
schema.load()
mcp = FastMCP(
    name="kahin",
    instructions="I am the Oracle. I see the Source (CDP schema) and judge your commands. Always validate commands before sending. Decode errors when they occur.",
)


# === GRIMOIRE — CDP Knowledge ===

@mcp.tool()
async def kahin_list_domains() -> str:
    """List all CDP domains (Page, Network, Runtime, etc.)"""
    return orjson.dumps(schema.list_domains(), option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def kahin_get_domain(domain: str) -> str:
    """Get detailed info about a CDP domain: commands, events, types"""
    result = schema.get_domain(domain)
    if result is None:
        return f"Domain '{domain}' not found"
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def kahin_get_command(domain: str, command: str) -> str:
    """Get full details of a CDP command: parameters, returns, deprecation status"""
    result = schema.get_command(domain, command)
    if result is None:
        return f"Command '{domain}.{command}' not found"
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def kahin_get_event(domain: str, event: str) -> str:
    """Get details of a CDP event: parameters and deprecation status"""
    result = schema.get_event(domain, event)
    if result is None:
        return f"Event '{domain}.{event}' not found"
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def kahin_find_concept(query: str, max_results: int = 10) -> str:
    """Semantic search across all CDP domains, commands, and events"""
    result = schema.find_concept(query, max_results)
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def kahin_list_types(domain: str) -> str:
    """List all CDP types in a domain"""
    domain_types = [t for t in schema.types.values() if t.domain == domain]
    result = [{"name": t.name, "description": t.description, "type": t.type} for t in domain_types]
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def kahin_get_type(domain: str, type_name: str) -> str:
    """Get detailed info about a CDP type: properties, enum values"""
    full = f"{domain}.{type_name}"
    t = schema.types.get(full)
    if t is None:
        return f"Type '{full}' not found"
    result = {
        "name": t.name, "domain": t.domain, "type": t.type,
        "description": t.description, "enum_values": t.enum_values,
        "properties": [{"name": p.name, "type": p.type, "optional": p.optional, "description": p.description, "enum_values": p.enum_values} for p in t.properties],
    }
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


# === SERAPH — Validation ===

@mcp.tool()
async def kahin_validate_command(domain: str, command: str, parameters: dict) -> str:
    """Validate a CDP command and parameters against the schema. Detects typos, missing required params."""
    result = schema.validate_command(domain, command, parameters)
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def kahin_error_decode(error_code: int | None = None, error_message: str = "") -> str:
    """Decode a CDP error code and message to get explanation, common causes, and solutions"""
    result = schema.error_decode(error_code=error_code, error_message=error_message)
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def kahin_get_dependencies(domain: str, command: str) -> str:
    """Get prerequisites and required events for a CDP command"""
    result = schema.get_dependencies(domain, command)
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
