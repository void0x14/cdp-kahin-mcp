"""agent_mirage.py — Agent-native observation tools (Faz 2, Tasks 2-3).

``kahin_mirage_snapshot`` wraps the live DOM snapshot (nodeId-carrying tree)
into the compact ref-carrying line format agents act on, with explicit token
accounting. Every emitted ref is a live DOM nodeId valid for
``kahin_mirage_dom_action`` until the document/frame lifecycle ends; after a
navigation or reset the agent must request a fresh snapshot before acting.
``kahin_mirage_fill_form`` types several live fields by ref in one call.
"""

from __future__ import annotations

from typing import Any

import orjson

from kahin._mcp import mcp
from kahin.agent_snapshot import format_snapshot
from kahin.tools._common import _RO, _RW, _healer_ref
from kahin.tools.dom_stream_mirage import mirage_dom_action, mirage_dom_snapshot
from kahin.tools.pilot_mirage import (
    _MAX_SELECTOR_LENGTH,
    _MAX_WAIT_TIMEOUT,
    _bounded_float,
    _bounded_int,
    _json_error,
    _text_arg,
)

_MAX_TOKEN_BUDGET = 100_000
_FILL_FIELDS_MAX = 100


def _loads(text: str) -> Any:
    return orjson.loads(text)


@mcp.tool(name="kahin_mirage_fill_form", annotations=_RW)
async def mirage_fill_form(
    fields: list[dict[str, Any]],
    timeout: float = 10.0,
    frame_id: str | None = None,
) -> str:
    """Mirage: fill several live fields in one call.

    ``fields`` is a list of ``{ref, text}`` objects; each ``ref`` must be a
    live nodeId from ``kahin_mirage_snapshot``/``kahin_mirage_dom_snapshot``.
    Every field is typed through ``kahin_mirage_dom_action(action="type")`` so
    no stale selector is ever trusted. Returns ``{filled, results[]}`` where
    each result is the corresponding dom_action payload; a field that went
    stale returns ``requiresSnapshot: true`` and never pretends the form
    completed.
    """
    if not isinstance(fields, list) or not fields or len(fields) > _FILL_FIELDS_MAX:
        return _json_error(
            "kahin_mirage_fill_form",
            f"fields must be a list of 1..{_FILL_FIELDS_MAX} items",
            "invalid_argument",
            field="fields",
        )
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=10.0)
    async with _healer_ref.safe(
        "kahin_mirage_fill_form", count=len(fields), timeout=timeout_value, frame_id=frame_id,
    ):
        results: list[dict[str, Any]] = []
        for field in fields:
            if not isinstance(field, dict):
                return _json_error(
                    "kahin_mirage_fill_form",
                    "each field must be an object {ref, text}",
                    "invalid_argument",
                )
            ref = field.get("ref")
            text = field.get("text")
            if not isinstance(ref, str) or not isinstance(text, str):
                return _json_error(
                    "kahin_mirage_fill_form",
                    "each field needs a string ref and a string text",
                    "invalid_argument",
                )
            outcome = _loads(await mirage_dom_action(node_id=ref, action="type", text=text, frame_id=frame_id))
            results.append(outcome)
            if not isinstance(outcome, dict):
                return _json_error(
                    "kahin_mirage_fill_form",
                    "a field action returned an invalid payload",
                    "invalid_engine_response",
                    filled=len(results) - 1,
                )
            if outcome.get("requiresSnapshot"):
                return orjson.dumps({
                    "error": "a field went stale mid-form",
                    "code": "stale_node",
                    "requiresSnapshot": True,
                    "filled": len(results) - 1,
                    "results": results,
                }, option=orjson.OPT_INDENT_2).decode()
            if outcome.get("error"):
                return _json_error(
                    "kahin_mirage_fill_form",
                    str(outcome["error"]),
                    str(outcome.get("code") or "tool_error"),
                    filled=len(results) - 1,
                )
        return orjson.dumps({"filled": len(results), "results": results}, option=orjson.OPT_INDENT_2).decode()


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
