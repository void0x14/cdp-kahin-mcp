"""agent_mirage.py — Agent-native observation tools (Faz 2, Task 2).

``kahin_mirage_snapshot`` wraps the live DOM snapshot (nodeId-carrying tree)
into the compact ref-carrying line format agents act on, with explicit token
accounting. Every emitted ref is a live DOM nodeId valid for
``kahin_mirage_dom_action`` until the document/frame lifecycle ends; after a
navigation or reset the agent must request a fresh snapshot before acting.
"""

from __future__ import annotations

from typing import Any

import orjson

from kahin._mcp import mcp
from kahin.agent_snapshot import format_snapshot
from kahin.tools._common import _RO, _healer_ref
from kahin.tools.dom_stream_mirage import mirage_dom_snapshot
from kahin.tools.pilot_mirage import (
    _MAX_SELECTOR_LENGTH,
    _bounded_int,
    _json_error,
    _text_arg,
)

_MAX_TOKEN_BUDGET = 100_000


def _loads(text: str) -> Any:
    return orjson.loads(text)


@mcp.tool(name="kahin_mirage_snapshot", annotations=_RO)
async def mirage_snapshot(
    selector: str | None = None,
    max_tokens: int = 1500,
    include_hidden: bool = False,
    frame_id: str | None = None,
) -> str:
    """Mirage: agent-ready page observation with live DOM refs.

    Returns compact lines in the form ``- button "Add" [ref=n9] [click]``.
    Every ``ref`` is a live DOM nodeId that ``kahin_mirage_dom_action`` can
    act on without re-resolving a selector. ``max_tokens`` caps the output;
    ``tokens_estimate``/``truncated`` are always reported so truncation is
    never silent. Snapshot metadata (``streamId``/``cursor``/``revision``/
    ``url``/``title``/``readyState``/``focused``) is passed through when the
    live DOM stream provides it.
    """
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or not 1 <= max_tokens <= _MAX_TOKEN_BUDGET
    ):
        return _json_error(
            "kahin_mirage_snapshot",
            f"max_tokens must be an integer in 1..{_MAX_TOKEN_BUDGET}",
            "invalid_argument",
            field="max_tokens",
        )
    selector_value: str | None = None
    if selector is not None:
        selector_value, error = _text_arg(
            selector,
            tool="kahin_mirage_snapshot",
            field="selector",
            maximum=_MAX_SELECTOR_LENGTH,
        )
        if error:
            return error
    async with _healer_ref.safe(
        "kahin_mirage_snapshot",
        selector=selector_value[:80] if selector_value else None,
        max_tokens=max_tokens,
        include_hidden=include_hidden,
        frame_id=frame_id,
    ):
        raw_text = await mirage_dom_snapshot(
            selector=selector_value,
            max_nodes=_bounded_int(2000, minimum=1, maximum=5000, default=2000),
            max_depth=_bounded_int(24, minimum=1, maximum=32, default=24),
            include_hidden=include_hidden,
            frame_id=frame_id,
        )
        raw = _loads(raw_text)
        if not isinstance(raw, dict):
            return _json_error(
                "kahin_mirage_snapshot",
                "DOM snapshot returned an invalid payload",
                "invalid_engine_response",
            )
        if raw.get("error"):
            return orjson.dumps(raw, option=orjson.OPT_INDENT_2).decode()
        tree = raw.get("root") or {}
        if not isinstance(tree, dict):
            return _json_error(
                "kahin_mirage_snapshot",
                "DOM snapshot returned no root tree",
                "invalid_engine_response",
            )
        formatted = format_snapshot(tree, max_tokens=max_tokens)
        payload: dict[str, Any] = dict(formatted)
        for key in ("streamId", "cursor", "revision", "url", "title", "readyState", "focused"):
            if key in raw:
                payload[key] = raw[key]
        payload["include_hidden"] = bool(include_hidden)
        return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()
