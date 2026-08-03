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
    instructions="I am the Oracle. Always validate CDP commands before sending. Ports 9222 and 9240 are RESERVED.",
)
