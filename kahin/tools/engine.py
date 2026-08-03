"""engine.py — engine health tool.

Faz 9 Task 3: ``kahin_engine_health`` probes the running engine directly.
Mirage answers with the sidecar-local ``Browser.health`` (never forwarded to
Camoufox); shadow/CDP engines get a lightweight liveness answer from the
engine's own ``is_alive()`` plus connection metadata.
"""

from __future__ import annotations

import orjson

from kahin import _state as state
from kahin.oracle import mcp
from kahin.tools._common import _RO, _healer_ref
from kahin.the_twins.mirage import Mirage


@mcp.tool(name="kahin_engine_health", annotations=_RO)
async def engine_health() -> str:
    """Health of the running browser engine. Mirage: Browser.health (sidecar).
    Returns engine type, alive flag and (for mirage) the health payload."""
    async with _healer_ref.safe("kahin_engine_health"):
        engine = state._current_engine
        if engine is None:
            return _dump({"engine": None, "error": "No browser engine running."})
        if isinstance(engine, Mirage):
            try:
                result = await engine.call("Browser.health")
            except Exception as e:  # noqa: BLE001
                return _dump({
                    "engine": "mirage",
                    "alive": engine.is_alive(),
                    "error": f"Browser.health failed: {e}",
                })
            return _dump({"engine": "mirage", "alive": engine.is_alive(), "health": result})
        proc = engine._process  # BrowserEngine declares _process on the base class
        pid = proc.pid if proc is not None and proc.poll() is None else None
        return _dump({
            "engine": type(engine).__name__.lower(),
            "alive": engine.is_alive(),
            "pid": pid,
        })


def _dump(payload: dict) -> str:
    return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()