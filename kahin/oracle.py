"""oracle.py — MCP Server (Kahin'in Sesi)."""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any
from urllib.parse import urlparse

import orjson
from mcp.server.fastmcp import FastMCP

from kahin._state import _current_engine, _current_event_log, _network_requests, _console_messages, clear_state
from kahin.the_source.architect import SchemaEngine
from kahin.the_twins.shadow import Obscura
from kahin.the_twins.mirage import Mirage
from kahin.residual_self.fate import FateDB

schema = SchemaEngine()
fate = FateDB()
schema.load()
mcp = FastMCP(
    name="kahin",
    instructions="I am the Oracle. Always validate CDP commands before sending. Ports 9222 and 9240 are RESERVED.",
)


async def _auto_learn(domain: str, command: str, params: dict[str, Any] | None = None) -> None:
    """Auto-record a CDP pattern to FateDB."""
    try:
        url = (params or {}).get("url", "")
        ctx = ""
        if url:
            parsed = urlparse(url)
            ctx = parsed.hostname or "unknown"
        fate.learn(domain, command, params or {}, context=ctx)
    except Exception:
        pass


# === PHASE 1: GRIMOIRE — CDP Knowledge ===

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
    return orjson.dumps(schema.find_concept(query, max_results), option=orjson.OPT_INDENT_2).decode()


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


# === PHASE 1: SERAPH — Validation ===

