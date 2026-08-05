"""oracle.py — MCP Server (Kahin'in Sesi).

Bootstrap only: the FastMCP instance, engine lifecycle glue (event
collectors) and ``main()``. All 32 tools live in engine-separated category
modules under :mod:`kahin.tools`, imported below for side-effect
``@mcp.tool`` registration.
"""

from __future__ import annotations

import time

from kahin import _state as state
from kahin._mcp import mcp  # noqa: F401  (re-exported for the old import path)
from kahin.the_twins.chassis import EventData


def _on_cdp_event(evt: EventData) -> None:
    state._current_event_log.append({"event": evt.method, "params": evt.params, "session_id": evt.session_id})


def _on_network_event(evt: EventData) -> None:
    if evt.method.startswith("Network."):
        state._network_requests.append({
            "event": evt.method.replace("Network.", ""),
            "params": evt.params,
            "timestamp": time.time(),
        })


def _on_console_event(evt: EventData) -> None:
    if evt.method == "Runtime.console":
        args = [a.get("value") for a in evt.params.get("args", []) if isinstance(a, dict)]
        state._console_messages.append({
            "type": evt.params.get("type"),
            "args": args,
            "location": evt.params.get("location"),
            "session_id": evt.session_id,
        })


def _on_engine_death(engine) -> None:
    """Reader EOF: mark the engine dead but keep it reachable for cleanup.

    ``browser_stop`` must still be able to reap a sidecar whose Firefox child
    disappeared first. Dropping the reference here made the next stop call
    return ``No engine running`` and left lifecycle cleanup with no owner.
    ``_require_engine`` and ``browser_start`` already reject/reap dead engines;
    keeping the reference lets those paths, or an explicit stop, do so.
    """
    if state._current_engine is engine:
        state.clear_state()


def main() -> None:
    mcp.run(transport="stdio")


# Register the engine-separated category tool modules (side-effect @mcp.tool).
import kahin.tools  # noqa: E402,F401


if __name__ == "__main__":
    main()
