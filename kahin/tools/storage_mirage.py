"""storage_mirage.py — Mirage (Camoufox/Juggler) storage tools.

Faz 9 Task 3: cookies via the real Juggler Browser domain
(Browser.getCookies / Browser.setCookies / Browser.clearCookies — the
protocol has NO dedicated Storage domain) and localStorage/sessionStorage
through Runtime.evaluate (web platform APIs, not fakes).
"""

from __future__ import annotations

import math
from numbers import Integral, Real
from typing import Any

import orjson

from kahin._mcp import mcp
from kahin.tools._common import (
    _DW,
    _RO,
    _RW,
    _healer_ref,
    _mirage_call,
    _mirage_engine,
    _mirage_evaluate,
)

_MAX_STORAGE_ENTRIES = 2000
_MAX_STORAGE_VALUE = 4096
_MAX_STORAGE_KEY = 1024
_MAX_STORAGE_WRITE_VALUE = 1024 * 1024
_MAX_COOKIES = 500
_MAX_COOKIE_TEXT = 8192
_MAX_COOKIE_PAYLOAD = 1024 * 1024
_COOKIE_FIELDS = frozenset({
    "name", "value", "url", "domain", "path", "secure", "httpOnly", "sameSite", "expires",
})
_COOKIE_SAME_SITE = frozenset({"Strict", "Lax", "None"})


def _storage_limit(value: Any, tool: str) -> tuple[int, str | None]:
    if isinstance(value, bool) or not isinstance(value, Integral):
        return 0, orjson.dumps({
            "error": "limit must be a non-negative integer",
            "code": "invalid_argument",
            "tool": tool,
            "field": "limit",
        }).decode()
    requested = int(value)
    if requested < 0:
        return 0, orjson.dumps({
            "error": "limit must be a non-negative integer",
            "code": "invalid_argument",
            "tool": tool,
            "field": "limit",
        }).decode()
    return min(_MAX_STORAGE_ENTRIES, requested), None


