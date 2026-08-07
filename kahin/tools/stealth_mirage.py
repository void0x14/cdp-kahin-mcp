"""stealth_mirage.py — Stealth & anti-detect tools (Faz 3).

Task 1: read-only self-audit probe (`kahin_stealth_audit`). Humanized
input tools (Task 3): jittered Bézier mouse travel, humanized clicks and
cadence typing. Task 4: per-domain identity rotation policy
(`kahin_identity_pin`/`unpin`/`pins`/`for_domain`) backed by the bounded
pin store. Proxy/geo sync and the fingerprint report land in later Faz 3
tasks.
"""

from __future__ import annotations

import asyncio

import orjson

from kahin._mcp import mcp
from kahin.humanize import bezier_trajectory, jittered_delay, typing_cadence
from kahin.stealth import (
    STEALTH_PROBE_JS,
    load_pins,
    normalize_domain,
    pin_identity,
    score_checks,
    unpin_identity,
)
from kahin.tools._common import _DW, _RO, _RW, _healer_ref
from kahin.tools.pilot_mirage import (
    _MAX_COORDINATE,
    _MAX_KEY_TEXT_LENGTH,
    _MAX_SELECTOR_LENGTH,
    _MAX_WAIT_TIMEOUT,
    _action_ready,
    _bounded_float,
    _bounded_int,
    _capture_page_session,
    _dispatch_mouse,
    _is_error_response,
    _json_error,
    _key_defaults,
    _safe_mirage_call,
    _safe_mirage_eval_result,
    _strict_float,
    _text_arg,
    get_last_mouse_position,
    mirage_key_text_fast,
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


@mcp.tool(name="kahin_mirage_mouse_trajectory", annotations=_DW)
async def mirage_mouse_trajectory(
    x: float, y: float, steps: int = 24, jitter: float = 2.0, seed: int | None = None,
) -> str:
    """Mirage: move the mouse from its last position to (x, y) along a
    jittered Bézier path — one Page.dispatchMouseEvent mousemove per step.
    Returns {moved, from, to}; humanizes cursor travel between actions.
    Bounds: steps 2..200, jitter 0..20px."""
    x_value, error = _strict_float(
        x, tool="kahin_mirage_mouse_trajectory", field="x",
        minimum=0.0, maximum=_MAX_COORDINATE,
    )
    if error:
        return error
    y_value, error = _strict_float(
        y, tool="kahin_mirage_mouse_trajectory", field="y",
        minimum=0.0, maximum=_MAX_COORDINATE,
    )
    if error:
        return error
    steps_value = _bounded_int(steps, minimum=2, maximum=200, default=24)
    jitter_value = _bounded_float(jitter, minimum=0.0, maximum=20.0, default=2.0)
    async with _healer_ref.safe(
        "kahin_mirage_mouse_trajectory", x=x_value, y=y_value,
        steps=steps_value, jitter=jitter_value, seed=seed,
    ):
        session_id, capture_error = await _capture_page_session("kahin_mirage_mouse_trajectory")
        if capture_error:
            return capture_error
        assert session_id is not None
        start_x, start_y = get_last_mouse_position()
        points = bezier_trajectory(
            start_x, start_y, x_value, y_value,
            steps=steps_value, jitter=jitter_value, seed=seed,
        )
        for px, py in points:
            move = await _dispatch_mouse("mousemove", px, py, button=0, buttons=0, session_id=session_id)
            if _is_error_response(move):
                return move
        return orjson.dumps({
            "moved": len(points), "from": [start_x, start_y], "to": [x_value, y_value],
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_click_humanized", annotations=_DW)
async def mirage_click_humanized(
    selector: str,
    steps: int = 24,
    jitter: float = 2.0,
    click_delay_ms: float = 80.0,
    click_jitter_ms: float = 20.0,
    seed: int | None = None,
    timeout: float = 10.0,
    frame_id: str | None = None,
) -> str:
    """Mirage: humanized click — actionability wait, jittered Bézier mouse
    travel to the element center, then real mousedown, a jittered press
    delay, and mouseup. The DOM click fires exactly like mirage_click's.
    Returns {clicked, moved, from, to, press_delay_ms}."""
    selector_value, error = _text_arg(
        selector, tool="kahin_mirage_click_humanized", field="selector",
        maximum=_MAX_SELECTOR_LENGTH,
    )
    if error:
        return error
    if not selector_value:
        return _json_error(
            "kahin_mirage_click_humanized",
            "selector must not be empty",
            "invalid_argument",
            field="selector",
        )
    steps_value = _bounded_int(steps, minimum=2, maximum=200, default=24)
    jitter_value = _bounded_float(jitter, minimum=0.0, maximum=20.0, default=2.0)
    delay_value = _bounded_float(click_delay_ms, minimum=10.0, maximum=2_000.0, default=80.0)
    delay_jitter = _bounded_float(click_jitter_ms, minimum=0.0, maximum=500.0, default=20.0)
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=10.0)
    async with _healer_ref.safe(
        "kahin_mirage_click_humanized", selector=selector_value[:80], steps=steps_value,
        jitter=jitter_value, click_delay_ms=delay_value, timeout=timeout_value, frame_id=frame_id,
    ):
        session_id, capture_error = await _capture_page_session("kahin_mirage_click_humanized")
        if capture_error:
            return capture_error
        assert session_id is not None
        ready = await _action_ready(
            "kahin_mirage_click_humanized", selector_value,
            timeout=timeout_value, frame_id=frame_id, session_id=session_id,
        )
        if isinstance(ready, str):
            return ready
        target_x, target_y = ready
        start_x, start_y = get_last_mouse_position()
        points = bezier_trajectory(
            start_x, start_y, target_x, target_y,
            steps=steps_value, jitter=jitter_value, seed=seed,
        )
        for index, (px, py) in enumerate(points):
            if index == len(points) - 1:
                down = await _dispatch_mouse("mousedown", px, py, button=0, buttons=1, session_id=session_id)
                if _is_error_response(down):
                    return down
            else:
                move = await _dispatch_mouse("mousemove", px, py, button=0, buttons=0, session_id=session_id)
                if _is_error_response(move):
                    return move
        await asyncio.sleep(jittered_delay(delay_value, delay_jitter, seed=seed) / 1000.0)
        up = await _dispatch_mouse("mouseup", target_x, target_y, button=0, buttons=0, session_id=session_id)
        if _is_error_response(up):
            return up
        return orjson.dumps({
            "clicked": selector_value, "moved": len(points),
            "from": [start_x, start_y], "to": [target_x, target_y],
            "press_delay_ms": delay_value,
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_key_text", annotations=_RW)
async def mirage_key_text(
    text: str,
    delay_ms: float = 45.0,
    jitter_ms: float = 25.0,
    seed: int | None = None,
    frame_id: str | None = None,
) -> str:
    """Mirage: type text with a humanized per-character cadence. Each char
    is dispatched as real keydown/keyup pairs (Juggler lowercase types,
    browser-native codes via _key_defaults) separated by jittered delays.
    delay_ms=0 disables the cadence and falls back to the fast path; the
    text lands in the currently focused element (use mirage_focus/
    mirage_type for selector-driven typing). Returns {typed, cadence}."""
    text_value, error = _text_arg(
        text, tool="kahin_mirage_key_text", field="text", maximum=_MAX_KEY_TEXT_LENGTH,
    )
    if error:
        return error
    base = _bounded_float(delay_ms, minimum=0.0, maximum=500.0, default=45.0)
    jitter = _bounded_float(jitter_ms, minimum=0.0, maximum=200.0, default=25.0)
    async with _healer_ref.safe(
        "kahin_mirage_key_text", text=text_value[:80],
        delay_ms=base, jitter_ms=jitter, seed=seed, frame_id=frame_id,
    ):
        if base == 0.0:
            return await mirage_key_text_fast(text_value)
        session_id, capture_error = await _capture_page_session("kahin_mirage_key_text")
        if capture_error:
            return capture_error
        assert session_id is not None
        cadence = typing_cadence(len(text_value), base_ms=base, jitter_ms=jitter, seed=seed)
        for index, char in enumerate(text_value):
            code, key_code = _key_defaults(char)
            key_code = _bounded_int(key_code, minimum=0, maximum=65_535, default=0)
            down = await _safe_mirage_call("kahin_mirage_key_text", "Page.dispatchKeyEvent", {
                "type": "keydown", "key": char, "keyCode": key_code,
                "location": 0, "code": code, "repeat": False, "text": char,
            }, session_id=session_id)
            if _is_error_response(down):
                return down
            up = await _safe_mirage_call("kahin_mirage_key_text", "Page.dispatchKeyEvent", {
                "type": "keyup", "key": char, "keyCode": key_code,
                "location": 0, "code": code, "repeat": False,
            }, session_id=session_id)
            if _is_error_response(up):
                return up
            if cadence[index] > 0:
                await asyncio.sleep(cadence[index] / 1000.0)
        return orjson.dumps({"typed": text_value, "cadence": cadence}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_identity_pin", annotations=_RW)
async def identity_pin(domain: str, name: str) -> str:
    """Mirage: pin a saved identity (Faz 2) to a canonical domain so
    rotation is deterministic per site. Only identities that exist in the
    Faz 2 store can be pinned; the map lives at ~/.config/kahin/pins.json.
    Returns {pinned, domain, name}."""
    tool = "kahin_identity_pin"
    domain_value, error = _text_arg(domain, tool=tool, field="domain", maximum=253)
    if error:
        return error
    name_value, error = _text_arg(name, tool=tool, field="name", maximum=64)
    if error:
        return error
    assert domain_value is not None and name_value is not None
    # Lazy import mirrors pilot.py's precedent: agent_mirage pulls the DOM
    # stream tooling, which this module must not load at import time.
    from kahin.tools.agent_mirage import _identity_path  # noqa: PLC0415

    path = _identity_path(name_value)
    if path is None or not path.is_file():
        return _json_error(tool, f"unknown identity: {name_value!r}", "invalid_argument", field="name")
    async with _healer_ref.safe(tool, domain=domain_value, name=name_value):
        normalized = normalize_domain(domain_value)
        if normalized is None:
            return _json_error(tool, "invalid domain", "invalid_argument", field="domain")
        try:
            failed = pin_identity(domain_value, name_value)
        except OSError as exc:
            return _json_error(tool, f"cannot write pins: {exc}", "tool_failed")
        if failed:
            return _json_error(tool, failed["error"], failed["code"], field="domain")
        return orjson.dumps({
            "pinned": True, "domain": normalized, "name": name_value,
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_identity_unpin", annotations=_DW)
async def identity_unpin(domain: str) -> str:
    """Mirage: remove the identity pin for a domain (rotation policy
    change). Destructive only to the pin mapping, never to identities or
    the engine. Returns {unpinned, domain}."""
    tool = "kahin_identity_unpin"
    domain_value, error = _text_arg(domain, tool=tool, field="domain", maximum=253)
    if error:
        return error
    assert domain_value is not None
    async with _healer_ref.safe(tool, domain=domain_value):
        normalized = normalize_domain(domain_value)
        if normalized is None:
            return _json_error(tool, "invalid domain", "invalid_argument", field="domain")
        try:
            failed = unpin_identity(domain_value)
        except OSError as exc:
            return _json_error(tool, f"cannot write pins: {exc}", "tool_failed")
        if failed:
            return _json_error(tool, failed["error"], failed["code"], field="domain")
        return orjson.dumps({
            "unpinned": True, "domain": normalized,
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_identity_pins", annotations=_RO)
async def identity_pins() -> str:
    """Mirage: list the current per-domain identity pin map
    ({pins: {domain: name}}). Read-only."""
    async with _healer_ref.safe("kahin_identity_pins"):
        return orjson.dumps({"pins": load_pins()}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_identity_for_domain", annotations=_RO)
async def identity_for_domain(domain: str) -> str:
    """Mirage: report which saved identity is pinned to a domain, plus an
    actionable next step ({domain, name|null, hint}). Read-only."""
    tool = "kahin_identity_for_domain"
    domain_value, error = _text_arg(domain, tool=tool, field="domain", maximum=253)
    if error:
        return error
    assert domain_value is not None
    async with _healer_ref.safe(tool, domain=domain_value):
        normalized = normalize_domain(domain_value)
        if normalized is None:
            return _json_error(tool, "invalid domain", "invalid_argument", field="domain")
        name = load_pins().get(normalized)
        hint = (
            f"start with: kahin_browser_start(identity={name!r})"
            if name
            else "no pin — rotate freely"
        )
        return orjson.dumps({
            "domain": normalized, "name": name, "hint": hint,
        }, option=orjson.OPT_INDENT_2).decode()