@mcp.tool()
async def kahin_validate_command(domain: str, command: str, parameters: dict[str, Any]) -> str:
    """Validate a CDP command and parameters against the schema. Detects typos, missing required params."""
    return orjson.dumps(schema.validate_command(domain, command, parameters), option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def kahin_error_decode(error_code: int | None = None, error_message: str = "") -> str:
    """Decode a CDP error code and message to get explanation, common causes, and solutions"""
    return orjson.dumps(schema.error_decode(error_code=error_code, error_message=error_message), option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def kahin_get_dependencies(domain: str, command: str) -> str:
    """Get prerequisites and required events for a CDP command"""
    return orjson.dumps(schema.get_dependencies(domain, command), option=orjson.OPT_INDENT_2).decode()


async def _safe_cdp(domain: str, command: str, params: dict[str, Any] | None = None) -> str:
    """Execute CDP with error handling. Returns JSON string or error message."""
    err = await _require_engine()
    if err:
        return err
    try:
        result = await _current_engine.send_cdp(domain, command, params or {})
        return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()
    except RuntimeError as e:
        return f'{{"error": "CDP error: {e}"}}'
    except Exception as e:
        return f'{{"error": "Connection lost: {e}"}}'

async def _require_engine() -> str | None:
    """Ensure engine is running. Returns error message or None."""
    global _current_engine
    if _current_engine is None:
        return "No browser engine running. Use kahin_browser_start first."
    return None


@mcp.tool()
async def kahin_browser_start(engine: str = "shadow", headless: bool = True, port: int = 0) -> str:
    """Start a browser engine. Choose shadow (fast Chrome) or mirage (stealth). Ports 9222/9240 are RESERVED."""
    global _current_engine
    if _current_engine is not None:
        return "Engine already running. Stop it first with kahin_browser_stop."

    if port in (9222, 9240):
        return f'{{"error": "Port {port} is RESERVED. Use a different port."}}'

    if engine == "shadow":
        _current_engine = Obscura()
        actual_port = port or 9241
    elif engine == "mirage":
        _current_engine = Mirage()
        actual_port = port or 9242
    else:
        return f"Unknown engine: {engine}. Use 'shadow' or 'mirage'."

    try:
        ctx = await asyncio.wait_for(
            _current_engine.start(headless=headless, port=actual_port),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        _current_engine = None
        return f'{{"error": "Engine {engine} failed to start on port {actual_port} (timeout)"}}'
    except RuntimeError as e:
        _current_engine = None
        return f'{{"error": "Engine {engine} failed: {e}"}}'
    except Exception as e:
        _current_engine = None
        return f'{{"error": "Unexpected error starting {engine}: {e}"}}'

    # Register event collectors
    await _current_engine.on_event(_on_cdp_event)
    await _current_engine.on_event(_on_network_event)
    await _current_engine.on_event(_on_console_event)

    # Enable domains for event collection (non-critical)
    try:
        await _current_engine.send_cdp("Network", "enable")
        await _current_engine.send_cdp("Console", "enable")
    except Exception:
        pass

    return orjson.dumps({"status": "started", "engine": engine, "port": actual_port}, option=orjson.OPT_INDENT_2).decode()


def _on_cdp_event(evt) -> None:
    _current_event_log.append({"event": evt.method, "params": evt.params, "session_id": evt.session_id})


def _on_network_event(evt) -> None:
    if evt.method.startswith("Network."):
        _network_requests.append({
            "event": evt.method.replace("Network.", ""),
            "params": evt.params,
            "timestamp": time.time(),
        })


def _on_console_event(evt) -> None:
    if evt.method == "Console.messageAdded":
        _console_messages.append(evt.params.get("message", {}))


@mcp.tool()
async def kahin_browser_stop() -> str:
    """Stop the active browser engine."""
    global _current_engine
    if _current_engine is None:
        return "No engine running."
    await _current_engine.stop()
    _current_engine = None
    clear_state()
    return '{"status": "stopped"}'


@mcp.tool()
async def kahin_navigate(url: str) -> str:
    """Navigate the current page to a URL."""
    await _auto_learn("Page", "navigate", {"url": url})
    return await _safe_cdp("Page", "navigate", {"url": url})


@mcp.tool()
async def kahin_click(selector: str) -> str:
    """Click an element by CSS selector. Uses JS evaluate (CDP has no DOM.click)."""
    await _auto_learn("Runtime", "click", {"selector": selector})
    expr = f"""(() => {{
        const el = document.querySelector({selector!r});
        if (!el) return {{"error": "not found"}};
        el.scrollIntoView({{block: "center"}});
        el.click();
        return {{"clicked": true}};
    }})()"""
    return await _safe_cdp("Runtime", "evaluate", {"expression": expr})


@mcp.tool()
async def kahin_extract(selector: str | None = None, attribute: str | None = None) -> str:
    """Extract text content or attribute from page/element."""
    if selector and attribute:
        expr = f"document.querySelector({selector!r})?.getAttribute({attribute!r}) || ''"
    elif selector:
        expr = f"document.querySelector({selector!r})?.textContent?.trim() || ''"
    elif attribute:
        expr = f"document.documentElement.getAttribute({attribute!r}) || ''"
    else:
        expr = "document.body.innerText"
    return await _safe_cdp("Runtime", "evaluate", {"expression": expr})


@mcp.tool()
async def kahin_screenshot(full_page: bool = False) -> str:
    """Capture a screenshot. Returns base64 PNG."""
    err = await _require_engine()
    if err:
        return err
    try:
        data = await _current_engine.screenshot(full_page=full_page)
        b64 = base64.b64encode(data).decode()
        return orjson.dumps({"screenshot": b64, "format": "png"}, option=orjson.OPT_INDENT_2).decode()
    except Exception as e:
        return f'{{"error": "Screenshot failed: {e}"}}'


@mcp.tool()
async def kahin_evaluate(expression: str) -> str:
    """Execute JavaScript in the browser context."""
    await _auto_learn("Runtime", "evaluate", {"expression": expression[:50]})
    return await _safe_cdp("Runtime", "evaluate", {"expression": expression})


@mcp.tool()
async def kahin_execute_cdp(domain: str, command: str, parameters: dict[str, Any] | None = None) -> str:
    """Execute a raw CDP command directly (advanced)."""
    return await _safe_cdp(domain, command, parameters or {})


# === PHASE 2: TRAINMAN — Session Tools ===

@mcp.tool()
async def kahin_list_sessions() -> str:
    """List all CDP targets/sessions."""
    return await _safe_cdp("Target", "getTargets")


@mcp.tool()
async def kahin_get_session(session_id: str | None = None) -> str:
    """Get session/target info. Omitting session_id returns the default page target."""
    raw = await _safe_cdp("Target", "getTargets")
    try:
        result = orjson.loads(raw)
    except Exception:
        return raw
    if "error" in result:
        return raw
    infos = result.get("targetInfos", [])
    if session_id:
        info = next((t for t in infos if t["targetId"] == session_id), None)
    else:
        info = next((t for t in infos if t["type"] == "page"), infos[0] if infos else None)
    return orjson.dumps(info or {"error": "No session found"}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def kahin_create_session(url: str = "about:blank") -> str:
    """Create a new page/target."""
    return await _safe_cdp("Target", "createTarget", {"url": url})


@mcp.tool()
async def kahin_kill_session(session_id: str) -> str:
    """Close a target by targetId."""
    return await _safe_cdp("Target", "closeTarget", {"targetId": session_id})


# === PHASE 2: DEJA_VU — Debug/Network ===

@mcp.tool()
async def kahin_event_history(event_type: str | None = None) -> str:
    """View accumulated CDP event history. Optionally filter by event type."""
    global _current_event_log
    if event_type:
        filtered = [e for e in _current_event_log if e["event"] == event_type]
    else:
        filtered = _current_event_log[-50:]
    return orjson.dumps(filtered, option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def kahin_list_network_requests(limit: int = 20) -> str:
    """List network requests captured from the current session."""
    if _current_engine is None:
        return "No browser engine running."
    return orjson.dumps(_network_requests[-limit:], option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def kahin_get_console() -> str:
    """Get accumulated console messages from the current session."""
    if _current_engine is None:
        return "No browser engine running."
    return orjson.dumps(_console_messages, option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def kahin_iframe_tree() -> str:
    """Get the iframe/frame tree of the current page."""
    return await _safe_cdp("Page", "getFrameTree")


# === PHASE 3: PROPHECY — Pattern Tools ===

@mcp.tool()
async def kahin_pattern_learn(domain: str, command: str, context: str = "") -> str:
    """Teach the Oracle a CDP pattern for future suggestions."""
    fate.learn(domain, command, {}, context=context)
    return '{"status": "learned"}'


@mcp.tool()
async def kahin_pattern_query(domain: str | None = None, context: str = "", limit: int = 10) -> str:
    """Query learned CDP patterns. Filter by domain or context."""
    return orjson.dumps(fate.query(domain=domain, context=context, limit=limit), option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def kahin_pattern_suggest(partial: str, limit: int = 5) -> str:
    """Suggest CDP commands matching a partial name (autocomplete)."""
    return orjson.dumps(fate.suggest(partial, limit=limit), option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def kahin_pattern_forget(domain: str, command: str) -> str:
    """Forget a specific CDP pattern."""
    ok = fate.forget(domain, command)
    return orjson.dumps({"status": "forgotten" if ok else "not found"}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def kahin_pattern_stats() -> str:
    """Get statistics about learned CDP patterns."""
    return orjson.dumps(fate.stats(), option=orjson.OPT_INDENT_2).decode()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