def _context_params(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Attach the selected tab's context without routing the page call."""
    result = dict(params or {})
    try:
        context_id = _mirage_engine().current_browser_context_id()
    except Exception:  # engine liveness is reported by _mirage_call itself
        context_id = None
    if context_id:
        result["browserContextId"] = context_id
    return result


def _validate_cookies(tool: str, cookies: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    if not isinstance(cookies, list):
        return None, orjson.dumps({
            "error": "cookies must be a list of objects",
            "code": "invalid_argument",
            "tool": tool,
            "field": "cookies",
        }).decode()
    if not cookies:
        return None, orjson.dumps({
            "error": "cookies must not be empty",
            "code": "invalid_argument",
            "tool": tool,
            "field": "cookies",
        }).decode()
    if len(cookies) > _MAX_COOKIES:
        return None, orjson.dumps({
            "error": f"cookies cannot contain more than {_MAX_COOKIES} entries",
            "code": "invalid_argument",
            "tool": tool,
            "field": "cookies",
        }).decode()
    checked: list[dict[str, Any]] = []
    for index, cookie in enumerate(cookies):
        field = f"cookies[{index}]"
        if not isinstance(cookie, dict):
            return None, orjson.dumps({
                "error": f"{field} must be an object",
                "code": "invalid_argument",
                "tool": tool,
                "field": field,
            }).decode()
        unknown = sorted(set(cookie) - _COOKIE_FIELDS)
        if unknown:
            return None, orjson.dumps({
                "error": f"{field} contains unsupported fields",
                "code": "invalid_argument",
                "tool": tool,
                "field": field,
                "unsupported": unknown[:20],
            }).decode()
        name = cookie.get("name")
        value = cookie.get("value")
        if not isinstance(name, str) or not name or len(name) > _MAX_COOKIE_TEXT:
            return None, orjson.dumps({
                "error": f"{field}.name must be a non-empty string of at most {_MAX_COOKIE_TEXT} characters",
                "code": "invalid_argument", "tool": tool, "field": f"{field}.name",
            }).decode()
        if not isinstance(value, str) or len(value) > _MAX_COOKIE_TEXT:
            return None, orjson.dumps({
                "error": f"{field}.value must be a string of at most {_MAX_COOKIE_TEXT} characters",
                "code": "invalid_argument", "tool": tool, "field": f"{field}.value",
            }).decode()
        normalized = {"name": name, "value": value}
        for optional in ("url", "domain", "path"):
            if optional in cookie:
                raw = cookie[optional]
                if not isinstance(raw, str) or len(raw) > _MAX_COOKIE_TEXT:
                    return None, orjson.dumps({
                        "error": f"{field}.{optional} must be a string of at most {_MAX_COOKIE_TEXT} characters",
                        "code": "invalid_argument", "tool": tool, "field": f"{field}.{optional}",
                    }).decode()
                normalized[optional] = raw
        for boolean in ("secure", "httpOnly"):
            if boolean in cookie:
                raw = cookie[boolean]
                if not isinstance(raw, bool):
                    return None, orjson.dumps({
                        "error": f"{field}.{boolean} must be a boolean",
                        "code": "invalid_argument", "tool": tool, "field": f"{field}.{boolean}",
                    }).decode()
                normalized[boolean] = raw
        if "sameSite" in cookie:
            same_site = cookie["sameSite"]
            if same_site not in _COOKIE_SAME_SITE:
                return None, orjson.dumps({
                    "error": f"{field}.sameSite must be Strict, Lax, or None",
                    "code": "invalid_argument", "tool": tool, "field": f"{field}.sameSite",
                }).decode()
            normalized["sameSite"] = same_site
        if "expires" in cookie:
            expires = cookie["expires"]
            try:
                valid_expires = (
                    not isinstance(expires, bool)
                    and isinstance(expires, Real)
                    and math.isfinite(float(expires))
                )
            except (OverflowError, ValueError, TypeError):
                valid_expires = False
            if not valid_expires:
                return None, orjson.dumps({
                    "error": f"{field}.expires must be a finite number",
                    "code": "invalid_argument", "tool": tool, "field": f"{field}.expires",
                }).decode()
            normalized["expires"] = expires
        checked.append(normalized)
    if len(orjson.dumps(checked)) > _MAX_COOKIE_PAYLOAD:
        return None, orjson.dumps({
            "error": "cookie payload exceeds the 1 MiB limit",
            "code": "invalid_argument",
            "tool": tool,
            "field": "cookies",
        }).decode()
    return checked, None


def _storage_expression(name: str, limit: int) -> str:
    """Return a bounded, backwards-compatible list of storage entries."""
    return (
        "(() => Object.entries("
        + name
        + f").slice(0, {limit}).map(([key, value]) => ({{"
        + f"key, value: value.length > {_MAX_STORAGE_VALUE} "
        + f"? value.slice(0, {_MAX_STORAGE_VALUE}) + '…[truncated]' : value"
        + "})))()"
    )


def _bounded_cookie_result(raw: str) -> str:
    """Keep Browser.getCookies output bounded without hiding native errors."""
    try:
        payload = orjson.loads(raw)
    except (TypeError, orjson.JSONDecodeError):
        return raw
    if not isinstance(payload, dict) or not isinstance(payload.get("cookies"), list):
        return raw
    cookies = payload["cookies"]
    kept: list[dict[str, Any]] = []
    for cookie in cookies[:_MAX_COOKIES]:
        if not isinstance(cookie, dict):
            continue
        bounded: dict[str, Any] = {}
        for field in _COOKIE_FIELDS:
            value = cookie.get(field)
            if isinstance(value, str):
                bounded[field] = value[:_MAX_COOKIE_TEXT]
            elif isinstance(value, bool) or (isinstance(value, (int, float)) and not isinstance(value, bool)):
                bounded[field] = value
        kept.append(bounded)
    result = dict(payload)
    result["cookies"] = kept
    truncated = len(cookies) > len(kept)
    while len(orjson.dumps(result)) > _MAX_COOKIE_PAYLOAD and kept:
        kept.pop()
        truncated = True
    if truncated:
        result["truncated"] = True
        result["availableCookies"] = len(cookies)
        result["returnedCookies"] = len(kept)
    return orjson.dumps(result, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_cookie_get", annotations=_RO)
async def mirage_cookie_get() -> str:
    """Mirage: all cookies of the current context (Browser.getCookies)."""
    async with _healer_ref.safe("kahin_mirage_cookie_get"):
        return _bounded_cookie_result(await _mirage_call("Browser.getCookies", _context_params()))


@mcp.tool(name="kahin_mirage_cookie_set", annotations=_RW)
async def mirage_cookie_set(cookies: list[dict[str, Any]]) -> str:
    """Mirage: set cookies (Browser.setCookies). Each cookie: name, value,
    and optional url/domain/path/secure/httpOnly/sameSite/expires."""
    tool = "kahin_mirage_cookie_set"
    async with _healer_ref.safe(tool, cookie_count=len(cookies) if isinstance(cookies, list) else None):
        checked, error = _validate_cookies(tool, cookies)
        if error:
            return error
        assert checked is not None
        return await _mirage_call("Browser.setCookies", _context_params({"cookies": checked}))


@mcp.tool(name="kahin_mirage_cookie_clear", annotations=_DW)
async def mirage_cookie_clear() -> str:
    """Mirage: clear all cookies of the current context (Browser.clearCookies)."""
    async with _healer_ref.safe("kahin_mirage_cookie_clear"):
        return await _mirage_call("Browser.clearCookies", _context_params())


@mcp.tool(name="kahin_mirage_storage_local_get", annotations=_RO)
async def mirage_storage_local_get(limit: int = 200) -> str:
    """Mirage: up to ``limit`` localStorage entries of the current origin.
    Values longer than 4096 characters are explicitly truncated."""
    tool = "kahin_mirage_storage_local_get"
    async with _healer_ref.safe(tool, limit=limit):
        checked, error = _storage_limit(limit, tool)
        if error:
            return error
        return await _mirage_evaluate(_storage_expression("localStorage", checked))


@mcp.tool(name="kahin_mirage_storage_local_set", annotations=_RW)
async def mirage_storage_local_set(key: str, value: str) -> str:
    """Mirage: write one localStorage entry for the current origin."""
    tool = "kahin_mirage_storage_local_set"
    async with _healer_ref.safe(
        tool,
        key=key[:120] if isinstance(key, str) else None,
        value=value[:200] if isinstance(value, str) else None,
    ):
        if not isinstance(key, str) or not key or len(key) > _MAX_STORAGE_KEY:
            return orjson.dumps({
                "error": f"key must be a non-empty string of at most {_MAX_STORAGE_KEY} characters",
                "code": "invalid_argument", "tool": tool, "field": "key",
            }).decode()
        if not isinstance(value, str) or len(value) > _MAX_STORAGE_WRITE_VALUE:
            return orjson.dumps({
                "error": f"value must be a string of at most {_MAX_STORAGE_WRITE_VALUE} characters",
                "code": "invalid_argument", "tool": tool, "field": "value",
            }).decode()
        k = orjson.dumps(key).decode()
        v = orjson.dumps(value).decode()
        return await _mirage_evaluate(f"(() => {{ localStorage.setItem({k}, {v}); return 'set'; }})()")


@mcp.tool(name="kahin_mirage_storage_session_get", annotations=_RO)
async def mirage_storage_session_get(limit: int = 200) -> str:
    """Mirage: up to ``limit`` sessionStorage entries of the current origin.
    Values longer than 4096 characters are explicitly truncated."""
    tool = "kahin_mirage_storage_session_get"
    async with _healer_ref.safe(tool, limit=limit):
        checked, error = _storage_limit(limit, tool)
        if error:
            return error
        return await _mirage_evaluate(_storage_expression("sessionStorage", checked))
