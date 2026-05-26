"""the_keymaker/pilot.py — Browser Control Tool'ları (Operator)."""

from mcp.types import Tool


def tools() -> list[Tool]:
    return [
        Tool(
            name="kahin_browser_start",
            description="Start a browser engine (shadow=fast Chrome, mirage=stealth). Port 9222 and 9240 are RESERVED.",
            inputSchema={
                "type": "object",
                "properties": {
                    "engine": {
                        "type": "string",
                        "enum": ["shadow", "mirage"],
                        "description": "shadow=Obscura (fast Chrome), mirage=Crmoufox (stealth)",
                    },
                    "headless": {
                        "type": "boolean",
                        "description": "Run headless (default true)",
                    },
                    "port": {
                        "type": "number",
                        "description": "CDP port (default 9241 for shadow, 9242 for mirage)",
                    },
                },
                "required": ["engine"],
            },
        ),
        Tool(
            name="kahin_browser_stop",
            description="Stop the active browser engine",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="kahin_navigate",
            description="Navigate the current page to a URL",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to"},
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="kahin_click",
            description="Click an element on the page by CSS selector",
            inputSchema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector to click"},
                },
                "required": ["selector"],
            },
        ),
        Tool(
            name="kahin_extract",
            description="Extract text content from the page or a specific element",
            inputSchema={
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector (omit for full page text)",
                    },
                    "attribute": {
                        "type": "string",
                        "description": "HTML attribute to extract (e.g. href, src)",
                    },
                },
            },
        ),
        Tool(
            name="kahin_screenshot",
            description="Capture a screenshot of the current page",
            inputSchema={
                "type": "object",
                "properties": {
                    "full_page": {
                        "type": "boolean",
                        "description": "Capture full page (default false)",
                    },
                },
            },
        ),
        Tool(
            name="kahin_evaluate",
            description="Execute JavaScript in the browser context",
            inputSchema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "JavaScript expression to evaluate",
                    },
                },
                "required": ["expression"],
            },
        ),
        Tool(
            name="kahin_execute_cdp",
            description="Execute a raw CDP command directly (advanced)",
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "CDP domain"},
                    "command": {"type": "string", "description": "CDP command"},
                    "parameters": {
                        "type": "object",
                        "description": "CDP command parameters",
                    },
                },
                "required": ["domain", "command"],
            },
        ),
    ]
