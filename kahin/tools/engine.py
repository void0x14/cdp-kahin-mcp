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
            })
        if isinstance(engine, Mirage):
            try:
                result = await asyncio.wait_for(engine.health(), timeout=5.0)
            except Exception as e:  # noqa: BLE001
                engine._mark_dead()
                return _dump({
                    "engine": "mirage",
                    "alive": False,
                    "error": f"Browser.health failed: {e}",
                })
            alive = bool(result.get("alive"))
            return _dump({
                "engine": "mirage",
                "alive": alive,
                "capabilities": capabilities_for("mirage"),
                "health": result,
            })
        proc = engine._process  # BrowserEngine declares _process on the base class
        pid = proc.pid if proc is not None and proc.poll() is None else None
        engine_name = "shadow" if isinstance(engine, Obscura) else type(engine).__name__.lower()
        return _dump({
            "engine": engine_name,
            "alive": engine.is_alive(),
            "capabilities": capabilities_for(engine_name),
            "pid": pid,
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
            })
        started = getattr(engine, "_started_monotonic", None)
        uptime_s = round(time.monotonic() - started, 3) if isinstance(started, float) else 0.0
        engine_name = "shadow" if isinstance(engine, Obscura) else "mirage"
        payload: dict[str, Any] = {
            "engine": engine_name,
            "uptime_s": uptime_s,
            **perf,
        }
        if isinstance(engine, Mirage):
            prewarm = getattr(engine, "_prewarm_info", None)
            if prewarm is not None:
                payload["prewarm"] = prewarm
        return _dump(payload)


def _dump(payload: dict) -> str:
    return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()
