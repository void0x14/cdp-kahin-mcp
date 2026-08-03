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
from typing import Any

import orjson

from kahin import _state as state
from kahin.oracle import mcp
from kahin.tools._common import (
    _DW,
    _RO,
    _RW,
    _healer_ref,
    _mirage_call,
    _mirage_engine,
    _require_engine,
)

_REQUEST_EVENTS = ("requestWillBeSent", "responseReceived", "requestFinished", "requestFailed")


@mcp.tool(name="kahin_mirage_network_requests", annotations=_RO)
async def mirage_network_requests(limit: int = 50) -> str:
    """Mirage: recent network requests from the buffered event stream
    (requestWillBeSent/responseReceived/requestFinished/requestFailed)."""
    async with _healer_ref.safe("kahin_mirage_network_requests", limit=limit):
        reqs = [e for e in state._network_requests if e["event"] in _REQUEST_EVENTS]
        return orjson.dumps(list(reqs)[-int(limit):], option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_get_response_body", annotations=_RO)
async def mirage_get_response_body(request_id: str) -> str:
    """Mirage: response body of a request (Network.getResponseBody ->
    {base64body, evicted?}), decoded to text when possible."""
    async with _healer_ref.safe("kahin_mirage_get_response_body", request_id=request_id[:80]):
        # Intentional: _require_engine (not _require_mirage) — Network.
        # getResponseBody exists in BOTH protocols (Juggler NetworkEx and CDP),
        # and Obscura.call folds the Juggler token back into a CDP command.
        err = await _require_engine()
        if err:
            return err
        try:
            result = await _mirage_engine().call("Network.getResponseBody", {"requestId": request_id})
        except Exception as e:  # noqa: BLE001
            return orjson.dumps({"error": f"getResponseBody failed: {e}"}).decode()
        raw = result.get("base64body", "") or ""
        try:
            body = base64.b64decode(raw).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body = raw
        out: dict[str, Any] = {"body": body, "base64body": raw}
        if "evicted" in result:
            out["evicted"] = result["evicted"]
        return orjson.dumps(out, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_intercept_requests", annotations=_RW)
async def mirage_intercept_requests() -> str:
    """Mirage: intercept page requests (Network.setRequestInterception
    enabled=true — Network domain, not the Browser context one)."""
    async with _healer_ref.safe("kahin_mirage_intercept_requests"):
        return await _mirage_call("Network.setRequestInterception", {"enabled": True})


@mcp.tool(name="kahin_mirage_unintercept_requests", annotations=_RW)
async def mirage_unintercept_requests() -> str:
    """Mirage: stop intercepting page requests (Network.setRequestInterception
    enabled=false)."""
    async with _healer_ref.safe("kahin_mirage_unintercept_requests"):
        return await _mirage_call("Network.setRequestInterception", {"enabled": False})


@mcp.tool(name="kahin_mirage_network_continue", annotations=_RW)
async def mirage_network_continue(
    request_id: str,
    url: str | None = None,
    method: str | None = None,
    headers: list[dict[str, Any]] | None = None,
    post_data: str | None = None,
) -> str:
    """Mirage: resume an intercepted request, optionally overriding url/method/
    headers/postData (Network.resumeInterceptedRequest)."""
    params: dict[str, Any] = {"requestId": request_id}
    if url is not None:
        params["url"] = url
    if method is not None:
        params["method"] = method
    if headers is not None:
        params["headers"] = headers
    if post_data is not None:
        params["postData"] = post_data
    return await _mirage_call("Network.resumeInterceptedRequest", params)


@mcp.tool(name="kahin_mirage_network_abort", annotations=_DW)
async def mirage_network_abort(request_id: str, error_code: str = "Aborted") -> str:
    """Mirage: abort an intercepted request (Network.abortInterceptedRequest)."""
    return await _mirage_call("Network.abortInterceptedRequest", {
        "requestId": request_id, "errorCode": error_code,
    })


@mcp.tool(name="kahin_mirage_cache_disable", annotations=_RW)
async def mirage_cache_disable(cache_disabled: bool = True) -> str:
    """Mirage: enable/disable the HTTP cache for the current page
    (Page.setCacheDisabled)."""
    return await _mirage_call("Page.setCacheDisabled", {"cacheDisabled": cache_disabled})


@mcp.tool(name="kahin_mirage_clear_cache", annotations=_RW)
async def mirage_clear_cache() -> str:
    """Mirage: clear the browser cache (Browser.clearCache)."""
    return await _mirage_call("Browser.clearCache")


@mcp.tool(name="kahin_mirage_console_log", annotations=_RO)
async def mirage_console_log(limit: int = 100) -> str:
    """Mirage: buffered console messages (Runtime.console events forwarded
    by the sidecar)."""
    async with _healer_ref.safe("kahin_mirage_console_log", limit=limit):
        return orjson.dumps(list(state._console_messages)[-int(limit):], option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_errors_list", annotations=_RO)
async def mirage_errors_list(limit: int = 50) -> str:
    """Mirage: buffered uncaught page errors (Page.uncaughtError events)."""
    async with _healer_ref.safe("kahin_mirage_errors_list", limit=limit):
        errors = [e for e in state._current_event_log if e["event"] == "Page.uncaughtError"]
        return orjson.dumps(list(errors)[-int(limit):], option=orjson.OPT_INDENT_2).decode()