"""pilot.py — PILOT tools: browser engine control (engine-agnostic).

Owns browser lifecycle (start/stop) plus the shared navigation, DOM and
CDP passthrough tools. Event collectors are registered from
``kahin.oracle`` (engine lifecycle glue lives there); this module pulls
them back in for ``browser_start``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import orjson

from kahin import _state as state
from kahin.oracle import mcp, _on_cdp_event, _on_console_event, _on_network_event
from kahin.the_twins.mirage import Mirage
from kahin.the_twins.shadow import Obscura
from kahin.tools._common import (
    _DW,
    _RO,
    _RW,
    _auto_learn,
    _get_schema,
    _healer_ref,
    _require_engine,
    _safe_cdp,
)

logger = logging.getLogger(__name__)


@mcp.tool(name="kahin_browser_start", annotations=_RW)
async def browser_start(engine: str = "shadow", headless: bool = True, port: int = 0) -> str:
    """Start a browser engine. Choose shadow (fast Chrome) or mirage/camoufox (stealth Camoufox, Juggler pipe). Ports 9222/9240 are RESERVED."""
    if port in (9222, 9240):
        return orjson.dumps({"error": f"Port {port} is RESERVED. Use a different port."}).decode()

    if engine not in ("shadow", "mirage", "camoufox"):
        return f"Unknown engine: {engine}. Use 'shadow', 'mirage' or 'camoufox'."

    if state._current_engine is not None:
        return "Engine already running. Stop it first with kahin_browser_stop."

    async with _healer_ref.safe("kahin_browser_start", engine=engine, headless=headless, port=port):
        if engine == "shadow":
            state._current_engine = Obscura()
            actual_port = port or 9241
        else:
            state._current_engine = Mirage(engine_name=engine)
            actual_port = 0  # Juggler pipe: no remote-debugging port (9222/9240 irrelevant)

        try:
            _ctx = await asyncio.wait_for(
                state._current_engine.start(headless=headless, port=actual_port),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            state._current_engine = None
            raise RuntimeError(f"Engine {engine} failed to start on port {actual_port} (timeout)")
        except RuntimeError:
            state._current_engine = None
            raise
        except Exception as e:
            state._current_engine = None
            raise RuntimeError(f"Unexpected error starting {engine}: {e}") from e

        # Register event collectors
        await state._current_engine.on_event(_on_cdp_event)
        await state._current_engine.on_event(_on_network_event)
        await state._current_engine.on_event(_on_console_event)

        try:
            await state._current_engine.send_cdp("Network", "enable")
            await state._current_engine.send_cdp("Console", "enable")
        except Exception as e:
            logger.warning("Failed to enable domains: %s", e)

        return orjson.dumps({"status": "started", "engine": engine, "port": actual_port}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_browser_stop", annotations=_RW)
async def browser_stop() -> str:
    """Stop the active browser engine."""
    if state._current_engine is None:
        return "No engine running."
    async with _healer_ref.safe("kahin_browser_stop"):
        await state._current_engine.stop()
        state._current_engine = None
        state.clear_state()
        return '{"status": "stopped"}'


@mcp.tool(name="kahin_navigate", annotations=_RW)
async def navigate(url: str) -> str:
    """Navigate the current page to a URL."""
    async with _healer_ref.safe("kahin_navigate", url=url[:80]):
        await _auto_learn("Page", "navigate", {"url": url})
        return await _safe_cdp("Page", "navigate", {"url": url})


@mcp.tool(name="kahin_click", annotations=_DW)
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


@mcp.tool(name="kahin_extract", annotations=_RO)
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


@mcp.tool(name="kahin_screenshot", annotations=_RO)
async def screenshot(full_page: bool = False) -> str:
    """Capture a screenshot. Returns base64 PNG."""
    err = await _require_engine()
    if err:
        return err
    async with _healer_ref.safe("kahin_screenshot", full_page=full_page):
        engine = state._current_engine
        data = await engine.screenshot(full_page=full_page)  # type: ignore[union-attr]
        b64 = base64.b64encode(data).decode()
        return orjson.dumps({"screenshot": b64, "format": "png"}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_evaluate", annotations=_RW)
async def evaluate(expression: str) -> str:
    """Execute JavaScript in the browser context. Returns JSON-serializable result."""
    async with _healer_ref.safe("kahin_evaluate", expression=expression[:80]):
        await _auto_learn("Runtime", "evaluate", {"expression": expression[:50]})
        return await _safe_cdp("Runtime", "evaluate", {"expression": expression, "returnByValue": True})


@mcp.tool(name="kahin_execute_cdp", annotations=_DW)
async def execute_cdp(domain: str, command: str, parameters: dict[str, Any] | None = None) -> str:
    """Execute a raw CDP command directly (advanced). Auto-validates before sending."""
    async with _healer_ref.safe("kahin_execute_cdp", domain=domain, command=command):
        validation = _get_schema().validate_command(domain, command, parameters or {})
        if not validation.get("valid"):
            errors = validation.get("errors", [])
            correction = _get_schema().error_decode(error_code=-32601, error_message=f"'{domain}.{command}' not found")
            result = {
                "error": "Command validation failed",
                "validation_errors": errors,
                "correction": correction.get("common_causes", []) + correction.get("solutions", []),
            }
            return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()
        return await _safe_cdp(domain, command, parameters or {})