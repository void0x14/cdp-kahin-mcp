"""the_keymaker/seraph.py — Doğrulama Tool'ları (Seraph, Oracle'ın Koruyucusu)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp.types import Tool, TextContent

if TYPE_CHECKING:
    from kahin.the_source.architect import SchemaEngine


def tools() -> list[Tool]:
    return [
        Tool(
            name="kahin_validate_command",
            description="Validate a CDP command and its parameters against the schema before sending. Detects typos, missing required params, and type mismatches.",
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "CDP domain (e.g. Page, Network, Runtime)",
                    },
                    "command": {
                        "type": "string",
                        "description": "CDP command name (e.g. navigate, enable)",
                    },
                    "parameters": {
                        "type": "object",
                        "description": "Parameters to validate",
                        "additionalProperties": True,
                    },
                },
                "required": ["domain", "command", "parameters"],
            },
        ),
        Tool(
            name="kahin_error_decode",
            description="Decode a CDP error code and message to get explanation, common causes, and solutions",
            inputSchema={
                "type": "object",
                "properties": {
                    "error_code": {
                        "type": "number",
                        "description": "JSON-RPC error code (e.g. -32601, -32000)",
                    },
                    "error_message": {
                        "type": "string",
                        "description": "Error message text from CDP response",
                    },
                },
            },
        ),
        Tool(
            name="kahin_get_dependencies",
            description="Get prerequisites and required events for a CDP command",
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "CDP domain",
                    },
                    "command": {
                        "type": "string",
                        "description": "CDP command name",
                    },
                },
                "required": ["domain", "command"],
            },
        ),
    ]


async def handle(name: str, args: dict[str, Any], schema: SchemaEngine) -> list[TextContent]:
    import orjson

    match name:
        case "kahin_validate_command":
            result = schema.validate_command(
                args["domain"], args["command"], args.get("parameters", {})
            )

        case "kahin_error_decode":
            result = schema.error_decode(
                error_code=args.get("error_code"),
                error_message=args.get("error_message", ""),
            )

        case "kahin_get_dependencies":
            result = schema.get_dependencies(args["domain"], args["command"])

        case _:
            return [TextContent(type="text", text=f"Unknown seraph tool: {name}")]

    return [TextContent(type="text", text=orjson.dumps(result, option=orjson.OPT_INDENT_2).decode())]
