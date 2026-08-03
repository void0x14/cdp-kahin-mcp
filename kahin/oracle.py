"""oracle.py — MCP Server (Kahin'in Sesi).

Bootstrap only: the FastMCP instance, engine lifecycle glue (event
collectors) and ``main()``. All 32 tools live in engine-separated category
modules under :mod:`kahin.tools`, imported below for side-effect
``@mcp.tool`` registration.
"""

from __future__ import annotations

import time

from mcp.server.fastmcp import FastMCP

from kahin import _state as state
from kahin.the_twins.chassis import EventData

mcp = FastMCP(
    name="kahin",
    instructions="I am the Oracle. Always validate CDP commands before sending. Ports 9222 and 9240 are RESERVED.",
)


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
    if evt.method == "Console.messageAdded":
        state._console_messages.append(evt.params.get("message", {}))


def main() -> None:
    mcp.run(transport="stdio")


# Register the engine-separated category tool modules (side-effect @mcp.tool).
import kahin.tools  # noqa: E402,F401


if __name__ == "__main__":
    main()