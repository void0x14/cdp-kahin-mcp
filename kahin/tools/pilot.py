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
from kahin.the_twins.capabilities import capabilities_for
from kahin.the_twins.mirage import Mirage
from kahin.the_twins.shadow import Obscura
from kahin.tools._common import (
    _DW,
    _RO,
    _RW,
    _auto_learn,
    _get_schema,
    _healer_ref,
    _MAX_TOOL_PAYLOAD_BYTES,
    _require_mirage,
    _safe_cdp,
)

logger = logging.getLogger(__name__)

_ENGINE_START_TIMEOUT = 60.0
_ENGINE_STOP_TIMEOUT = 15.0
_ENGINE_HEALTH_TIMEOUT = 5.0
_MAX_SELECTOR_LENGTH = 16_384
_MAX_ATTRIBUTE_LENGTH = 1_024
_MAX_EXTRACT_LENGTH = 100_000
_MAX_EVALUATE_LENGTH = 1_000_000
_MAX_SCREENSHOT_BYTES = 32 * 1024 * 1024


def _json_error(tool: str, message: str, code: str = "tool_error", **details: Any) -> str:
    payload: dict[str, Any] = {"error": message, "code": code, "tool": tool}
    payload.update(details)
    return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()


def _js_literal(value: str) -> str:
    """Encode a Python string as a JavaScript string literal, never repr()."""
    return orjson.dumps(value).decode()


def _validate_text(
    value: Any, *, tool: str, field: str, maximum: int, allow_none: bool = False,
) -> tuple[str | None, str | None]:
    if value is None and allow_none:
        return None, None
    if not isinstance(value, str):
        return None, _json_error(tool, f"{field} must be a string", "invalid_argument", field=field)
    if len(value) > maximum:
        return None, _json_error(
            tool,
            f"{field} exceeds the maximum length of {maximum}",
            "argument_too_large",
            field=field,
            maximum=maximum,
            received=len(value),
        )
    return value, None


def _normalize_evaluate_response(raw: Any, tool: str, *, nested_error: bool = False) -> str:
    """Turn evaluate failures into an error object without changing successes."""
    if isinstance(raw, str):
        try:
            payload = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return _json_error(tool, raw[:1_000], "tool_failed", raw_response=True)
    else:
        payload = raw

    if not isinstance(payload, dict):
        return _json_error(tool, "Browser returned an invalid evaluate response", "invalid_engine_response")
    if payload.get("exceptionDetails"):
        return _json_error(
            tool,
            "JavaScript evaluation failed",
            "javascript_error",
            exception=payload["exceptionDetails"],
        )
    if nested_error:
        result = payload.get("result")
        value = result.get("value") if isinstance(result, dict) else None
        if isinstance(value, dict) and value.get("error"):
            return _json_error(
                tool,
                str(value["error"]),
                "dom_error",
            )
    return raw if isinstance(raw, str) else orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()


