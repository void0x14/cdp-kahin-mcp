"""_common.py — shared plumbing for the kahin tool modules.

Engine-agnostic helpers moved out of kahin/oracle.py so tool category
modules only carry their own ``@mcp.tool`` registrations.

NOTES
- The ``mcp`` instance does NOT live here — it lives in ``kahin/oracle.py``;
  tool modules do ``from kahin._mcp import mcp``.
- Mutable runtime state (``_current_engine``, event/network/console buffers)
  lives in ``kahin._state``; tools reach it through the ``state`` module
  reference so assignments and appends share one object set across modules.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

import orjson

from kahin import _state as state
from kahin._healer import get_healer
from kahin.residual_self.fate import FateDB
from kahin.the_twins.mirage import Mirage
from kahin.the_source.architect import SchemaEngine

logger = logging.getLogger(__name__)

_healer_ref = get_healer()

_schema: SchemaEngine | None = None
_fate: FateDB | None = None


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


_RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_RW = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
_DW = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}

_MIRAGE_PAGE_DOMAINS = ("Page", "Runtime", "Network", "Input", "Accessibility", "Heap")
_MIRAGE_HEALTH_TIMEOUT = 5.0


def _needs_mirage_page(domain: str, command: str) -> bool:
    """Whether a CDP-looking operation needs a current Mirage tab."""
    if domain not in _MIRAGE_PAGE_DOMAINS:
        return False
    # Closing a tab must report the real missing-target error; it must not
    # create a fresh tab just so it can immediately close it.
    return not (domain == "Page" and command == "close")


async def _safe_cdp(domain: str, command: str, params: dict[str, Any] | None = None) -> str:
    """Execute CDP with error handling + auto-education. Returns JSON string.

    Engine-agnostic: Mirage (Juggler) folds to ``call("Domain.command")``,
    CDP engines keep ``send_cdp(domain, command)``.
    """
    err = await _require_engine()
    if err:
        return err
    engine = state._current_engine
    try:
        if isinstance(engine, Mirage):
            if _needs_mirage_page(domain, command):
                await engine.ensure_page()
            result = await engine.execute_cdp(domain, command, params or {})
        else:
            result = await engine.send_cdp(domain, command, params or {})  # type: ignore[union-attr]
        return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()
    except RuntimeError as e:
        msg = str(e)
        correction = _get_schema().error_decode(error_code=-32601, error_message=f"'{domain}.{command}' not found")
        return orjson.dumps({
            "error": f"CDP error: {msg}",
            "hint": correction.get("common_causes", []) + correction.get("solutions", []),
        }, option=orjson.OPT_INDENT_2).decode()
    except Exception as e:
        msg = str(e)
        return orjson.dumps({
            "error": f"Connection lost: {msg}",
            "hint": "Browser engine may have crashed. Use kahin_browser_stop then kahin_browser_start to restart.",
        }).decode()


async def _require_engine() -> str | None:
    """Ensure engine is running and alive. Returns error message or None.

    A dead engine stays reachable until ``browser_stop`` or ``browser_start``
    reaps it. The transport is unusable, but discarding the object here would
    make explicit cleanup impossible when Firefox dies before its sidecar.
    """
    engine = state._current_engine
    if engine is None:
        return "No browser engine running. Use kahin_browser_start first."
    if isinstance(engine, Mirage):
        try:
            health = await asyncio.wait_for(engine.health(), timeout=_MIRAGE_HEALTH_TIMEOUT)
        except asyncio.TimeoutError:
            engine._mark_dead()
            state.clear_state()
            return "Browser engine health check timed out. Use kahin_browser_stop, then kahin_browser_start to restart."
        if not health.get("alive"):
            state.clear_state()
            return "Browser engine is dead (crashed). Use kahin_browser_stop, then kahin_browser_start to restart."
        return None
    if not engine.is_alive():
        state.clear_state()
        return "Browser engine is dead (crashed). Use kahin_browser_stop, then kahin_browser_start to restart."
    return None


# --- Mirage (Juggler) tool plumbing ---------------------------------------


async def _require_mirage() -> str | None:
    """Mirage engine required: returns an error string, or None when fine."""
    err = await _require_engine()
    if err:
        return err
    if not isinstance(state._current_engine, Mirage):
        return (
            "This tool requires the mirage/camoufox (Juggler) engine. "
            "Start it with kahin_browser_start(engine='mirage')."
        )
    return None


def _mirage_engine() -> Mirage:
    """The running Mirage instance — caller must have checked _require_mirage."""
    return state._current_engine  # type: ignore[return-value]


async def _mirage_call(method: str, params: dict[str, Any] | None = None) -> str:
    """Run one Juggler method through Mirage.call(); answer is pretty JSON."""
    err = await _require_mirage()
    if err:
        return err
    try:
        domain, _, command = method.partition(".")
        engine = _mirage_engine()
        if _needs_mirage_page(domain, command):
            await engine.ensure_page()
        result = await engine.call(method, params or {})
        return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()
    except RuntimeError as e:
        return orjson.dumps({
            "error": f"Juggler call failed: {e}",
            "hint": "Check the engine with kahin_engine_health.",
        }, option=orjson.OPT_INDENT_2).decode()
    except Exception as e:
        return orjson.dumps({
            "error": f"Connection lost: {e}",
            "hint": "Browser engine may have crashed. Use kahin_browser_stop then kahin_browser_start.",
        }).decode()


async def _mirage_eval_result(expression: str, frame_id: str | None = None) -> dict[str, Any] | str:
    """Run an expression in a frame's main world; raw Juggler result dict.

    Default (frame_id=None) keeps the historical path: Runtime.evaluate,
    whose sidecar handler always resolves the MAIN frame's context. With a
    frame_id the context is resolved from the Mirage frame->context map and
    the expression runs via Runtime.callFunction — a passthrough method that
    accepts an explicit executionContextId (the browser's Juggler dispatcher
    requires it) — so it executes inside that frame's own world.
    Returns a JSON error string on engine/context failure.
    """
    err = await _require_mirage()
    if err:
        return err
    engine = _mirage_engine()
    await engine.ensure_page()
    method = "Runtime.evaluate"
    params: dict[str, Any] = {"expression": expression}
    if frame_id is not None:
        ctx_id = engine.resolve_context(frame_id)
        if ctx_id is None:
            return orjson.dumps({
                "error": f"no execution context for frame {frame_id}; "
                "list frames with kahin_mirage_frame_tree",
            }, option=orjson.OPT_INDENT_2).decode()
        method = "Runtime.callFunction"
        params = {
            "executionContextId": ctx_id,
            "functionDeclaration": "function(expr) { return eval(expr); }",
            "args": [{"value": expression}],
            "returnByValue": True,
        }
    try:
        return await engine.call(method, params)
    except RuntimeError as e:
        return orjson.dumps({"error": f"Juggler evaluate failed: {e}"}, option=orjson.OPT_INDENT_2).decode()
    except Exception as e:
        return orjson.dumps({"error": f"Connection lost: {e}"}).decode()


async def _mirage_evaluate(expression: str, frame_id: str | None = None) -> str:
    """Evaluate an expression (optionally in a specific frame's main world);
    returns result.value as pretty JSON."""
    result = await _mirage_eval_result(expression, frame_id)
    if isinstance(result, str):
        return result
    if result.get("exceptionDetails"):
        return orjson.dumps({
            "error": "evaluate threw",
            "exception": result["exceptionDetails"],
        }, option=orjson.OPT_INDENT_2).decode()
    return orjson.dumps((result.get("result") or {}).get("value"), option=orjson.OPT_INDENT_2).decode()
