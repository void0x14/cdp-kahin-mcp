"""oracle.py — MCP Server (Kahin'in Sesi)."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any
from urllib.parse import urlparse

import orjson
from mcp.server.fastmcp import FastMCP

from kahin._healer import get_healer, get_tracker
from kahin._state import (
    _console_messages,
    _current_engine,
    _current_event_log,
    _network_requests,
    clear_state,
)
from kahin.residual_self.fate import FateDB
from kahin.the_twins.chassis import EventData
from kahin.the_source.architect import SchemaEngine
from kahin.the_twins.mirage import Mirage
from kahin.the_twins.shadow import Obscura

logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="kahin",
    instructions="I am the Oracle. Always validate CDP commands before sending. Ports 9222 and 9240 are RESERVED.",
)


_schema: SchemaEngine | None = None
_fate: FateDB | None = None
_healer_ref = get_healer()


def _get_schema() -> SchemaEngine:
    global _schema
    if _schema is None:
        s = SchemaEngine()
        s.load()
        _schema = s
    return _schema


def _get_fate() -> FateDB:
    global _fate
    if _fate is None:
        _fate = FateDB()
    return _fate


async def _auto_learn(domain: str, command: str, params: dict[str, Any] | None = None) -> None:
    """Auto-record a CDP pattern to FateDB."""
    try:
        url = (params or {}).get("url", "")
        ctx = ""
        if url:
            parsed = urlparse(url)
            ctx = parsed.hostname or "unknown"
        _get_fate().learn(domain, command, params or {}, context=ctx)
    except Exception as e:
        logger.warning("auto_learn failed for %s.%s: %s", domain, command, e)


# === PHASE 1: GRIMOIRE — CDP Knowledge ===

@mcp.tool()
async def list_domains() -> str:
    """List all CDP domains (Page, Network, Runtime, etc.)"""
    return orjson.dumps(_get_schema().list_domains(), option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def get_domain(domain: str) -> str:
    """Get detailed info about a CDP domain: commands, events, types"""
    result = _get_schema().get_domain(domain)
    if result is None:
        return f"Domain '{domain}' not found"
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def get_command(domain: str, command: str) -> str:
    """Get full details of a CDP command: parameters, returns, deprecation status"""
    result = _get_schema().get_command(domain, command)
    if result is None:
        return f"Command '{domain}.{command}' not found"
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def get_event(domain: str, event: str) -> str:
    """Get details of a CDP event: parameters and deprecation status"""
    result = _get_schema().get_event(domain, event)
    if result is None:
        return f"Event '{domain}.{event}' not found"
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def find_concept(query: str, max_results: int = 10) -> str:
    """Semantic search across all CDP domains, commands, and events"""
    return orjson.dumps(_get_schema().find_concept(query, max_results), option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def list_types(domain: str) -> str:
    """List all CDP types in a domain"""
    domain_types = [t for t in _get_schema().types.values() if t.domain == domain]
    result = [{"name": t.name, "description": t.description, "type": t.type} for t in domain_types]
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
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


# === PHASE 1: SERAPH — Validation ===

@mcp.tool()
async def validate_command(domain: str, command: str, parameters: dict[str, Any]) -> str:
    """Validate a CDP command and parameters against the _get_schema(). Detects typos, missing required params."""
    return orjson.dumps(_get_schema().validate_command(domain, command, parameters), option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def error_decode(error_code: int | None = None, error_message: str | None = None) -> str:
    """Decode a CDP error code and message to get explanation, common causes, and solutions"""
    return orjson.dumps(_get_schema().error_decode(error_code=error_code, error_message=error_message), option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def get_dependencies(domain: str, command: str) -> str:
    """Get prerequisites and required events for a CDP command"""
    return orjson.dumps(_get_schema().get_dependencies(domain, command), option=orjson.OPT_INDENT_2).decode()


async def _safe_cdp(domain: str, command: str, params: dict[str, Any] | None = None) -> str:
    """Execute CDP with error handling. Returns JSON string or error message."""
    err = await _require_engine()
    if err:
        return err
    engine = _current_engine
    try:
        result = await engine.send_cdp(domain, command, params or {})  # type: ignore[union-attr]
        return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()
    except RuntimeError as e:
        msg = str(e)
        return orjson.dumps({"error": f"CDP error: {msg}"}).decode()
    except Exception as e:
        msg = str(e)
        return orjson.dumps({"error": f"Connection lost: {msg}"}).decode()

async def _require_engine() -> str | None:
    """Ensure engine is running. Returns error message or None."""
    global _current_engine
    if _current_engine is None:
        return "No browser engine running. Use kahin_browser_start first."
    return None


@mcp.tool()
async def browser_start(engine: str = "shadow", headless: bool = True, port: int = 0) -> str:
    """Start a browser engine. Choose shadow (fast Chrome) or mirage (stealth). Ports 9222/9240 are RESERVED."""
    global _current_engine

    if port in (9222, 9240):
        return orjson.dumps({"error": f"Port {port} is RESERVED. Use a different port."}).decode()

    if engine not in ("shadow", "mirage"):
        return f"Unknown engine: {engine}. Use 'shadow' or 'mirage'."

    if _current_engine is not None:
        return "Engine already running. Stop it first with kahin_browser_stop."

    async with _healer_ref.safe("kahin_browser_start", engine=engine, headless=headless, port=port) as ctx:
        if engine == "shadow":
            _current_engine = Obscura()
            actual_port = port or 9241
        else:
            _current_engine = Mirage()
            actual_port = port or 9242

        try:
            _ctx = await asyncio.wait_for(
                _current_engine.start(headless=headless, port=actual_port),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            _current_engine = None
            raise RuntimeError(f"Engine {engine} failed to start on port {actual_port} (timeout)")
        except RuntimeError as e:
            _current_engine = None
            raise
        except Exception as e:
            _current_engine = None
            raise RuntimeError(f"Unexpected error starting {engine}: {e}") from e

        # Register event collectors
        await _current_engine.on_event(_on_cdp_event)
        await _current_engine.on_event(_on_network_event)
        await _current_engine.on_event(_on_console_event)

        try:
            await _current_engine.send_cdp("Network", "enable")
            await _current_engine.send_cdp("Console", "enable")
        except Exception as e:
            logger.warning("Failed to enable domains: %s", e)

        return orjson.dumps({"status": "started", "engine": engine, "port": actual_port}, option=orjson.OPT_INDENT_2).decode()


def _on_cdp_event(evt: EventData) -> None:
    _current_event_log.append({"event": evt.method, "params": evt.params, "session_id": evt.session_id})


def _on_network_event(evt: EventData) -> None:
    if evt.method.startswith("Network."):
        _network_requests.append({
            "event": evt.method.replace("Network.", ""),
            "params": evt.params,
            "timestamp": time.time(),
        })


def _on_console_event(evt: EventData) -> None:
    if evt.method == "Console.messageAdded":
        _console_messages.append(evt.params.get("message", {}))


@mcp.tool()
async def browser_stop() -> str:
    """Stop the active browser engine."""
    global _current_engine
    if _current_engine is None:
        return "No engine running."
    async with _healer_ref.safe("kahin_browser_stop"):
        await _current_engine.stop()
        _current_engine = None
        clear_state()
        return '{"status": "stopped"}'


@mcp.tool()
async def navigate(url: str) -> str:
    """Navigate the current page to a URL."""
    async with _healer_ref.safe("kahin_navigate", url=url[:80]):
        await _auto_learn("Page", "navigate", {"url": url})
        return await _safe_cdp("Page", "navigate", {"url": url})


@mcp.tool()
async def click(selector: str) -> str:
    """Click an element by CSS selector. Uses JS evaluate (CDP has no DOM.click)."""
    async with _healer_ref.safe("kahin_click", selector=selector[:80]):
        await _auto_learn("Runtime", "click", {"selector": selector})
        expr = f"""(() => {{
            const el = document.querySelector({selector!r});
            if (!el) return {{"error": "not found"}};
            el.scrollIntoView({{block: "center"}});
            el.click();
            return "clicked";
        }})()"""
        return await _safe_cdp("Runtime", "evaluate", {"expression": expr, "returnByValue": True})


@mcp.tool()
async def extract(selector: str | None = None, attribute: str | None = None) -> str:
    """Extract text content or attribute from page/element."""
    async with _healer_ref.safe("kahin_extract", selector=selector or "", attribute=attribute or ""):
        if selector and attribute:
            expr = f"document.querySelector({selector!r})?.getAttribute({attribute!r}) || ''"
        elif selector:
            expr = f"document.querySelector({selector!r})?.textContent?.trim() || ''"
        elif attribute:
            expr = f"document.documentElement.getAttribute({attribute!r}) || ''"
        else:
            expr = "document.body.innerText"
        return await _safe_cdp("Runtime", "evaluate", {"expression": expr, "returnByValue": True})


@mcp.tool()
async def screenshot(full_page: bool = False) -> str:
    """Capture a screenshot. Returns base64 PNG."""
    err = await _require_engine()
    if err:
        return err
    async with _healer_ref.safe("kahin_screenshot", full_page=full_page):
        engine = _current_engine
        data = await engine.screenshot(full_page=full_page)  # type: ignore[union-attr]
        b64 = base64.b64encode(data).decode()
        return orjson.dumps({"screenshot": b64, "format": "png"}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def evaluate(expression: str) -> str:
    """Execute JavaScript in the browser context. Returns JSON-serializable result."""
    async with _healer_ref.safe("kahin_evaluate", expression=expression[:80]):
        await _auto_learn("Runtime", "evaluate", {"expression": expression[:50]})
        return await _safe_cdp("Runtime", "evaluate", {"expression": expression, "returnByValue": True})


@mcp.tool()
async def execute_cdp(domain: str, command: str, parameters: dict[str, Any] | None = None) -> str:
    """Execute a raw CDP command directly (advanced)."""
    async with _healer_ref.safe("kahin_execute_cdp", domain=domain, command=command):
        return await _safe_cdp(domain, command, parameters or {})


# === PHASE 2: TRAINMAN — Session Tools ===

@mcp.tool()
async def list_sessions() -> str:
    """List all CDP targets/sessions."""
    async with _healer_ref.safe("kahin_list_sessions"):
        return await _safe_cdp("Target", "getTargets")


@mcp.tool()
async def get_session(session_id: str | None = None) -> str:
    """Get session/target info. Omitting session_id returns the default page target."""
    async with _healer_ref.safe("kahin_get_session", session_id=session_id or ""):
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
async def create_session(url: str = "about:blank") -> str:
    """Create a new page/target."""
    async with _healer_ref.safe("kahin_create_session", url=url):
        return await _safe_cdp("Target", "createTarget", {"url": url})


@mcp.tool()
async def kill_session(session_id: str) -> str:
    """Close a target by targetId."""
    async with _healer_ref.safe("kahin_kill_session", session_id=session_id):
        return await _safe_cdp("Target", "closeTarget", {"targetId": session_id})


# === PHASE 2: DEJA_VU — Debug/Network ===

@mcp.tool()
async def event_history(event_type: str | None = None) -> str:
    """View accumulated CDP event history. Optionally filter by event type."""
    global _current_event_log
    async with _healer_ref.safe("kahin_event_history", event_type=event_type or ""):
        if event_type:
            filtered = [e for e in _current_event_log if e["event"] == event_type]
        else:
            filtered = list(_current_event_log)[-50:]
        return orjson.dumps(filtered, option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def list_network_requests(limit: int = 20) -> str:
    """List network requests captured from the current session."""
    if _current_engine is None:
        return "No browser engine running."
    async with _healer_ref.safe("kahin_list_network_requests", limit=limit):
        return orjson.dumps(list(_network_requests)[-limit:], option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def get_console() -> str:
    """Get accumulated console messages from the current session."""
    if _current_engine is None:
        return "No browser engine running."
    async with _healer_ref.safe("kahin_get_console"):
        return orjson.dumps(list(_console_messages), option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def iframe_tree() -> str:
    """Get the iframe/frame tree of the current page."""
    async with _healer_ref.safe("kahin_iframe_tree"):
        return await _safe_cdp("Page", "getFrameTree")


# === PHASE 3: PROPHECY — Pattern Tools ===

@mcp.tool()
async def pattern_learn(domain: str, command: str, context: str = "") -> str:
    """Teach the Oracle a CDP pattern for future suggestions."""
    async with _healer_ref.safe("kahin_pattern_learn", domain=domain, command=command):
        _get_fate().learn(domain, command, {}, context=context)
        return '{"status": "learned"}'


@mcp.tool()
async def pattern_query(domain: str | None = None, context: str = "", limit: int = 10) -> str:
    """Query learned CDP patterns. Filter by domain or context."""
    async with _healer_ref.safe("kahin_pattern_query", domain=domain or ""):
        return orjson.dumps(_get_fate().query(domain=domain, context=context, limit=limit), option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def pattern_suggest(partial: str, limit: int = 5) -> str:
    """Suggest CDP commands matching a partial name (autocomplete)."""
    async with _healer_ref.safe("kahin_pattern_suggest", partial=partial):
        return orjson.dumps(_get_fate().suggest(partial, limit=limit), option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def pattern_forget(domain: str, command: str) -> str:
    """Forget a specific CDP pattern."""
    async with _healer_ref.safe("kahin_pattern_forget", domain=domain, command=command):
        ok = _get_fate().forget(domain, command)
        return orjson.dumps({"status": "forgotten" if ok else "not found"}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def pattern_stats() -> str:
    """Get statistics about learned CDP patterns."""
    async with _healer_ref.safe("kahin_pattern_stats"):
        return orjson.dumps(_get_fate().stats(), option=orjson.OPT_INDENT_2).decode()


@mcp.tool()
async def healer_stats() -> str:
    """Get error tracking and self-healing statistics."""
    return orjson.dumps(get_tracker().get_stats(), option=orjson.OPT_INDENT_2).decode()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
