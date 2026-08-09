"""dejavu_mirage.py — Mirage (Camoufox/Juggler) DEJA_VU tools.

Faz 9 Task 3: network inspection + console/error buffers over the real
Juggler surface:

- NetworkEx (8): request list + response bodies + interception
  (enable/disable/continue/abort) + cache controls; each maps to a real
  Network.* / Page.* / Browser.* Juggler method or to the forwarded event
  buffer (requestWillBeSent/responseReceived/requestFinished/requestFailed).
- console_log / errors_list: Runtime.console / Page.uncaughtError events the
  sidecar forwards verbatim (Faz 9 Task 2 console buffering).
"""

from __future__ import annotations

import base64
import math
from numbers import Integral
from typing import Any

import orjson

from kahin import _state as state
from kahin._mcp import mcp
from kahin.tools._common import (
    _DW,
    _RO,
    _RW,
    _healer_ref,
    _mirage_call,
    _mirage_engine,
    _network_response_payload,
    _require_mirage,
)

_REQUEST_EVENTS = ("requestWillBeSent", "responseReceived", "requestFinished", "requestFailed")

# Public inspection tools must remain bounded even when a page produces a
# pathological amount of traffic, console output, or a very large response.
_MAX_LIST_LIMIT = 500
_MAX_LIST_BYTES = 2 * 1024 * 1024
_MAX_EVENT_BYTES = 64 * 1024
_MAX_EVENT_STRING = 8 * 1024
_MAX_EVENT_ITEMS = 128
_MAX_EVENT_DEPTH = 8
_MAX_RESPONSE_BODY_BYTES = 1024 * 1024
_MAX_RESPONSE_BASE64_CHARS = ((_MAX_RESPONSE_BODY_BYTES + 2) // 3) * 4
_MAX_REQUEST_ID = 512
_MAX_URL = 8192
_MAX_METHOD = 32
_MAX_HEADER_COUNT = 100
_MAX_HEADER_TEXT = 16 * 1024
_MAX_POST_DATA = 1024 * 1024


def _ctx(value: Any, limit: int = 160) -> str:
    """Make healer context safe for malformed or adversarial input."""
    try:
        text = value if isinstance(value, str) else repr(value)
    except Exception:  # pragma: no cover - defensive logging path
        text = f"<{type(value).__name__}>"
    return text[:limit]


def _safe_len(value: Any, maximum: int = 501) -> int | None:
    try:
        if isinstance(value, (list, tuple, dict, str)):
            return min(len(value), maximum)
    except Exception:  # pragma: no cover - defensive logging path
        return None
    return None


def _dump(value: Any) -> str:
    return orjson.dumps(value, option=orjson.OPT_INDENT_2).decode()


def _error(
    tool: str,
    code: str,
    message: str,
    *,
    method: str | None = None,
    **details: Any,
) -> str:
    payload: dict[str, Any] = {
        "error": message,
        "code": code,
        "tool": tool,
        "engine": "mirage",
    }
    if method is not None:
        payload["method"] = method
    payload.update(details)
    return _dump(payload)


def _normalise_error(raw: Any, tool: str, method: str | None = None) -> str:
    """Turn legacy/plain liveness failures into the public error contract."""
    if isinstance(raw, str):
        try:
            payload = orjson.loads(raw)
        except orjson.JSONDecodeError:
            payload = None
    else:
        payload = raw
    if not isinstance(payload, dict) or "error" not in payload:
        return _error(
            tool,
            "engine_unavailable",
            str(raw)[:500] or "Mirage engine is unavailable",
            method=method,
            hint="Check kahin_engine_health, then restart the browser if needed.",
        )
    payload = dict(payload)
    payload.setdefault("tool", tool)
    payload.setdefault("engine", "mirage")
    if method is not None:
        payload.setdefault("method", method)
    if "code" not in payload:
        message = str(payload.get("error", ""))
        payload["code"] = (
            "connection_lost"
            if "connection" in message.lower() or "closed" in message.lower()
            else "juggler_call_failed"
        )
    return _dump(payload)


async def _require_mirage_structured(tool: str) -> str | None:
    try:
        err = await _require_mirage()
    except RuntimeError as exc:
        return _error(
            tool,
            "juggler_health_failed",
            f"Mirage health check failed: {exc}",
            hint="Check kahin_engine_health, then restart the browser if needed.",
        )
    except Exception as exc:  # noqa: BLE001
        return _error(
            tool,
            "connection_lost",
            f"Mirage health check lost the browser connection: {exc}",
            hint="Use kahin_browser_stop, then kahin_browser_start.",
        )
    return _normalise_error(err, tool) if err else None


async def _mirage_call_checked(
    tool: str,
    method: str,
    params: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> str:
    err = await _require_mirage_structured(tool)
    if err:
        return err
    try:
        result = await _mirage_call(method, params, session_id=session_id)
    except RuntimeError as exc:
        return _error(tool, "juggler_call_failed", f"Juggler call failed: {exc}", method=method)
    except Exception as exc:  # noqa: BLE001
        return _error(
            tool,
            "connection_lost",
            f"Connection lost while calling {method}: {exc}",
            method=method,
            hint="Use kahin_browser_stop, then kahin_browser_start.",
        )
    try:
        payload = orjson.loads(result)
    except (TypeError, orjson.JSONDecodeError):
        return _normalise_error(result, tool, method)
    if isinstance(payload, dict) and "error" in payload:
        return _normalise_error(payload, tool, method)
    return result


def _validate_limit(tool: str, value: Any) -> tuple[int, bool, str | None]:
    if isinstance(value, bool) or not isinstance(value, Integral):
        return 0, False, _error(tool, "invalid_parameter", "limit must be a non-negative integer", field="limit")
    requested = int(value)
    if requested < 0:
        return 0, False, _error(tool, "invalid_parameter", "limit must be a non-negative integer", field="limit")
    effective = min(requested, _MAX_LIST_LIMIT)
    return effective, requested != effective, None


def _validate_text(
    tool: str,
    field: str,
    value: Any,
    *,
    max_length: int,
    allow_empty: bool = False,
    allow_controls: bool = False,
) -> tuple[str | None, str | None]:
    if not isinstance(value, str):
        return None, _error(tool, "invalid_parameter", f"{field} must be a string", field=field)
    if not allow_empty and not value.strip():
        return None, _error(tool, "invalid_parameter", f"{field} must not be empty", field=field)
    if len(value) > max_length:
        return None, _error(
            tool, "invalid_parameter", f"{field} exceeds the {max_length}-character limit", field=field,
        )
    if not allow_controls and any(ord(char) < 32 and char not in "\t" for char in value):
        return None, _error(tool, "invalid_parameter", f"{field} contains control characters", field=field)
    return value, None


def _validate_headers(tool: str, headers: Any) -> tuple[list[dict[str, str]] | None, str | None]:
    if headers is None:
        return None, None
    if not isinstance(headers, list):
        return None, _error(tool, "invalid_parameter", "headers must be a list", field="headers")
    if len(headers) > _MAX_HEADER_COUNT:
        return None, _error(
            tool, "invalid_parameter", f"headers cannot contain more than {_MAX_HEADER_COUNT} entries", field="headers",
        )
    checked: list[dict[str, str]] = []
    total = 0
    for index, header in enumerate(headers):
        if not isinstance(header, dict):
            return None, _error(tool, "invalid_parameter", f"headers[{index}] must be an object", field="headers")
        name, name_err = _validate_text(tool, f"headers[{index}].name", header.get("name"), max_length=_MAX_HEADER_TEXT)
        if name_err:
            return None, name_err
        value, value_err = _validate_text(
            tool, f"headers[{index}].value", header.get("value"), max_length=_MAX_HEADER_TEXT, allow_empty=True,
        )
        if value_err:
            return None, value_err
        assert name is not None and value is not None
        total += len(name) + len(value)
        if total > _MAX_POST_DATA:
            return None, _error(tool, "invalid_parameter", "headers payload is too large", field="headers")
        checked.append({"name": name, "value": value})
    return checked, None


def _bounded_value(value: Any, depth: int = 0) -> tuple[Any, bool]:
    if depth >= _MAX_EVENT_DEPTH:
        return "<truncated>", True
    if isinstance(value, str):
        if len(value) > _MAX_EVENT_STRING:
            return value[:_MAX_EVENT_STRING] + "...<truncated>", True
        return value, False
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        truncated = False
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_EVENT_ITEMS:
                truncated = True
                break
            bounded, item_truncated = _bounded_value(item, depth + 1)
            out[str(key)] = bounded
            truncated = truncated or item_truncated
        return out, truncated
    if isinstance(value, (list, tuple)):
        out = []
        truncated = len(value) > _MAX_EVENT_ITEMS
        for item in value[:_MAX_EVENT_ITEMS]:
            bounded, item_truncated = _bounded_value(item, depth + 1)
            out.append(bounded)
            truncated = truncated or item_truncated
        return out, truncated
    if isinstance(value, (bytes, bytearray)):
        return f"<{type(value).__name__} {len(value)} bytes>", True
    if isinstance(value, int) and value.bit_length() > 256:
        return "<integer-truncated>", True
    if isinstance(value, float) and not math.isfinite(value):
        return "<non-finite>", True
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    return _ctx(value, _MAX_EVENT_STRING), True


def _bounded_event(event: Any) -> tuple[dict[str, Any], bool]:
    bounded, truncated = _bounded_value(event)
    if not isinstance(bounded, dict):
        bounded = {"value": bounded}
        truncated = True
    if truncated:
        bounded["truncated"] = True
        bounded["truncation"] = {
            "maxEventBytes": _MAX_EVENT_BYTES,
            "maxStringChars": _MAX_EVENT_STRING,
        }
    try:
        encoded = orjson.dumps(bounded)
    except Exception:  # pragma: no cover - defensive state corruption path
        encoded = b""
    if len(encoded) > _MAX_EVENT_BYTES:
        return {
            "event": bounded.get("event"),
            "timestamp": bounded.get("timestamp"),
            "truncated": True,
            "truncation": {"maxEventBytes": _MAX_EVENT_BYTES, "reason": "event_payload_limit"},
        }, True
    return bounded, truncated


def _bounded_list(items: list[Any], requested: int, effective: int, limit_clamped: bool) -> str:
    selected = items[-effective:] if effective else []
    kept = [_bounded_event(item)[0] for item in selected]
    payload_truncated = any(item.get("truncated") is True for item in kept)
    dropped = 0
    rendered = _dump(kept)
    while len(rendered.encode()) > _MAX_LIST_BYTES and kept:
        kept.pop(0)
        dropped += 1
        rendered = _dump(kept)
    truncated = bool(payload_truncated or dropped or len(items) > effective)
    if not (truncated or limit_clamped):
        return rendered
    result: dict[str, Any] = {
        "items": kept,
        "count": len(kept),
        "available": len(items),
        "requestedLimit": (
            requested if requested.bit_length() <= 256 else "<integer-too-large>"
        ),
        "appliedLimit": effective,
        "truncated": truncated,
        "limitClamped": limit_clamped,
    }
    if payload_truncated:
        result["payloadTruncated"] = True
    if dropped:
        result["droppedItems"] = dropped
    while len(_dump(result).encode()) > _MAX_LIST_BYTES and result["items"]:
        result["items"].pop(0)
        result["count"] -= 1
        result["droppedItems"] = int(result.get("droppedItems", 0)) + 1
        result["truncated"] = True
    return _dump(result)


def _current_session_id() -> str | None:
    """Return the session owning the selected tab for buffer filtering."""
    try:
        engine = _mirage_engine()
        return engine._sessions.get(engine._current_target or "")
    except Exception:  # pragma: no cover - only reached during engine teardown
        return None


def _network_summary(event: dict[str, Any]) -> dict[str, Any]:
    """Return the crawl-friendly request identity without raw headers."""
    params = event.get("params") or {}
    request = params.get("request") or {}
    response = _network_response_payload(event)
    summary: dict[str, Any] = {
        "event": event.get("event"),
        "timestamp": event.get("timestamp"),
        "session_id": event.get("session_id"),
            "requestId": params.get("requestId"),
        "url": params.get("url") or request.get("url") or response.get("url"),
        "method": params.get("method") or request.get("method"),
        "resourceType": params.get("type") or params.get("resourceType"),
    }
    if response:
        summary.update({
            "status": response.get("status"),
            "statusText": response.get("statusText"),
            "mimeType": response.get("mimeType"),
        })
    if params.get("errorText") is not None:
        summary["errorText"] = params.get("errorText")
    if params.get("isIntercepted") is True:
        summary["isIntercepted"] = True
    return {key: value for key, value in summary.items() if value is not None}


def _owner_session_id(request_id: str, preferred_session: str | None = None) -> str | None:
    """Find the live Juggler session that emitted a request event.

    CDP request ids are only unique within a page session. Prefer the
    currently selected tab and refuse an ambiguous cross-tab match instead
    of sending a decision to the wrong page.
    """
    owners: list[str] = []
    for event in reversed(state._network_requests):
        if not isinstance(event, dict) or event.get("session_id") is None:
            continue
        params = event.get("params") or {}
        if params.get("requestId") == request_id:
            owner = event.get("session_id")
            if isinstance(owner, str) and owner:
                if preferred_session == owner:
                    return owner
                if owner not in owners:
                    owners.append(owner)
    return owners[0] if len(owners) == 1 else None


@mcp.tool(name="kahin_mirage_network_requests", annotations=_RO)
async def mirage_network_requests(limit: int = 50, detail: bool = False) -> str:
    """Mirage: recent network requests from the buffered event stream
    (requestWillBeSent/responseReceived/requestFinished/requestFailed)."""
    async with _healer_ref.safe("kahin_mirage_network_requests", limit=_ctx(limit, 64), detail=detail):
        if not isinstance(detail, bool):
            return _error("kahin_mirage_network_requests", "invalid_parameter", "detail must be a boolean", field="detail")
        effective, clamped, validation_error = _validate_limit("kahin_mirage_network_requests", limit)
        if validation_error:
            return validation_error
        err = await _require_mirage_structured("kahin_mirage_network_requests")
        if err:
            return err
        current_session = _current_session_id()
        reqs = [
            e for e in state._network_requests
            if isinstance(e, dict)
            and e.get("event") in _REQUEST_EVENTS
            and e.get("session_id") == current_session
        ]
        items = reqs if detail else [_network_summary(item) for item in reqs]
        return _bounded_list(items, int(limit), effective, clamped)


@mcp.tool(name="kahin_mirage_get_response_body", annotations=_RO)
async def mirage_get_response_body(request_id: str) -> str:
    """Mirage: response body of a request (Network.getResponseBody ->
    {base64body, evicted?}), decoded to text when possible."""
    tool = "kahin_mirage_get_response_body"
    async with _healer_ref.safe(tool, request_id=_ctx(request_id, 80)):
        request_id, validation_error = _validate_text(
            tool, "request_id", request_id, max_length=_MAX_REQUEST_ID,
        )
        if validation_error:
            return validation_error
        assert request_id is not None
        err = await _require_mirage_structured(tool)
        if err:
            return err
        try:
            engine = _mirage_engine()
            await engine.ensure_page()
            owner_session = _owner_session_id(request_id, _current_session_id())
            if owner_session is None:
                return _error(
                    tool,
                    "request_owner_unknown",
                    "requestId is not present in the live network buffer; refusing to route it to the current tab",
                    method="Network.getResponseBody",
                    hint="Refresh kahin_mirage_network_requests and use a request from the active browser session.",
                )
            result = await engine.get_response_body(request_id, session_id=owner_session)
        except RuntimeError as exc:
            return _error(tool, "juggler_call_failed", f"Juggler call failed: {exc}", method="Network.getResponseBody")
        except Exception as exc:  # noqa: BLE001
            return _error(
                tool,
                "connection_lost",
                f"Connection lost while calling Network.getResponseBody: {exc}",
                method="Network.getResponseBody",
                hint="Use kahin_browser_stop, then kahin_browser_start.",
            )
        if not isinstance(result, dict):
            return _error(tool, "invalid_native_response", "Network.getResponseBody returned a non-object response")
        if "error" in result:
            return _normalise_error(result, tool, "Network.getResponseBody")
        raw = result.get("base64body", "") or ""
        if not isinstance(raw, str):
            return _error(
                tool, "invalid_native_response", "Network.getResponseBody.base64body must be a string",
                method="Network.getResponseBody",
            )
        raw_limit = min(len(raw), _MAX_RESPONSE_BASE64_CHARS)
        raw_prefix = raw[:raw_limit]
        if len(raw) > _MAX_RESPONSE_BASE64_CHARS:
            raw_prefix = raw_prefix[: len(raw_prefix) - (len(raw_prefix) % 4)]
        try:
            body_bytes = base64.b64decode(raw_prefix, validate=True)
        except Exception as exc:  # noqa: BLE001
            return _error(
                tool,
                "invalid_native_response",
                f"Network.getResponseBody returned invalid base64body: {exc}",
                method="Network.getResponseBody",
            )
        body_truncated = len(body_bytes) > _MAX_RESPONSE_BODY_BYTES
        body_bytes = body_bytes[:_MAX_RESPONSE_BODY_BYTES]
        truncated = len(raw) > len(raw_prefix) or body_truncated
        out: dict[str, Any] = {
            "body": body_bytes.decode("utf-8", errors="replace"),
            "base64body": raw_prefix,
        }
        if "evicted" in result:
            out["evicted"] = result["evicted"]
        if truncated:
            out["truncated"] = True
            out["truncation"] = {
                "maxBodyBytes": _MAX_RESPONSE_BODY_BYTES,
                "originalBase64Chars": len(raw),
                "returnedBase64Chars": len(raw_prefix),
            }
        return _dump(out)


@mcp.tool(name="kahin_mirage_intercept_requests", annotations=_RW)
async def mirage_intercept_requests() -> str:
    """Mirage: intercept page requests (Network.setRequestInterception
    enabled=true — Network domain, not the Browser context one). This is a
    persistent mode: continue/abort each intercepted request and call
    kahin_mirage_unintercept_requests when the interception session is over."""
    async with _healer_ref.safe("kahin_mirage_intercept_requests"):
        return await _mirage_call_checked(
            "kahin_mirage_intercept_requests", "Network.setRequestInterception", {"enabled": True},
        )


@mcp.tool(name="kahin_mirage_unintercept_requests", annotations=_RW)
async def mirage_unintercept_requests() -> str:
    """Mirage: stop intercepting page requests and release any future
    request pauses (Network.setRequestInterception enabled=false)."""
    async with _healer_ref.safe("kahin_mirage_unintercept_requests"):
        return await _mirage_call_checked(
            "kahin_mirage_unintercept_requests", "Network.setRequestInterception", {"enabled": False},
        )


@mcp.tool(name="kahin_mirage_network_continue", annotations=_RW)
async def mirage_network_continue(
    request_id: str,
    url: str | None = None,
    method: str | None = None,
    headers: list[dict[str, Any]] | None = None,
    post_data: str | None = None,
) -> str:
    """Mirage: resume an intercepted request, optionally overriding url/method/
    headers/postData (Network.resumeInterceptedRequest). Interception remains
    enabled for later requests; call kahin_mirage_unintercept_requests when
    finished."""
    tool = "kahin_mirage_network_continue"
    async with _healer_ref.safe(
        tool,
        request_id=_ctx(request_id, 80),
        url=_ctx(url, 120) if url is not None else None,
        method=_ctx(method, 40) if method is not None else None,
        header_count=_safe_len(headers),
    ):
        checked_request_id, validation_error = _validate_text(
            tool, "request_id", request_id, max_length=_MAX_REQUEST_ID,
        )
        if validation_error:
            return validation_error
        assert checked_request_id is not None
        params: dict[str, Any] = {"requestId": checked_request_id}
        if url is not None:
            checked_url, validation_error = _validate_text(tool, "url", url, max_length=_MAX_URL)
            if validation_error:
                return validation_error
            params["url"] = checked_url
        if method is not None:
            checked_method, validation_error = _validate_text(tool, "method", method, max_length=_MAX_METHOD)
            if validation_error:
                return validation_error
            assert checked_method is not None
            if any(char.isspace() for char in checked_method):
                return _error(tool, "invalid_parameter", "method must not contain whitespace", field="method")
            params["method"] = checked_method
        checked_headers, validation_error = _validate_headers(tool, headers)
        if validation_error:
            return validation_error
        if checked_headers is not None:
            params["headers"] = checked_headers
        if post_data is not None:
            checked_post_data, validation_error = _validate_text(
                tool,
                "post_data",
                post_data,
                max_length=_MAX_POST_DATA,
                allow_empty=True,
                allow_controls=True,
            )
            if validation_error:
                return validation_error
            params["postData"] = checked_post_data
        owner_session = _owner_session_id(checked_request_id, _current_session_id())
        if owner_session is None:
            return _error(
                tool,
                "request_owner_unknown",
                "requestId is not present in the live network buffer; refusing to resume a different tab's request",
                method="Network.resumeInterceptedRequest",
                hint="Use a requestId returned by kahin_mirage_network_requests.",
            )
        return await _mirage_call_checked(
            tool, "Network.resumeInterceptedRequest", params, session_id=owner_session,
        )


@mcp.tool(name="kahin_mirage_network_abort", annotations=_DW)
async def mirage_network_abort(request_id: str, error_code: str = "Aborted") -> str:
    """Mirage: abort an intercepted request (Network.abortInterceptedRequest).
    Interception remains enabled for later requests; call
    kahin_mirage_unintercept_requests when finished."""
    tool = "kahin_mirage_network_abort"
    async with _healer_ref.safe(tool, request_id=_ctx(request_id, 80), error_code=_ctx(error_code, 80)):
        checked_request_id, validation_error = _validate_text(
            tool, "request_id", request_id, max_length=_MAX_REQUEST_ID,
        )
        if validation_error:
            return validation_error
        checked_error_code, validation_error = _validate_text(
            tool, "error_code", error_code, max_length=128,
        )
        if validation_error:
            return validation_error
        owner_session = _owner_session_id(checked_request_id, _current_session_id())
        if owner_session is None:
            return _error(
                tool,
                "request_owner_unknown",
                "requestId is not present in the live network buffer; refusing to abort a different tab's request",
                method="Network.abortInterceptedRequest",
                hint="Use a requestId returned by kahin_mirage_network_requests.",
            )
        return await _mirage_call_checked(
            tool,
            "Network.abortInterceptedRequest",
            {"requestId": checked_request_id, "errorCode": checked_error_code},
            session_id=owner_session,
        )


@mcp.tool(name="kahin_mirage_cache_disable", annotations=_RW)
async def mirage_cache_disable(cache_disabled: bool = True) -> str:
    """Mirage: enable/disable the HTTP cache for the current page
    (Page.setCacheDisabled)."""
    tool = "kahin_mirage_cache_disable"
    async with _healer_ref.safe(tool, cache_disabled=_ctx(cache_disabled, 40)):
        if not isinstance(cache_disabled, bool):
            return _error(tool, "invalid_parameter", "cache_disabled must be a boolean", field="cache_disabled")
        return await _mirage_call_checked(tool, "Page.setCacheDisabled", {"cacheDisabled": cache_disabled})


@mcp.tool(name="kahin_mirage_clear_cache", annotations=_RW)
async def mirage_clear_cache() -> str:
    """Mirage: clear the browser cache (Browser.clearCache)."""
    tool = "kahin_mirage_clear_cache"
    async with _healer_ref.safe(tool):
        return await _mirage_call_checked(tool, "Browser.clearCache")


@mcp.tool(name="kahin_mirage_console_log", annotations=_RO)
async def mirage_console_log(limit: int = 100) -> str:
    """Mirage: buffered console messages (Runtime.console events forwarded
    by the sidecar)."""
    async with _healer_ref.safe("kahin_mirage_console_log", limit=_ctx(limit, 64)):
        effective, clamped, validation_error = _validate_limit("kahin_mirage_console_log", limit)
        if validation_error:
            return validation_error
        err = await _require_mirage_structured("kahin_mirage_console_log")
        if err:
            return err
        current_session = _current_session_id()
        return _bounded_list(
            [e for e in state._console_messages if isinstance(e, dict) and e.get("session_id") == current_session],
            int(limit), effective, clamped,
        )


@mcp.tool(name="kahin_mirage_errors_list", annotations=_RO)
async def mirage_errors_list(limit: int = 50) -> str:
    """Mirage: buffered uncaught page errors (Page.uncaughtError events)."""
    async with _healer_ref.safe("kahin_mirage_errors_list", limit=_ctx(limit, 64)):
        effective, clamped, validation_error = _validate_limit("kahin_mirage_errors_list", limit)
        if validation_error:
            return validation_error
        err = await _require_mirage_structured("kahin_mirage_errors_list")
        if err:
            return err
        current_session = _current_session_id()
        errors = [
            e for e in state._current_event_log
            if isinstance(e, dict)
            and e.get("event") == "Page.uncaughtError"
            and e.get("session_id") == current_session
        ]
        return _bounded_list(errors, int(limit), effective, clamped)
