"""accessibility_mirage.py — Mirage (Camoufox/Juggler) accessibility tools.

Gap E: Camoufox's Juggler fork is the ONLY engine here with a real
accessibility tree surface. Upstream Playwright's Firefox Juggler protocol
has no Accessibility domain; CDP has Accessibility.getFullAXTree only for
Chromium. The Camoufox fork added it (vendored Protocol.js:1006-1021):

    Accessibility.getFullAXTree{objectId?} -> {tree: axTypes.AXTree}
    targets: ['page']  -> the call runs on the PAGE session, not root

axTypes.AXTree (vendored Protocol.js:147-174) is a recursive node:
    {role, name, children?: [AXTree], selected?, focused?, pressed?,
     focusable?, haspopup?, required?, invalid?, modal?, editable?, busy?,
     multiline?, readonly?, checked?, expanded?, disabled?, multiselectable?,
     value?, description?, roledescription?, valuetext?, orientation?,
     autocomplete?, keyshortcuts?, level?, tag?, foundObject?}

The tool returns the whole tree with a nodeCount + truncated flag; the raw
AXTree can be huge (every DOM node), so max_nodes caps the JSON payload —
the full count is still reported so the caller knows how much was cut.

No frame_id support: Juggler has no frameId -> element objectId path
(describeNode needs an objectId it only hands out for Runtime objects), so
objectId is intentionally not exposed — the full-page tree is the clean
surface. Nothing is faked: the tree comes straight from the browser.
"""

from __future__ import annotations

from collections import deque
from typing import Any

import orjson

from kahin._mcp import mcp
from kahin.tools._common import _RO, _healer_ref, _mirage_engine, _require_mirage

_MAX_AX_NODES = 5000
_MAX_AX_RESPONSE_BYTES = 8 * 1024 * 1024


def _count(tree: dict[str, Any]) -> int:
    """Total node count of an AXTree without recursion depth risk."""
    total = 0
    stack: list[dict[str, Any]] = [tree]
    while stack:
        node = stack.pop()
        total += 1
        stack.extend(child for child in node.get("children") or [] if isinstance(child, dict))
    return total


def _trim(tree: dict[str, Any], budget: int) -> dict[str, Any]:
    """Copy of the tree keeping at most ``budget`` nodes (BFS order).

    Children beyond the budget are dropped AND stripped from their copies
    (a shallow copy keeps its own subtree alive — the budget must count
    every node reachable from the root). The returned copy never mutates
    the caller's tree.
    """
    root = dict(tree)
    kept = 1
    queue: deque[tuple[dict[str, Any], list[dict[str, Any]]]] = deque([(root, tree.get("children") or [])])
    while queue and kept < budget:
        node, orig_children = queue.popleft()
        copies: list[dict[str, Any]] = []
        for child in orig_children:
            if kept >= budget:
                break
            copy = dict(child)
            copy.pop("children", None)  # subtree stays owned by the parent only
            copies.append(copy)
            kept += 1
            queue.append((copy, child.get("children") or []))
        if copies:
            node["children"] = copies
        elif "children" in node:
            del node["children"]
    return root


@mcp.tool(name="kahin_mirage_accessibility_tree", annotations=_RO)
async def mirage_accessibility_tree(max_nodes: int = 200) -> str:
    """Mirage: full accessibility tree via Accessibility.getFullAXTree
    (Camoufox-only — upstream Playwright Juggler has no Accessibility
    domain). Returns {tree, nodeCount, truncated}: tree is the recursive
    AXTree (role/name/children + state flags), nodeCount the FULL node
    count, truncated true when the returned tree was cut to max_nodes
    (keep it high for complete dumps)."""
    async with _healer_ref.safe("kahin_mirage_accessibility_tree", max_nodes=str(max_nodes)):
        if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or max_nodes <= 0:
            return orjson.dumps({
                "error": "max_nodes must be a positive integer",
                "code": "invalid_argument",
                "field": "max_nodes",
            }, option=orjson.OPT_INDENT_2).decode()
        err = await _require_mirage()
        if err:
            return err
        try:
            engine = _mirage_engine()
            page = await engine.ensure_page()
            session_id = page.get("sessionId") if isinstance(page, dict) else None
            if not isinstance(session_id, str) or not session_id:
                return orjson.dumps({
                    "error": "selected page has no live session",
                    "code": "session_unavailable",
                }, option=orjson.OPT_INDENT_2).decode()
            result = await engine.call("Accessibility.getFullAXTree", {}, session_id=session_id)
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
        tree = result.get("tree")
        if not isinstance(tree, dict):
            return orjson.dumps({
                "error": f"getFullAXTree returned no tree: {result}",
            }, option=orjson.OPT_INDENT_2).decode()
        # The current native protocol returns the complete tree before this
        # tool can trim it. Refuse pathological responses before doing a
        # second unbounded traversal and public serialization.
        try:
            response_bytes = len(orjson.dumps(result))
        except (TypeError, ValueError) as exc:
            return orjson.dumps({
                "error": f"getFullAXTree returned an unserializable tree: {exc}",
                "code": "invalid_native_response",
            }, option=orjson.OPT_INDENT_2).decode()
        if response_bytes > _MAX_AX_RESPONSE_BYTES:
            return orjson.dumps({
                "error": "accessibility tree exceeds Kahin's bounded response size",
                "code": "result_too_large",
                "payloadBytes": response_bytes,
                "maxPayloadBytes": _MAX_AX_RESPONSE_BYTES,
                "hint": "Reduce the page accessibility surface before requesting a full tree.",
            }, option=orjson.OPT_INDENT_2).decode()
        effective_max = min(max_nodes, _MAX_AX_NODES)
        total = _count(tree)
        # Clamping the caller's budget is not itself evidence that the page
        # tree was truncated. A 7-node page requested with max_nodes=100000
        # still returns all 7 nodes even though the safe internal ceiling is
        # 5000. Report truncation only when nodes were actually removed.
        truncated = total > effective_max
        payload = _trim(tree, effective_max) if truncated else tree
        return orjson.dumps({
            "tree": payload,
            "nodeCount": total,
            "truncated": truncated,
            "maxNodes": effective_max,
        }, option=orjson.OPT_INDENT_2).decode()
