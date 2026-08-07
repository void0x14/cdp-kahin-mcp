"""stealth_mirage.py — Stealth & anti-detect tools (Faz 3).

Task 1: read-only self-audit probe (`kahin_stealth_audit`). Humanized
input, identity pins, proxy/geo sync and the fingerprint report land in
later Faz 3 tasks.
"""

from __future__ import annotations

import orjson

from kahin._mcp import mcp
from kahin.stealth import STEALTH_PROBE_JS, score_checks
from kahin.tools._common import _RO, _healer_ref
from kahin.tools.pilot_mirage import (
    _capture_page_session,
    _json_error,
    _safe_mirage_eval_result,
)


@mcp.tool(name="kahin_stealth_audit", annotations=_RO)
async def stealth_audit(frame_id: str | None = None) -> str:
    """Mirage: run the read-only stealth probe package on the current page.
    Returns {audited, engine, score: {passed, total, ratio}, checks:
    [{check, passed, detail}]}. A low ratio pinpoints leak vectors to fix;
    every check is always reported, unknown probes fail with a detail."""
    async with _healer_ref.safe("kahin_stealth_audit", frame_id=frame_id):
        session_id, capture_error = await _capture_page_session("kahin_stealth_audit")
        if capture_error:
            return capture_error
        assert session_id is not None
        result = await _safe_mirage_eval_result(
            "kahin_stealth_audit", STEALTH_PROBE_JS, frame_id, session_id=session_id,
        )
        if isinstance(result, str):
            return result
        if result.get("exceptionDetails"):
            return _json_error(
                "kahin_stealth_audit",
                "JavaScript evaluation failed",
                "javascript_error",
            )
        value = result.get("result") or {}
        parsed = value.get("value") if isinstance(value, dict) else None
        if not isinstance(parsed, dict):
            return _json_error(
                "kahin_stealth_audit",
                "probe returned no value",
                "invalid_engine_response",
            )
        checks = parsed.get("checks")
        if not isinstance(checks, list):
            return _json_error(
                "kahin_stealth_audit",
                "probe returned no checks list",
                "invalid_engine_response",
            )
        return orjson.dumps({
            "audited": True,
            "engine": "mirage",
            "score": score_checks(checks),
            "checks": checks,
        }, option=orjson.OPT_INDENT_2).decode()