async def _stop_engine(engine: Any, *, suppress: bool = True) -> None:
    """Best-effort bounded cleanup used on every failed lifecycle path."""
    try:
        await asyncio.wait_for(engine.stop(), timeout=_ENGINE_STOP_TIMEOUT)
    except asyncio.CancelledError:
        # Cancellation must reach the caller, but an interrupted MCP request
        # must not leave a browser child behind for the next request.
        try:
            await asyncio.shield(asyncio.wait_for(engine.stop(), timeout=_ENGINE_STOP_TIMEOUT))
        except BaseException:  # noqa: BLE001 - cleanup is already best effort
            logger.exception("browser cleanup failed after cancellation for %s", type(engine).__name__)
        raise
    except Exception:  # noqa: BLE001 - cleanup must not mask the start error
        logger.exception("browser cleanup failed for %s", type(engine).__name__)
        if not suppress:
            raise


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
async def browser_start(engine: str = "mirage", headless: bool = True, port: int = 0) -> str:
    """Start or reuse one browser engine.

    Camoufox/Mirage is the default because it is the complete visual browser
    surface: screenshots, mobile viewport, input, accessibility and
    screencast all work in the same Kahin process. Shadow/Obscura remains an
    explicit fast CDP opt-in; a visual tool promotes it to Mirage in-process.
    Repeating a start for the active engine is idempotent; use
    ``kahin_mirage_tab_new`` for another task/page instead of booting another
    browser.
    """
    if not isinstance(engine, str):
        return _json_error("kahin_browser_start", "engine must be a string", "invalid_argument", field="engine")
    if not isinstance(headless, bool):
        return _json_error("kahin_browser_start", "headless must be a boolean", "invalid_argument", field="headless")
    if isinstance(port, bool) or not isinstance(port, int) or port < 0 or port > 65535:
        return _json_error("kahin_browser_start", "port must be an integer between 0 and 65535", "invalid_argument", field="port")
    if port in (9222, 9240):
        return _json_error(
            "kahin_browser_start",
            f"Port {port} is RESERVED. Use a different port.",
            "reserved_port",
            field="port",
        )

    if engine not in ("shadow", "mirage", "camoufox"):
        return _json_error(
            "kahin_browser_start",
            f"Unknown engine: {engine}. Use 'shadow', 'mirage' or 'camoufox'.",
            "unknown_engine",
            field="engine",
        )

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
                        current_port = (
                            getattr(current, "port", None) if current_kind == "shadow" else 0
                        )
                        return orjson.dumps({
                            "status": "reused",
                            "engine": current_kind,
                            "capabilities": capabilities_for(current_kind),
                            "message": "Engine already running; reusing the existing browser and tabs.",
                            "port": current_port or 0,
                        }, option=orjson.OPT_INDENT_2).decode()
                    return orjson.dumps({
                        "error": f"Engine {current_kind} already running. Stop it before switching to {engine}.",
                        "hint": "Reuse the current engine or use its tab tools; no second browser was started.",
                    }, option=orjson.OPT_INDENT_2).decode()

                # The sidecar can outlive its Firefox child briefly. Probe
                # above catches that; now reap the stale process before any
                # replacement is allowed to start.
                logger.warning("replacing dead engine: %s", type(current).__name__)
                try:
                    await _stop_engine(current, suppress=False)
                except Exception as exc:
                    return _json_error(
                        "kahin_browser_start",
                        f"Could not reap the dead {type(current).__name__} engine: {exc}",
                        "engine_reap_failed",
                        hint="The old engine reference was retained; retry kahin_browser_stop before starting a replacement.",
                    )
                state._current_engine = None
                _healer_ref.bind_engine(None)
                state.clear_state()

            if engine == "shadow":
                candidate: Any = Obscura()
                actual_port = port
            else:
                candidate = Mirage(engine_name=engine)
                actual_port = 0  # Juggler pipe: no remote-debugging port

            try:
                await asyncio.wait_for(
                    candidate.start(headless=headless, port=actual_port),
                    timeout=_ENGINE_START_TIMEOUT,
                )
                if engine == "shadow":
                    actual_port = candidate.port or actual_port
            except asyncio.TimeoutError:
                failed_port = getattr(candidate, "port", None) or actual_port
                await _stop_engine(candidate)
                _healer_ref.bind_engine(None)
                return _json_error(
                    "kahin_browser_start",
                    f"Engine {engine} failed to start on port {failed_port} (timeout after {_ENGINE_START_TIMEOUT:.0f}s)",
                    "engine_start_timeout",
                    engine=engine,
                    port=failed_port,
                )
            except asyncio.CancelledError:
                await _stop_engine(candidate)
                _healer_ref.bind_engine(None)
                raise
            except Exception as exc:
                await _stop_engine(candidate)
                _healer_ref.bind_engine(None)
                return _json_error(
                    "kahin_browser_start",
                    f"Engine {engine} failed to start: {exc}",
                    "engine_start_failed",
                    engine=engine,
                )

            # Publish only a fully booted engine. If registration or a later
            # callback fails, the same cleanup rule prevents an orphan.
            try:
                state._current_engine = candidate
                await candidate.on_event(_on_cdp_event)
                await candidate.on_event(_on_network_event)
                await candidate.on_event(_on_console_event)
                eng = candidate
                eng.on_death(lambda: _on_engine_death(eng))
                _healer_ref.bind_engine(candidate)
            except BaseException:
                state._current_engine = None
                await _stop_engine(candidate)
                _healer_ref.bind_engine(None)
                state.clear_state()
                raise

            return orjson.dumps({
                "status": "started",
                "engine": "mirage" if engine == "camoufox" else engine,
                "capabilities": capabilities_for("mirage" if engine == "camoufox" else engine),
                "port": actual_port,
                "tabs": [],
                "hint": "Reuse this browser; for separate work create/switch a Mirage tab.",
            }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_browser_stop", annotations=_RW)
async def browser_stop() -> str:
    """Stop the active browser engine."""
    async with state._lifecycle_lock:
        if state._current_engine is None:
            _healer_ref.bind_engine(None)
            return _json_error(
                "kahin_browser_stop",
                "No engine running.",
                "engine_unavailable",
            )
        async with _healer_ref.safe("kahin_browser_stop"):
            engine = state._current_engine
            try:
                await _stop_engine(engine, suppress=False)
            except Exception as exc:
                return _json_error(
                    "kahin_browser_stop",
                    f"Browser cleanup failed: {exc}",
                    "engine_stop_failed",
                    hint="Retry kahin_browser_stop; the engine reference was retained for cleanup.",
                )
            state._current_engine = None
            _healer_ref.bind_engine(None)
            state.clear_state()
            return '{"status": "stopped"}'


@mcp.tool(name="kahin_navigate", annotations=_RW)
async def navigate(url: str) -> str:
    """Navigate the current page to a URL."""
    url_value, error = _validate_text(url, tool="kahin_navigate", field="url", maximum=_MAX_SELECTOR_LENGTH)
    if error:
        return error
    assert url_value is not None
    async with _healer_ref.safe("kahin_navigate", url=url_value[:80]):
        await _auto_learn("Page", "navigate", {"url": url_value})
        return await _safe_cdp("Page", "navigate", {"url": url_value})


@mcp.tool(name="kahin_click", annotations=_DW)
async def click(selector: str) -> str:
    """Click an element by CSS selector.

    Mirage uses its native coordinate/actionability path.  The JS click is
    retained only for an explicitly selected Shadow session, whose CDP
    surface has no native DOM-click primitive.
    """
    selector_value, error = _validate_text(
        selector, tool="kahin_click", field="selector", maximum=_MAX_SELECTOR_LENGTH,
    )
    if error:
        return error
    if not selector_value:
        return _json_error("kahin_click", "selector must not be empty", "invalid_argument", field="selector")
    async with _healer_ref.safe("kahin_click", selector=selector_value[:80]):
        try:
            if isinstance(state._current_engine, Mirage):
                # Lazy import avoids the pilot <-> pilot_mirage bootstrap
                # cycle while making the default Camoufox path real input.
                from kahin.tools.pilot_mirage import mirage_click  # noqa: PLC0415

                return await mirage_click(selector_value)
            await _auto_learn("Runtime", "click", {"selector": selector_value})
            expr = f"""(() => {{
                const el = document.querySelector({_js_literal(selector_value)});
                if (!el) return {{"error": "not found"}};
                el.scrollIntoView({{block: "center"}});
                el.click();
                return "clicked";
            }})()"""
            raw = await _safe_cdp("Runtime", "evaluate", {"expression": expr, "returnByValue": True})
            return _normalize_evaluate_response(raw, "kahin_click", nested_error=True)
        except Exception as exc:  # noqa: BLE001 - MCP tools must return JSON errors
            return _json_error("kahin_click", f"Click failed: {exc}", "tool_failed")


@mcp.tool(name="kahin_extract", annotations=_RO)
async def extract(selector: str | None = None, attribute: str | None = None) -> str:
    """Extract text content or attribute from page/element."""
    selector_value, error = _validate_text(
        selector,
        tool="kahin_extract",
        field="selector",
        maximum=_MAX_SELECTOR_LENGTH,
        allow_none=True,
    )
    if error:
        return error
    attribute_value, error = _validate_text(
        attribute,
        tool="kahin_extract",
        field="attribute",
        maximum=_MAX_ATTRIBUTE_LENGTH,
        allow_none=True,
    )
    if error:
        return error

    async with _healer_ref.safe(
        "kahin_extract", selector=selector_value or "", attribute=attribute_value or "",
    ):
        try:
            if selector_value and attribute_value:
                expr = (
                    "(() => {"
                    f"const el = document.querySelector({_js_literal(selector_value)});"
                    "if (!el) return '';"
                    f"const name = {_js_literal(attribute_value)};"
                    "const type = String(el.getAttribute('type') || el.type || '').toLowerCase();"
                    "if (type === 'password' && name.toLowerCase() === 'value') return '[redacted]';"
                    f"return String(el.getAttribute(name) ?? '').slice(0, {_MAX_EXTRACT_LENGTH});"
                    "})()"
                )
            elif selector_value:
                expr = (
                    "(() => {"
                    f"const el = document.querySelector({_js_literal(selector_value)});"
                    f"return String(el?.textContent ?? '').trim().slice(0, {_MAX_EXTRACT_LENGTH});"
                    "})()"
                )
            elif attribute_value:
                expr = (
                    f"String(document.documentElement.getAttribute({_js_literal(attribute_value)}) ?? '')"
                    f".slice(0, {_MAX_EXTRACT_LENGTH})"
                )
            else:
                expr = f"String(document.body?.innerText ?? '').slice(0, {_MAX_EXTRACT_LENGTH})"
            raw = await _safe_cdp("Runtime", "evaluate", {"expression": expr, "returnByValue": True})
            return _normalize_evaluate_response(raw, "kahin_extract")
        except Exception as exc:  # noqa: BLE001 - MCP tools must return JSON errors
            return _json_error("kahin_extract", f"Extraction failed: {exc}", "tool_failed")


@mcp.tool(name="kahin_screenshot", annotations=_RO)
async def screenshot(full_page: bool = False) -> str:
    """Capture a screenshot through Camoufox, promoting Shadow if needed."""
    if not isinstance(full_page, bool):
        return _json_error(
            "kahin_screenshot", "full_page must be a boolean", "invalid_argument", field="full_page",
        )
    try:
        err = await _require_mirage()
        if err:
            return err
        async with _healer_ref.safe("kahin_screenshot", full_page=full_page):
            engine = state._current_engine
            if engine is None:
                return _json_error("kahin_screenshot", "No browser engine is running", "engine_unavailable")
            data = await engine.screenshot(full_page=full_page)
            if not isinstance(data, (bytes, bytearray)) or not data:
                return _json_error(
                    "kahin_screenshot", "Browser returned no screenshot data", "invalid_engine_response",
                )
            if len(data) > _MAX_SCREENSHOT_BYTES:
                return _json_error(
                    "kahin_screenshot",
                    "Screenshot exceeds Kahin's bounded response size",
                    "result_too_large",
                    payloadBytes=len(data),
                    maxPayloadBytes=_MAX_SCREENSHOT_BYTES,
                    hint="Use the mobile viewport or a non-full-page screenshot.",
                )
            b64 = base64.b64encode(data).decode()
            return orjson.dumps({"screenshot": b64, "format": "png"}, option=orjson.OPT_INDENT_2).decode()
    except Exception as exc:  # noqa: BLE001 - visual tools must never leak exceptions
        return _json_error("kahin_screenshot", f"Screenshot failed: {exc}", "tool_failed")


@mcp.tool(name="kahin_evaluate", annotations=_RW)
async def evaluate(expression: str) -> str:
    """Execute JavaScript in the browser context. Returns JSON-serializable result."""
    expression_value, error = _validate_text(
        expression, tool="kahin_evaluate", field="expression", maximum=_MAX_EVALUATE_LENGTH,
    )
    if error:
        return error
    assert expression_value is not None
    async with _healer_ref.safe("kahin_evaluate", expression=expression_value[:80]):
        await _auto_learn("Runtime", "evaluate", {"expression": expression_value[:50]})
        return await _safe_cdp("Runtime", "evaluate", {
            "expression": expression_value,
            "returnByValue": True,
            "awaitPromise": True,
        })


@mcp.tool(name="kahin_execute_cdp", annotations=_DW)
async def execute_cdp(domain: str, command: str, parameters: dict[str, Any] | None = None) -> str:
    """Execute a raw CDP command directly (advanced). Auto-validates before sending."""
    if not isinstance(domain, str) or not domain or not isinstance(command, str) or not command:
        return _json_error(
            "kahin_execute_cdp",
            "domain and command must be non-empty strings",
            "invalid_argument",
        )
    if parameters is not None and not isinstance(parameters, dict):
        return _json_error(
            "kahin_execute_cdp", "parameters must be an object", "invalid_argument", field="parameters",
        )
    try:
        parameter_bytes = len(orjson.dumps(parameters or {}))
    except (TypeError, ValueError) as exc:
        return _json_error(
            "kahin_execute_cdp", f"parameters are not JSON-serializable: {exc}", "invalid_argument",
            field="parameters",
        )
    if parameter_bytes > _MAX_TOOL_PAYLOAD_BYTES:
        return _json_error(
            "kahin_execute_cdp",
            "parameters exceed Kahin's bounded tool payload",
            "argument_too_large",
            field="parameters",
            payloadBytes=parameter_bytes,
            maxPayloadBytes=_MAX_TOOL_PAYLOAD_BYTES,
        )
    async with _healer_ref.safe("kahin_execute_cdp", domain=domain, command=command):
        try:
            validation = _get_schema().validate_command(domain, command, parameters or {})
        except Exception as exc:  # noqa: BLE001 - schema is a public dependency
            return _json_error(
                "kahin_execute_cdp", f"CDP schema unavailable: {exc}", "schema_unavailable",
            )
        if not validation.get("valid"):
            errors = validation.get("errors", [])
            try:
                correction = _get_schema().error_decode(
                    error_code=-32601, error_message=f"'{domain}.{command}' not found",
                )
            except Exception:
                correction = {"common_causes": [], "solutions": []}
            result = {
                "error": "Command validation failed",
                "validation_errors": errors,
                "correction": correction.get("common_causes", []) + correction.get("solutions", []),
            }
            return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()
        return await _safe_cdp(domain, command, parameters or {})
