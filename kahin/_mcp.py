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
from pathlib import Path

from kahin import __version__


_DOC_ROOT = Path(__file__).with_name("_docs")


def _packaged_doc(name: str) -> str:
    path = _DOC_ROOT / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"# Kahin documentation unavailable\n\n{type(exc).__name__}: {exc}"

mcp = FastMCP(
    name="kahin",
    instructions=(
        "I am the Oracle. Use the built-in documentation tools instead of "
        "searching the source tree: kahin_find_concept/kahin_get_command/"
        "kahin_get_event/kahin_get_type answer CDP questions, and "
        "kahin_validate_command must pass before any raw CDP call. On a "
        "validation error, use kahin_error_decode or the returned "
        "correction; do not guess method names or schemas. "
        "Start one browser and reuse it; kahin_browser_start defaults to the "
        "complete Camoufox/Mirage engine, and repeated start calls "
        "reuse the active engine. With mirage/camoufox, use tabs ("
        "kahin_mirage_tab_new/switch/close) for separate pages instead of "
        "starting another browser. CDP commands are automatically routed to "
        "their Mirage equivalent when Camoufox is active. If Shadow was "
        "explicitly selected, visual capabilities promote the live page to "
        "Mirage inside Kahin; never leave Kahin for another automation library. "
        "Ports 9222 and "
        "9240 are RESERVED. Check kahin_agent_status/kahin_engine_health after "
        "failures. Before crawling, call kahin_challenge_status and stop or back off when it "
        "reports captcha, rate_limit, or access_denied; it never bypasses a "
        "challenge. For adaptive Mirage automation, start "
        "kahin_mirage_dom_start, read kahin_mirage_dom_snapshot, then consume "
        "kahin_mirage_dom_events with its streamId/cursor; on reset or dropped "
        "take a fresh snapshot, and use kahin_mirage_dom_action only with a "
        "live nodeId. For long-running authorized crawls use "
        "kahin_crawl_start/status/results/pause/resume/stop: it reuses one "
        "Mirage browser/tab, rotates fresh launch identity only at page "
        "boundaries, honors bounded Retry-After backoff, and pauses on "
        "CAPTCHA/access-denied. The crawler never bypasses challenges."
    ),
)

# FastMCP currently derives serverInfo.version from the MCP SDK when no
# version is supplied. Override that fallback with Kahin's own package
# version so clients can identify the runtime they are actually connected to.
mcp._mcp_server.version = __version__


@mcp.resource("kahin://docs/usage")
def usage_documentation() -> str:
    """Packaged Kahin usage contract available through MCP resources."""
    return _packaged_doc("README.md")


@mcp.resource("kahin://docs/juggler-ai-native")
def juggler_ai_native_documentation() -> str:
    """Packaged Mirage/Juggler agent contract available through MCP."""
    return _packaged_doc("juggler-ai-native.md")
