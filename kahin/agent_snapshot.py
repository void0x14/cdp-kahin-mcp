"""agent_snapshot.py — DOM-stream snapshot -> AI-agent observation format.

Turns the live DOM snapshot (nodeId-carrying tree from the page) into the
compact line format agents consume, with a hard token budget. The budget
is a character/4 estimate, always reported, and truncation is always
explicit — an agent is never silently shown a partial page.

Line format (Playwright-MCP-like):

    - button "Add to cart" [ref=n9] [click]
    - textbox "Search" [ref=n3] [focus, type]

refs are the LIVE nodeIds of the DOM stream, so every line can be acted on
with kahin_mirage_dom_action without re-resolving a selector.
"""

from __future__ import annotations

from typing import Any

_DEFAULT_MAX_TOKENS = 1500
_MAX_LINE_TEXT = 60
_TOKEN_PER_CHAR = 4


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / _TOKEN_PER_CHAR))


def _label_of(node: dict[str, Any]) -> str:
    """Display label: name first, falling back to text, capped and one-line."""
    raw = str(node.get("name") or node.get("text") or "").strip().replace("\n", " ")
    if len(raw) > _MAX_LINE_TEXT:
        raw = raw[:_MAX_LINE_TEXT] + "…"
    return raw


def _emittable(node: dict[str, Any]) -> bool:
    """Hidden or empty (no name/text/action) nodes never become lines."""
    if node.get("visible") is False:
        return False
    return bool(_label_of(node) or node.get("actions"))


def _line_for(node: dict[str, Any], depth: int) -> str:
    indent = "  " * depth
    role = node.get("role") or node.get("tag") or "element"
    label = _label_of(node)
    flags: list[str] = []
    ref = str(node.get("nodeId") or "")
    if ref:
        flags.append(f"[ref={ref}]")
    actions = node.get("actions") or []
    if actions:
        flags.append("[" + ", ".join(str(action) for action in actions) + "]")
    suffix = " ".join(flags)
    quoted = f'"{label}"' if label else ""
    return f"{indent}- {role} {quoted} {suffix}".rstrip()


def _count_tree(tree: dict[str, Any]) -> tuple[int, int, bool]:
    """Count nodeId-carrying nodes, emittable nodes, and structural markers.

    A child dict without ``nodeId`` is a depth/node-limit marker from the
    DOM stream (``{"truncated": true, "childCount": N}``), not a real node.
    """
    node_count = 0
    emittable_count = 0
    marker_found = False
    stack: list[dict[str, Any]] = [tree]
    while stack:
        node = stack.pop()
        if "nodeId" not in node:
            marker_found = True
        else:
            node_count += 1
            if _emittable(node):
                emittable_count += 1
        stack.extend(child for child in (node.get("children") or []) if isinstance(child, dict))
    return node_count, emittable_count, marker_found


def _walk(tree: dict[str, Any], depth: int, budget: int) -> tuple[list[str], int]:
    """Collect lines depth-first until the token budget is spent.

    Empty/hidden containers are skipped without adding indentation, so a bare
    wrapper never inflates the agent's view.
    """
    lines: list[str] = []
    shown = 0
    total_chars = 0
    stack: list[tuple[dict[str, Any], int]] = [(tree, depth)]
    while stack and total_chars < budget:
        node, level = stack.pop()
        children = [child for child in (node.get("children") or []) if isinstance(child, dict)]
        if not _emittable(node):
            # Flatten: children of a skipped container keep its level.
            for child in reversed(children):
                stack.append((child, level))
            continue
        line = _line_for(node, level)
        if total_chars + len(line) > budget:
            # Never overshoot: a line that does not fit is left unshown and
            # counts as truncation.
            break
        lines.append(line)
        shown += 1
        total_chars += len(line)
        for child in reversed(children):
            stack.append((child, level + 1))
    return lines, shown


def format_snapshot(tree: dict[str, Any], *, max_tokens: int = _DEFAULT_MAX_TOKENS) -> dict[str, Any]:
    """Format a DOM-stream snapshot into agent lines with token accounting."""
    budget = max(1, min(int(max_tokens) * _TOKEN_PER_CHAR, 10_000_000))
    node_count, emittable_count, marker_found = _count_tree(tree)
    lines, shown = _walk(tree, 0, budget)
    truncated = marker_found or shown < emittable_count
    return {
        "lines": lines,
        "tokens_estimate": _estimate_tokens("\n".join(lines)) if lines else 0,
        "max_tokens": int(max_tokens),
        "truncated": truncated,
        "nodeCount": node_count,
        "shownCount": shown,
    }
