"""the_keymaker/grimoire.py — CDP Bilgi Tool'ları (Büyü Kitabı)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcp.types import Tool, TextContent

if TYPE_CHECKING:
    from kahin.the_source.architect import SchemaEngine


def tools() -> list[Tool]:
    return [
        Tool(
            name="kahin_list_domains",
            description="List all CDP domains (Page, Network, Runtime, etc.)",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="kahin_get_domain",
            description="Get detailed info about a CDP domain: its commands, events, and types",
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Domain name (e.g. Page, Network, Runtime)",
                    }
                },
                "required": ["domain"],
            },
        ),
        Tool(
            name="kahin_get_command",
            description="Get full details of a CDP command: parameters, returns, deprecation status",
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Domain name (e.g. Page, Network)",
                    },
                    "command": {
                        "type": "string",
                        "description": "Command name (e.g. navigate, enable)",
                    },
                },
                "required": ["domain", "command"],
            },
        ),
        Tool(
            name="kahin_get_event",
            description="Get details of a CDP event: parameters and deprecation status",
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Domain name (e.g. Page, Network)",
                    },
                    "event": {
                        "type": "string",
                        "description": "Event name (e.g. loadEventFired, requestWillBeSent)",
                    },
                },
                "required": ["domain", "event"],
            },
        ),
        Tool(
            name="kahin_find_concept",
            description="Semantic search across all CDP domains, commands, and events",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language query (e.g. 'frame navigation lifecycle', 'capture screenshot')",
                    },
                    "max_results": {
                        "type": "number",
                        "description": "Maximum results (default 10)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="kahin_list_types",
            description="List all CDP types in a domain",
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Domain name (e.g. Page, Network)",
                    }
                },
                "required": ["domain"],
            },
        ),
        Tool(
            name="kahin_get_type",
            description="Get detailed info about a CDP type: properties, enum values",
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Domain name (e.g. Page, Network)",
                    },
                    "type_name": {
                        "type": "string",
                        "description": "Type name (e.g. FrameId, Cookie)",
                    },
                },
                "required": ["domain", "type_name"],
            },
        ),
    ]


async def handle(name: str, args: dict, schema: SchemaEngine) -> list[TextContent]:
    match name:
        case "kahin_list_domains":
            result = schema.list_domains()

        case "kahin_get_domain":
            result = schema.get_domain(args["domain"])
            if result is None:
                return [TextContent(type="text", text=f"Domain '{args['domain']}' not found")]

        case "kahin_get_command":
            result = schema.get_command(args["domain"], args["command"])
            if result is None:
                return [TextContent(type="text", text=f"Command '{args['domain']}.{args['command']}' not found")]

        case "kahin_get_event":
            result = schema.get_event(args["domain"], args["event"])
            if result is None:
                return [TextContent(type="text", text=f"Event '{args['domain']}.{args['event']}' not found")]

        case "kahin_find_concept":
            query = args["query"]
            max_results = int(args.get("max_results", 10))
            result = schema.find_concept(query, max_results)

        case "kahin_list_types":
            types = schema.types
            domain = args["domain"]
            domain_types = [t for t in types.values() if t.domain == domain]
            result = [
                {"name": t.name, "description": t.description, "type": t.type}
                for t in domain_types
            ]

        case "kahin_get_type":
            full = f"{args['domain']}.{args['type_name']}"
            t = schema.types.get(full)
            if t is None:
                return [TextContent(type="text", text=f"Type '{full}' not found")]
            result = {
                "name": t.name,
                "domain": t.domain,
                "type": t.type,
                "description": t.description,
                "enum_values": t.enum_values,
                "properties": [
                    {
                        "name": p.name,
                        "type": p.type,
                        "optional": p.optional,
                        "description": p.description,
                        "enum_values": p.enum_values,
                    }
                    for p in t.properties
                ],
            }

        case _:
            return [TextContent(type="text", text=f"Unknown grimoire tool: {name}")]

    import orjson
    return [TextContent(type="text", text=orjson.dumps(result, option=orjson.OPT_INDENT_2).decode())]
