"""engine.py — engine health tool.

Faz 9 Task 3: ``kahin_engine_health`` probes the running engine directly.
Mirage answers with the sidecar-local ``Browser.health`` (never forwarded to
Camoufox); shadow/CDP engines get a lightweight liveness answer from the
engine's own ``is_alive()`` plus connection metadata.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import orjson

from kahin import _state as state
from kahin._healer import get_tracker
from kahin._mcp import mcp
from kahin.tools._common import _RO, _healer_ref
from kahin.the_twins.capabilities import capabilities_for
from kahin.the_twins.mirage import Mirage
from kahin.the_twins.shadow import Obscura


@mcp.tool(name="kahin_engine_health", annotations=_RO)
async def engine_health() -> str:
    """Health of the running browser engine. Mirage: Browser.health (sidecar).
    Returns engine type, alive flag and (for mirage) the health payload."""
    async with _healer_ref.safe("kahin_engine_health"):
        engine = state._current_engine
        if engine is None:
            return _dump({
                "engine": None,
                "error": "No browser engine running.",
                "code": "engine_unavailable",
                "last_engine_death": state._last_engine_death,
            })
        if isinstance(engine, Mirage):
            try:
                result = await asyncio.wait_for(engine.health(), timeout=5.0)
            except Exception as e:  # noqa: BLE001
                process_alive = (
                    not engine._dead
                    and engine._process is not None
                    and engine._process.returncode is None
                )
                if not process_alive:
                    engine._mark_dead(f"health_error:{type(e).__name__}")
                return _dump({
                    "engine": "mirage",
                    "alive": False,
                    "state": "degraded" if process_alive else "dead",
                    "error": f"Browser.health failed: {e}",
                    "reason": getattr(engine, "_death_reason", None),
                    "stderr_log": str(getattr(engine, "_stderr_path", "")) or None,
                    "last_engine_death": state._last_engine_death,
                })
            alive = bool(result.get("alive"))
            payload: dict[str, Any] = {
                "engine": "mirage",
                "alive": alive,
                "capabilities": capabilities_for("mirage"),
                "health": result,
                "pid": result.get("pid"),
                "stderr_log": str(getattr(engine, "_stderr_path", "")) or None,
                "currentTarget": getattr(engine, "_current_target", None),
                "uptime_s": round(
                    time.monotonic() - engine._started_monotonic, 3
                ) if isinstance(getattr(engine, "_started_monotonic", None), float) else 0.0,
            }
            try:
                payload["tabCount"] = len(await asyncio.wait_for(engine.list_pages(), timeout=1.0))
            except Exception:  # noqa: BLE001 - health remains useful if tab metadata races
                payload["tabCount"] = None
            if not alive:
                payload["last_engine_death"] = state._last_engine_death
            return _dump(payload)
        proc = engine._process  # BrowserEngine declares _process on the base class
        engine_name = "shadow" if isinstance(engine, Obscura) else type(engine).__name__.lower()
        alive = bool(engine.is_alive())
        # Both current engines use asyncio.subprocess.Process. It exposes
        # ``returncode``/``pid`` but not ``subprocess.Popen.poll()``; health
        # must use the engine's transport-aware liveness contract instead of
        # assuming one process implementation.
        pid = proc.pid if proc is not None and alive else None
        return _dump({
            "engine": engine_name,
            "alive": alive,
            "capabilities": capabilities_for(engine_name),
            "pid": pid,
            "last_engine_death": state._last_engine_death if not alive else None,
        })


@mcp.tool(name="kahin_engine_stats", annotations=_RO)
async def engine_stats() -> str:
    """Bounded per-tool performance statistics for the running engine.
    Reports uptime (monotonic), total tool calls/errors, the slowest tools
    (top 10 by average duration) and the most recent error. With no engine
    running it returns a structured ``engine_unavailable`` payload instead
    of raising."""
    async with _healer_ref.safe("kahin_engine_stats"):
        perf = get_tracker().engine_stats()
        engine = state._current_engine
        if engine is None:
            return _dump({
                "engine": None,
                "uptime_s": 0.0,
                **perf,
                "code": "engine_unavailable",
                "hint": "Use kahin_browser_start to boot an engine.",
                "last_engine_death": state._last_engine_death,
            })
        started = getattr(engine, "_started_monotonic", None)
        uptime_s = round(time.monotonic() - started, 3) if isinstance(started, float) else 0.0
        engine_name = "shadow" if isinstance(engine, Obscura) else "mirage"
        alive = bool(engine.is_alive())
        health_state = "running" if alive else "dead"
        if isinstance(engine, Mirage) and alive:
            try:
                health = await asyncio.wait_for(engine.health(), timeout=5.0)
                alive = bool(health.get("alive"))
                health_state = str(health.get("state") or ("running" if alive else "dead"))
            except asyncio.TimeoutError:
                # A slow health probe is degraded evidence, not proof of a
                # dead sidecar. Keep the process lock and make the ambiguity
                # visible to the agent instead of reporting running blindly.
                alive = False
                health_state = "degraded"
            except Exception:
                alive = bool(engine.is_alive())
                health_state = "degraded" if alive else "dead"
        payload: dict[str, Any] = {
            "engine": engine_name,
            "uptime_s": uptime_s,
            "alive": alive,
            "state": health_state,
            **perf,
        }
        if not alive and health_state == "dead":
            payload["last_engine_death"] = state._last_engine_death
            if payload.get("last_error") is None and state._last_engine_death is not None:
                death = state._last_engine_death
                payload["last_error"] = {
                    "tool": "engine",
                    "code": "ENGINE_DEAD",
                    "message": f"Browser engine died: {death.get('reason')}",
                    "time": death.get("timestamp"),
                    "recovery": "RESTART_ENGINE",
                }
        if isinstance(engine, Mirage):
            prewarm = getattr(engine, "_prewarm_info", None)
            if prewarm is not None:
                payload["prewarm"] = prewarm
        return _dump(payload)


def _dump(payload: dict) -> str:
    return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()
