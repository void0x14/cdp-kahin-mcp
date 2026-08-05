"""The single shared FastMCP instance.

Why this module exists: ``python -m kahin.oracle`` loads oracle.py as
``__main__``; the tool modules' ``from kahin.oracle import mcp`` would then
re-import ``kahin.oracle`` under its own name, creating a SECOND FastMCP
instance — tools registered on it, while ``main()`` ran the first (empty)
one. Importing ``mcp`` from this dedicated module guarantees exactly one
instance regardless of how the server is launched (``-m``, entry point,
``import``).
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    name="kahin",
    instructions=(
        "I am the Oracle. Always validate CDP commands before sending. "
        "Start one browser and reuse it; repeated kahin_browser_start calls "
        "reuse the active engine. With mirage/camoufox, use tabs ("
        "kahin_mirage_tab_new/switch/close) for separate pages instead of "
        "starting another browser. CDP commands are automatically routed to "
        "their Mirage equivalent when Camoufox is active. Ports 9222 and "
        "9240 are RESERVED. For adaptive Mirage automation, start "
        "kahin_mirage_dom_start, read kahin_mirage_dom_snapshot, then consume "
        "kahin_mirage_dom_events with its streamId/cursor; on reset or dropped "
        "take a fresh snapshot, and use kahin_mirage_dom_action only with a "
        "live nodeId."
    ),
)
