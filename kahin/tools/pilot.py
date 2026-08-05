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
from kahin._mcp import mcp
from kahin.oracle import _on_cdp_event, _on_console_event, _on_engine_death, _on_network_event
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

_ENGINE_START_TIMEOUT = 60.0
_ENGINE_STOP_TIMEOUT = 15.0
_ENGINE_HEALTH_TIMEOUT = 5.0


async def _stop_engine(engine: Any) -> None:
    """Best-effort bounded cleanup used on every failed lifecycle path."""
    try:
        await asyncio.wait_for(engine.stop(), timeout=_ENGINE_STOP_TIMEOUT)
    except BaseException:  # noqa: BLE001 - cleanup must not mask the start error
        logger.exception("browser cleanup failed for %s", type(engine).__name__)


async def _engine_is_healthy(engine: Any) -> bool:
    """Check the browser child, not only the Python/sidecar process."""
    if isinstance(engine, Mirage):
        try:
            result = await asyncio.wait_for(engine.health(), timeout=_ENGINE_HEALTH_TIMEOUT)
        except Exception:  # noqa: BLE001
            engine._mark_dead()
            return False
        return bool(result.get("alive"))
    return bool(engine.is_alive())


@mcp.tool(name="kahin_browser_start", annotations=_RW)
async def browser_start(engine: str = "shadow", headless: bool = True, port: int = 0) -> str:
    """Start or reuse one browser engine.

    Mirage/camoufox tabs live inside this one process. Repeating a start for
    the active engine is idempotent; use ``kahin_mirage_tab_new`` for another
    task/page instead of booting another browser.
    """
    if port in (9222, 9240):
        return orjson.dumps({"error": f"Port {port} is RESERVED. Use a different port."}).decode()

    if engine not in ("shadow", "mirage", "camoufox"):
        return f"Unknown engine: {engine}. Use 'shadow', 'mirage' or 'camoufox'."

    async with state._lifecycle_lock:
        async with _healer_ref.safe("kahin_browser_start", engine=engine, headless=headless, port=port):
            current = state._current_engine
            if current is not None:
                if await _engine_is_healthy(current):
                    current_kind = "shadow" if isinstance(current, Obscura) else "mirage"
                    requested_kind = "shadow" if engine == "shadow" else "mirage"
                    if current_kind == requested_kind:
                        # Same browser, same process: callers may safely make
                        # start part of their setup without leaking a child.
                        return orjson.dumps({
                            "status": "reused",
                            "engine": current_kind,
                            "message": "Engine already running; reusing the existing browser and tabs.",
                            "port": port or (9241 if current_kind == "shadow" else 0),
                        }, option=orjson.OPT_INDENT_2).decode()
                    return orjson.dumps({
                        "error": f"Engine {current_kind} already running. Stop it before switching to {engine}.",
                        "hint": "Reuse the current engine or use its tab tools; no second browser was started.",
                    }, option=orjson.OPT_INDENT_2).decode()

                # The sidecar can outlive its Firefox child briefly. Probe
                # above catches that; now reap the stale process before any
                # replacement is allowed to start.
                logger.warning("replacing dead engine: %s", type(current).__name__)
                await _stop_engine(current)
                state._current_engine = None
                state.clear_state()

            if engine == "shadow":
                candidate: Any = Obscura()
                actual_port = port or 9241
            else:
                candidate = Mirage(engine_name=engine)
                actual_port = 0  # Juggler pipe: no remote-debugging port

            try:
                _ctx = await asyncio.wait_for(
                    candidate.start(headless=headless, port=actual_port),
                    timeout=_ENGINE_START_TIMEOUT,
                )
            except asyncio.TimeoutError as exc:
                await _stop_engine(candidate)
                raise RuntimeError(
                    f"Engine {engine} failed to start on port {actual_port} (timeout after {_ENGINE_START_TIMEOUT:.0f}s)"
                ) from exc
            except BaseException:
                await _stop_engine(candidate)
                raise

            # Publish only a fully booted engine. If registration or a later
            # callback fails, the same cleanup rule prevents an orphan.
            try:
                state._current_engine = candidate
                await candidate.on_event(_on_cdp_event)
                await candidate.on_event(_on_network_event)
                await candidate.on_event(_on_console_event)
                eng = candidate
                eng.on_death(lambda: _on_engine_death(eng))
            except BaseException:
                state._current_engine = None
                await _stop_engine(candidate)
                state.clear_state()
                raise

            return orjson.dumps({
                "status": "started",
                "engine": engine,
                "port": actual_port,
                "tabs": [],
                "hint": "Reuse this browser; for separate work create/switch a Mirage tab.",
            }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_browser_stop", annotations=_RW)
async def browser_stop() -> str:
    """Stop the active browser engine."""
    async with state._lifecycle_lock:
        if state._current_engine is None:
            return "No engine running."
        async with _healer_ref.safe("kahin_browser_stop"):
            engine = state._current_engine
            await _stop_engine(engine)
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
