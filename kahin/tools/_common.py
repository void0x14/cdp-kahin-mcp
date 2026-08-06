"""_common.py — shared plumbing for the kahin tool modules.

Engine-agnostic helpers moved out of kahin/oracle.py so tool category
modules only carry their own ``@mcp.tool`` registrations.

NOTES
- The ``mcp`` instance does NOT live here — it lives in ``kahin/oracle.py``;
  tool modules do ``from kahin._mcp import mcp``.
- Mutable runtime state (``_current_engine``, event/network/console buffers)
  lives in ``kahin._state``; tools reach it through the ``state`` module
  reference so assignments and appends share one object set across modules.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any
from urllib.parse import urlparse

import orjson

from kahin import _state as state
from kahin._healer import get_healer
from kahin.residual_self.fate import FateDB
from kahin.the_twins.capabilities import requires_mirage
from kahin.the_twins.mirage import Mirage
from kahin.the_twins.shadow import Obscura
from kahin.the_source.architect import SchemaEngine

logger = logging.getLogger(__name__)

_healer_ref = get_healer()
_healer_ref.bind_state(state)

_schema: SchemaEngine | None = None
_fate: FateDB | None = None


def _get_schema() -> SchemaEngine:
    global _schema
    if _schema is None:
        s = SchemaEngine()
        s.load()
        _schema = s
    return _schema


def _get_fate() -> FateDB:
    global _fate
    if _fate is None:
        _fate = FateDB()
    return _fate


async def _auto_learn(domain: str, command: str, params: dict[str, Any] | None = None) -> None:
    """Auto-record a CDP pattern to FateDB."""
    try:
        url = (params or {}).get("url", "")
        ctx = ""
        if url:
            parsed = urlparse(url)
            ctx = parsed.hostname or "unknown"
        _get_fate().learn(domain, command, params or {}, context=ctx)
    except Exception as e:
        logger.warning("auto_learn failed for %s.%s: %s", domain, command, e)


_RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_RW = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
_DW = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}

_MIRAGE_PAGE_DOMAINS = ("Page", "Runtime", "Network", "Input", "Accessibility", "Heap")
_MIRAGE_HEALTH_TIMEOUT = 5.0
_MIRAGE_PROMOTE_TIMEOUT = 60.0
_MIRAGE_STOP_TIMEOUT = 15.0
_MAX_TOOL_PAYLOAD_BYTES = 16 * 1024 * 1024


def _needs_mirage_page(domain: str, command: str) -> bool:
    """Whether a CDP-looking operation needs a current Mirage tab."""
    if domain not in _MIRAGE_PAGE_DOMAINS:
        return False
    # Closing a tab must report the real missing-target error; it must not
    # create a fresh tab just so it can immediately close it.
    return not (domain == "Page" and command == "close")


async def _safe_cdp(domain: str, command: str, params: dict[str, Any] | None = None) -> str:
    """Execute CDP with error handling + auto-education. Returns JSON string.

    Engine-agnostic: Mirage (Juggler) folds to ``call("Domain.command")``,
    CDP engines keep ``send_cdp(domain, command)``.
    """
    err = await _require_engine()
    if err:
        try:
            parsed = orjson.loads(err)
        except orjson.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return orjson.dumps(parsed, option=orjson.OPT_INDENT_2).decode()
        return orjson.dumps({
            "error": err,
            "code": "engine_unavailable",
            "hint": "Use kahin_engine_health, then kahin_browser_start.",
        }, option=orjson.OPT_INDENT_2).decode()
    engine = state._current_engine
    try:
        if isinstance(engine, Obscura) and requires_mirage(domain, command):
            promoted = await _promote_shadow_to_mirage()
            if isinstance(promoted, str):
                return promoted
            engine = promoted
        if isinstance(engine, Mirage):
            page_session_id = None
            if _needs_mirage_page(domain, command):
                page = await engine.ensure_page()
                page_session_id = page.get("sessionId")
            result = await engine.execute_cdp(
                domain, command, params or {}, session_id=page_session_id,
            )
        else:
            result = await engine.send_cdp(domain, command, params or {})  # type: ignore[union-attr]
        encoded = orjson.dumps(result, option=orjson.OPT_INDENT_2)
        if len(encoded) > _MAX_TOOL_PAYLOAD_BYTES:
            return orjson.dumps({
                "error": "CDP result exceeds Kahin's bounded tool payload",
                "code": "result_too_large",
                "method": f"{domain}.{command}",
                "payloadBytes": len(encoded),
                "maxPayloadBytes": _MAX_TOOL_PAYLOAD_BYTES,
                "hint": "Narrow the query, page, event limit, or screenshot viewport.",
            }, option=orjson.OPT_INDENT_2).decode()
        return encoded.decode()
    except RuntimeError as e:
        msg = str(e)
        lowered = msg.lower()
        if "not supported" in lowered or "method not found" in lowered:
            engine_name = "mirage" if isinstance(engine, Mirage) else "shadow"
            return orjson.dumps({
                "error": f"{domain}.{command} is not supported by the active {engine_name} adapter",
                "code": "unsupported_on_engine",
                "engine": engine_name,
                "method": f"{domain}.{command}",
                "hint": [
                    "Use kahin_find_concept/kahin_get_command to inspect the supported surface.",
                    "Use the native kahin_mirage_* tool when one exists; no external automation fallback is used.",
                ],
            }, option=orjson.OPT_INDENT_2).decode()
        correction = _get_schema().error_decode(error_code=-32601, error_message=f"'{domain}.{command}' not found")
        return orjson.dumps({
            "error": f"CDP error: {msg}",
            "code": "cdp_command_failed",
            "method": f"{domain}.{command}",
            "hint": correction.get("common_causes", []) + correction.get("solutions", []),
        }, option=orjson.OPT_INDENT_2).decode()
    except Exception as e:
        msg = str(e)
        return orjson.dumps({
            "error": f"Connection lost: {msg}",
            "code": "connection_lost",
            "method": f"{domain}.{command}",
            "hint": "Browser engine may have crashed. Use kahin_browser_stop then kahin_browser_start to restart.",
        }).decode()


async def _require_engine() -> str | None:
    """Ensure engine is running and alive. Returns error message or None.

    A dead engine stays reachable until ``browser_stop`` or ``browser_start``
    reaps it. The transport is unusable, but discarding the object here would
    make explicit cleanup impossible when Firefox dies before its sidecar.
    """
    engine = state._current_engine
    if engine is None:
        return orjson.dumps({
            "error": "No browser engine running. Use kahin_browser_start first.",
            "code": "engine_unavailable",
        }, option=orjson.OPT_INDENT_2).decode()
    if isinstance(engine, Mirage):
        try:
            health = await asyncio.wait_for(engine.health(), timeout=_MIRAGE_HEALTH_TIMEOUT)
        except asyncio.TimeoutError:
            engine._mark_dead()
            return orjson.dumps({
                "error": "Browser engine health check timed out. Use kahin_browser_stop, then kahin_browser_start to restart.",
                "code": "engine_health_timeout",
            }, option=orjson.OPT_INDENT_2).decode()
        if not health.get("alive"):
            return orjson.dumps({
                "error": "Browser engine is dead (crashed). Use kahin_browser_stop, then kahin_browser_start to restart.",
                "code": "engine_dead",
            }, option=orjson.OPT_INDENT_2).decode()
        return None
    if not engine.is_alive():
        return orjson.dumps({
            "error": "Browser engine is dead (crashed). Use kahin_browser_stop, then kahin_browser_start to restart.",
            "code": "engine_dead",
        }, option=orjson.OPT_INDENT_2).decode()
    return None


# --- Mirage (Juggler) tool plumbing ---------------------------------------


async def _require_mirage() -> str | None:
    """Ensure the visual Camoufox backend is active.

    Shadow is an explicit fast opt-in, not a reason for an agent to leave
    Kahin.  When a Mirage-only tool is called on a live Shadow session, move
    that session into Camoufox and preserve its current URL (and a blank-page
    DOM snapshot when there is no navigable URL).
    """
    err = await _require_engine()
    if err:
        return err
    if isinstance(state._current_engine, Obscura):
        promoted = await _promote_shadow_to_mirage()
        if isinstance(promoted, str):
            return promoted
    if not isinstance(state._current_engine, Mirage):
        return orjson.dumps({
            "error": "Capability requires the Camoufox/Mirage engine.",
            "code": "capability_requires_mirage",
            "engine": type(state._current_engine).__name__ if state._current_engine else None,
            "hint": "Kahin could not promote the active browser; inspect kahin_engine_health.",
        }, option=orjson.OPT_INDENT_2).decode()
    return None


def _mirage_engine() -> Mirage:
    """The running Mirage instance — caller must have checked _require_mirage."""
    return state._current_engine  # type: ignore[return-value]


async def _mirage_call(
    method: str,
    params: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> str:
    """Run one Juggler method through Mirage.call(); answer is pretty JSON."""
    err = await _require_mirage()
    if err:
        try:
            parsed = orjson.loads(err)
        except orjson.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return orjson.dumps(parsed, option=orjson.OPT_INDENT_2).decode()
        return orjson.dumps({
            "error": err,
            "code": "engine_unavailable",
            "engine": "mirage",
            "hint": "Use kahin_engine_health, then kahin_browser_start when the engine is unavailable.",
        }, option=orjson.OPT_INDENT_2).decode()
    try:
        domain, _, command = method.partition(".")
        engine = _mirage_engine()
        if _needs_mirage_page(domain, command):
            page = await engine.ensure_page()
            # Pin page-scoped calls to the target selected at the start of
            # this helper. A concurrent tab switch must not redirect a
            # multi-step tool action to another page.
            if session_id is None:
                session_id = page.get("sessionId")
        result = await engine.call(method, params or {}, session_id=session_id)
        return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()
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


async def _promote_shadow_to_mirage() -> Mirage | str:
    """Replace a live Shadow process with Camoufox for a visual capability.

    This is the single in-process handoff used by screenshots, mobile
    emulation, screencast, upload and accessibility tools.  It never invokes
    another automation library and never publishes the new engine until its
    health check, event hooks and page handoff have succeeded.
    """
    current = state._current_engine
    if isinstance(current, Mirage):
        return current
    if not isinstance(current, Obscura):
        return orjson.dumps({
            "error": "No live engine can be promoted to Camoufox/Mirage.",
            "code": "mirage_promotion_unavailable",
        }, option=orjson.OPT_INDENT_2).decode()

    async with state._lifecycle_lock:
        current = state._current_engine
        if isinstance(current, Mirage):
            return current
        if not isinstance(current, Obscura):
            return orjson.dumps({
                "error": "No live Shadow engine can be promoted to Camoufox/Mirage.",
                "code": "mirage_promotion_unavailable",
            }, option=orjson.OPT_INDENT_2).decode()

        try:
            page_state = await _shadow_page_state(current)
        except Exception as exc:  # noqa: BLE001
            return orjson.dumps({
                "error": f"Could not capture the active Shadow page before Camoufox handoff: {exc}",
                "code": "mirage_promotion_snapshot_failed",
                "hint": "The Shadow browser is still active; retry the capability or inspect its health.",
            }, option=orjson.OPT_INDENT_2).decode()

        candidate = Mirage()
        try:
            await asyncio.wait_for(
                candidate.start(headless=True, port=0),
                timeout=_MIRAGE_PROMOTE_TIMEOUT,
            )
            # Import lazily: oracle imports the tool modules during bootstrap,
            # while this function is only called after bootstrap is complete.
            from kahin.oracle import (  # noqa: PLC0415
                _on_cdp_event,
                _on_console_event,
                _on_engine_death,
                _on_network_event,
            )

            await candidate.on_event(_on_cdp_event)
            await candidate.on_event(_on_network_event)
            await candidate.on_event(_on_console_event)
            eng = candidate
            eng.on_death(lambda: _on_engine_death(eng))

            await candidate.ensure_page()
            # Restore HTTP-only/auth cookies before navigation. Without this,
            # a Shadow -> Mirage promotion silently turns an authenticated
            # session into an anonymous one. The CDP and Juggler cookie
            # schemas overlap, but their sameSite spellings do not always;
            # normalize only fields the Juggler schema actually accepts.
            cookies = page_state.get("cookies")
            if isinstance(cookies, list) and cookies:
                await candidate.call("Browser.setCookies", {"cookies": cookies})
            target_url = page_state["url"]
            if target_url and target_url != "about:blank":
                await candidate.execute_cdp("Page", "navigate", {"url": target_url})
            elif page_state["html"]:
                encoded = base64.b64encode(page_state["html"].encode()).decode()
                await candidate.execute_cdp(
                    "Page",
                    "navigate",
                    {"url": f"data:text/html;base64,{encoded}"},
                )
            storage = page_state.get("storage")
            if target_url and target_url != "about:blank" and isinstance(storage, dict):
                encoded_storage = orjson.dumps(storage).decode()
                storage_result = await candidate.execute_cdp(
                    "Runtime",
                    "evaluate",
                    {
                        "expression": (
                            "(() => {"
                            f"const s={encoded_storage};"
                            "for (const [k,v] of Object.entries(s.localStorage || {})) localStorage.setItem(k,v);"
                            "for (const [k,v] of Object.entries(s.sessionStorage || {})) sessionStorage.setItem(k,v);"
                            "return true;"
                            "})()"
                        ),
                        "returnByValue": True,
                    },
                )
                if not isinstance(storage_result, dict) or storage_result.get("exceptionDetails"):
                    raise RuntimeError(f"could not restore Shadow storage: {storage_result}")
        except asyncio.CancelledError:
            try:
                await asyncio.wait_for(candidate.stop(), timeout=_MIRAGE_STOP_TIMEOUT)
            except BaseException:  # noqa: BLE001
                logger.exception("failed to clean up cancelled Mirage promotion")
            raise
        except Exception as exc:  # noqa: BLE001
            try:
                await asyncio.wait_for(candidate.stop(), timeout=_MIRAGE_STOP_TIMEOUT)
            except BaseException:  # noqa: BLE001
                logger.exception("failed to clean up failed Mirage promotion")
            return orjson.dumps({
                "error": f"Camoufox/Mirage promotion failed: {exc}",
                "code": "mirage_promotion_failed",
                "hint": "Kahin did not fall back to an external automation library.",
            }, option=orjson.OPT_INDENT_2).decode()

        # Reap Shadow before publishing Mirage. Publishing first leaves two
        # browsers on cleanup failure and leaves the healer bound to Shadow.
        try:
            await asyncio.wait_for(current.stop(), timeout=_MIRAGE_STOP_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - handoff must fail closed
            try:
                await asyncio.wait_for(candidate.stop(), timeout=_MIRAGE_STOP_TIMEOUT)
            except Exception:
                logger.exception("failed to clean up Mirage after Shadow handoff failure")
            return orjson.dumps({
                "error": f"Shadow cleanup failed during Mirage handoff: {exc}",
                "code": "mirage_handoff_cleanup_failed",
                "hint": "The existing Shadow engine was retained; retry stop/start before using Mirage.",
            }, option=orjson.OPT_INDENT_2).decode()

        # Publish only after Camoufox is healthy, the page is available, and
        # Shadow has been reaped. Keep event/network evidence across the
        # backend handoff.
        state._current_engine = candidate
        _healer_ref.bind_engine(candidate)
        return candidate


async def _shadow_page_state(engine: Obscura) -> dict[str, Any]:
    """Read bounded live state so promotion does not erase a real session."""
    result = await asyncio.wait_for(
        engine.send_cdp(
            "Runtime",
            "evaluate",
            {
                "expression": """(() => {
                    const read = (storage) => {
                        try {
                            const out = {};
                            for (let i = 0; i < Math.min(storage.length, 200); i++) {
                                const key = storage.key(i);
                                if (key !== null) out[String(key).slice(0, 1024)] = String(storage.getItem(key) ?? '').slice(0, 4096);
                            }
                            return out;
                        } catch (_) { return {}; }
                    };
                    return {
                        url: location.href,
                        html: document.documentElement?.outerHTML || '',
                        storage: {localStorage: read(localStorage), sessionStorage: read(sessionStorage)},
                    };
                })()""",
                "returnByValue": True,
            },
        ),
        timeout=10.0,
    )
    value = (result.get("result") or {}).get("value")
    if not isinstance(value, dict):
        raise RuntimeError(f"Runtime.evaluate returned no page state: {result}")
    url = value.get("url")
    html = value.get("html")
    if not isinstance(url, str) or not isinstance(html, str):
        raise RuntimeError("active page state had an invalid URL or HTML snapshot")
    cookies: list[dict[str, Any]] = []
    try:
        cookie_result = await asyncio.wait_for(
            engine.send_cdp("Network", "getAllCookies", {}), timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001 - incomplete auth handoff is unsafe
        raise RuntimeError(f"could not snapshot Shadow cookies: {exc}") from exc
    raw_cookies = cookie_result.get("cookies") if isinstance(cookie_result, dict) else None
    if not isinstance(raw_cookies, list):
        raise RuntimeError("Shadow Network.getAllCookies returned no cookie list")
    for raw in raw_cookies[:500]:
        if not isinstance(raw, dict):
            continue
        name, cookie_value = raw.get("name"), raw.get("value")
        if not isinstance(name, str) or not name or not isinstance(cookie_value, str):
            continue
        cookie: dict[str, Any] = {"name": name[:8192], "value": cookie_value[:8192]}
        for field in ("domain", "path"):
            if isinstance(raw.get(field), str):
                cookie[field] = raw[field][:8192]
        for field in ("secure", "httpOnly"):
            if isinstance(raw.get(field), bool):
                cookie[field] = raw[field]
        same_site = raw.get("sameSite")
        if same_site in {"Strict", "Lax", "None"}:
            cookie["sameSite"] = same_site
        expires = raw.get("expires")
        if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires >= 0:
            cookie["expires"] = expires
        cookies.append(cookie)
    # Keep the handoff bounded.  Navigable URLs retain external resources;
    # the HTML snapshot is only used for about:blank documents.
    storage = value.get("storage")
    if not isinstance(storage, dict):
        storage = {"localStorage": {}, "sessionStorage": {}}
    return {"url": url, "html": html[:5_000_000], "cookies": cookies, "storage": storage}


async def _mirage_eval_result(
    expression: str,
    frame_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any] | str:
    """Run an expression in a frame's main world; raw Juggler result dict.

    Default (frame_id=None) keeps the historical path: Runtime.evaluate,
    whose sidecar handler always resolves the MAIN frame's context. With a
    frame_id the context is resolved from the Mirage frame->context map and
    the expression runs via Runtime.callFunction — a passthrough method that
    accepts an explicit executionContextId (the browser's Juggler dispatcher
    requires it) — so it executes inside that frame's own world.
    Returns a JSON error string on engine/context failure.
    """
    err = await _require_mirage()
    if err:
        # Visual helpers historically leaked this plain liveness sentence
        # through their evaluate path. Keep every Mirage tool machine
        # readable, including the failure before a Juggler call exists.
        try:
            parsed = orjson.loads(err)
        except orjson.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        return orjson.dumps({
            "error": err,
            "code": "engine_unavailable",
            "engine": "mirage",
            "hint": "Use kahin_engine_health, then kahin_browser_start when the engine is unavailable.",
        }, option=orjson.OPT_INDENT_2).decode()
    engine = _mirage_engine()
    page = await engine.ensure_page()
    if session_id is None:
        session_id = page.get("sessionId")
    method = "Runtime.evaluate"
    params: dict[str, Any] = {"expression": expression}
    if frame_id is not None:
        ctx_id = engine.resolve_context(frame_id, session_id=session_id)
        if ctx_id is None:
            return orjson.dumps({
                "error": f"no execution context for frame {frame_id}; "
                "list frames with kahin_mirage_frame_tree",
            }, option=orjson.OPT_INDENT_2).decode()
        method = "Runtime.callFunction"
        params = {
            "executionContextId": ctx_id,
            "functionDeclaration": "function(expr) { return eval(expr); }",
            "args": [{"value": expression}],
            "returnByValue": True,
        }
    try:
        return await engine.call(method, params, session_id=session_id)
    except RuntimeError as e:
        return orjson.dumps({"error": f"Juggler evaluate failed: {e}"}, option=orjson.OPT_INDENT_2).decode()
    except Exception as e:
        return orjson.dumps({"error": f"Connection lost: {e}"}).decode()


async def _mirage_evaluate(
    expression: str,
    frame_id: str | None = None,
    session_id: str | None = None,
) -> str:
    """Evaluate an expression (optionally in a specific frame's main world);
    returns result.value as pretty JSON."""
    result = await _mirage_eval_result(expression, frame_id, session_id=session_id)
    if isinstance(result, str):
        return result
    if "error" in result:
        return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()
    if result.get("exceptionDetails"):
        return orjson.dumps({
            "error": "evaluate threw",
            "exception": result["exceptionDetails"],
        }, option=orjson.OPT_INDENT_2).decode()
    return orjson.dumps((result.get("result") or {}).get("value"), option=orjson.OPT_INDENT_2).decode()
