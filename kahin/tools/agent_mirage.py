"""agent_mirage.py — Agent-native observation tools (Faz 2, Tasks 2-4).

``kahin_mirage_snapshot`` wraps the live DOM snapshot (nodeId-carrying tree)
into the compact ref-carrying line format agents act on, with explicit token
accounting. Every emitted ref is a live DOM nodeId valid for
``kahin_mirage_dom_action`` until the document/frame lifecycle ends; after a
navigation or reset the agent must request a fresh snapshot before acting.
``kahin_mirage_fill_form`` types several live fields by ref in one call.
``kahin_mirage_state_save``/``kahin_mirage_state_load`` persist a session
(cookies, localStorage, sessionStorage, current url) to an absolute-path
JSON file and restore it — storage is origin-bound, so load navigates back
to the saved url before restoring anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson

from kahin._mcp import mcp
from kahin.agent_snapshot import format_snapshot
from kahin.tools._common import _RO, _RW, _healer_ref, _mirage_evaluate
from kahin.tools.dom_stream_mirage import mirage_dom_action, mirage_dom_snapshot
from kahin.tools.pilot_mirage import (
    _MAX_SELECTOR_LENGTH,
    _MAX_WAIT_TIMEOUT,
    _bounded_float,
    _bounded_int,
    _capture_page_session,
    _json_error,
    _text_arg,
)

_MAX_TOKEN_BUDGET = 100_000
_FILL_FIELDS_MAX = 100
_MAX_STATE_PATH_LENGTH = 4_096
_MAX_STATE_PAYLOAD = 16 * 1024 * 1024
_STATE_STORAGE_KEY_MAX = 1_024
_STATE_STORAGE_VALUE_MAX = 1024 * 1024
_STATE_URL_MAX = 4_096


def _loads(text: str) -> Any:
    return orjson.loads(text)


def _state_absolute_path(value: str, tool: str, field: str) -> tuple[Path | None, str | None]:
    """Expand ``~`` and require an absolute path (upload_files security rule)."""
    expanded = Path(value).expanduser()
    if not expanded.is_absolute():
        return None, _json_error(
            tool,
            f"{field} must be an absolute path (or ~-prefixed)",
            "invalid_argument",
            field=field,
        )
    return expanded, None


async def _storage_snapshot(
    tool: str,
    storage_name: str,
    session_id: str,
) -> tuple[dict[str, str] | None, str | None]:
    """Read one web-storage object pinned to the captured page session.

    ``kahin_mirage_storage_*_get`` returns a list of ``{key, value}``
    entries; here that is normalized into the ``{key: value}`` object the
    state file stores, so restore can write every key back.
    """
    from kahin.tools.storage_mirage import (  # noqa: PLC0415
        _MAX_STORAGE_ENTRIES,
        _storage_expression,
    )

    try:
        raw = _loads(await _mirage_evaluate(
            _storage_expression(storage_name, _MAX_STORAGE_ENTRIES),
            session_id=session_id,
        ))
    except orjson.JSONDecodeError:
        return None, _json_error(tool, "storage read returned invalid JSON", "invalid_engine_response")
    if isinstance(raw, dict) and raw.get("error"):
        return None, orjson.dumps(raw, option=orjson.OPT_INDENT_2).decode()
    if not isinstance(raw, list):
        return None, _json_error(tool, "storage read returned an invalid payload", "invalid_engine_response")
    normalized: dict[str, str] = {}
    for entry in raw:
        if not (
            isinstance(entry, dict)
            and isinstance(entry.get("key"), str)
            and isinstance(entry.get("value"), str)
        ):
            return None, _json_error(tool, "storage read returned a malformed entry", "invalid_engine_response")
        normalized[entry["key"]] = entry["value"]
    return normalized, None


def _validate_loaded_storage(
    value: Any,
    tool: str,
    field: str,
    path: str,
) -> tuple[dict[str, str] | None, str | None]:
    """Validate one restored web-storage object from a state file."""
    if not isinstance(value, dict):
        return None, _json_error(
            tool,
            f"state file {field} must be an object",
            "invalid_argument",
            path=path,
            field=field,
        )
    checked: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > _STATE_STORAGE_KEY_MAX:
            return None, _json_error(
                tool,
                f"state file {field} has an out-of-bounds key",
                "invalid_argument",
                path=path,
                field=field,
            )
        if not isinstance(item, str) or len(item) > _STATE_STORAGE_VALUE_MAX:
            return None, _json_error(
                tool,
                f"state file {field} has an out-of-bounds value",
                "invalid_argument",
                path=path,
                field=field,
            )
        checked[key] = item
    return checked, None


def _normalize_loaded_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adapt restored cookies to Browser.setCookies' real behavior.

    The Juggler harness silently drops cookies whose ``expires`` is a
    non-positive number (the representation ``Browser.getCookies`` uses for
    session cookies). Dropping that field restores them as session cookies;
    genuine future expirations are kept intact.
    """
    normalized: list[dict[str, Any]] = []
    for cookie in cookies:
        entry = dict(cookie)
        expires = entry.get("expires")
        if (
            isinstance(expires, (int, float))
            and not isinstance(expires, bool)
            and expires <= 0
        ):
            entry.pop("expires", None)
        normalized.append(entry)
    return normalized


async def _restore_storage(
    tool: str,
    storage_name: str,
    entries: dict[str, str],
    session_id: str,
) -> str | None:
    """Write every storage entry back, pinned to the loaded page session."""
    for key, value in entries.items():
        k = orjson.dumps(key).decode()
        v = orjson.dumps(value).decode()
        expression = f"(() => {{ {storage_name}.setItem({k}, {v}); return 'set'; }})()"
        outcome = _loads(await _mirage_evaluate(expression, session_id=session_id))
        if isinstance(outcome, dict) and outcome.get("error"):
            return orjson.dumps(outcome, option=orjson.OPT_INDENT_2).decode()
        if outcome != "set":
            return _json_error(tool, f"{storage_name} write returned an invalid payload", "invalid_engine_response")
    return None

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


@mcp.tool(name="kahin_mirage_state_save", annotations=_RO)
async def mirage_state_save(path: str) -> str:
    """Mirage: persist the current session to an absolute-path JSON file.

    Captures the current url plus every cookie and every local/session
    storage entry of the active page (values are bounded by the storage
    layer), writes ``{version, url, cookies, localStorage, sessionStorage}``
    and reports per-section counts. Restore with kahin_mirage_state_load.
    """
    tool = "kahin_mirage_state_save"
    path_value, error = _text_arg(
        path, tool=tool, field="path", maximum=_MAX_STATE_PATH_LENGTH,
    )
    if error:
        return error
    assert path_value is not None
    target, error = _state_absolute_path(path_value, tool, "path")
    if error:
        return error
    assert target is not None
    async with _healer_ref.safe(tool, path=str(target)):
        from kahin.tools import storage_mirage  # noqa: PLC0415

        session_id, capture_error = await _capture_page_session(tool)
        if capture_error:
            return capture_error

        cookies_raw = _loads(await storage_mirage.mirage_cookie_get())
        if isinstance(cookies_raw, dict) and cookies_raw.get("error"):
            return orjson.dumps(cookies_raw, option=orjson.OPT_INDENT_2).decode()
        cookies = (
            cookies_raw.get("cookies")
            if isinstance(cookies_raw, dict) and isinstance(cookies_raw.get("cookies"), list)
            else []
        )

        local, storage_error = await _storage_snapshot(tool, "localStorage", session_id)
        if storage_error:
            return storage_error
        session, storage_error = await _storage_snapshot(tool, "sessionStorage", session_id)
        if storage_error:
            return storage_error
        assert local is not None and session is not None
        for storage_name, entries in (("localStorage", local), ("sessionStorage", session)):
            for key in entries:
                if len(key) > _STATE_STORAGE_KEY_MAX:
                    return _json_error(
                        tool,
                        f"{storage_name} key exceeds the persistence bound",
                        "invalid_argument",
                        field=storage_name,
                        maximum=_STATE_STORAGE_KEY_MAX,
                    )

        url = ""
        try:
            url_raw = _loads(await _mirage_evaluate("location.href", session_id=session_id))
        except orjson.JSONDecodeError:
            url_raw = None
        if isinstance(url_raw, str):
            url = url_raw[:_STATE_URL_MAX]

        payload = {
            "version": 1,
            "url": url,
            "cookies": cookies,
            "localStorage": local,
            "sessionStorage": session,
        }
        try:
            serialized = orjson.dumps(payload, option=orjson.OPT_INDENT_2)
        except (TypeError, ValueError) as exc:
            return _json_error(tool, f"cannot serialize session state: {exc}", "tool_failed")
        if len(serialized) > _MAX_STATE_PAYLOAD:
            return _json_error(
                tool,
                "session state exceeds the persistence payload bound",
                "invalid_argument",
                maximum=_MAX_STATE_PAYLOAD,
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(serialized)
        except OSError as exc:
            return _json_error(tool, f"cannot write state file: {exc}", "tool_failed", path=str(target))
        return orjson.dumps({
            "saved": True,
            "path": str(target),
            "cookies": len(cookies),
            "localStorage": len(local),
            "sessionStorage": len(session),
            "url": url,
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_state_load", annotations=_RW)
async def mirage_state_load(path: str) -> str:
    """Mirage: restore a session saved by kahin_mirage_state_save.

    Navigates to the saved url first (storage is origin-bound), then
    restores cookies, localStorage and sessionStorage against that origin.
    """
    tool = "kahin_mirage_state_load"
    path_value, error = _text_arg(
        path, tool=tool, field="path", maximum=_MAX_STATE_PATH_LENGTH,
    )
    if error:
        return error
    assert path_value is not None
    target, error = _state_absolute_path(path_value, tool, "path")
    if error:
        return error
    assert target is not None
    async with _healer_ref.safe(tool, path=str(target)):
        try:
            data = target.read_bytes()
        except OSError as exc:
            return _json_error(tool, f"cannot read state file: {exc}", "tool_failed", path=str(target))
        if len(data) > _MAX_STATE_PAYLOAD:
            return _json_error(
                tool,
                "state file exceeds the read payload bound",
                "invalid_argument",
                path=str(target),
                maximum=_MAX_STATE_PAYLOAD,
            )
        try:
            payload = orjson.loads(data)
        except orjson.JSONDecodeError as exc:
            return _json_error(
                tool,
                f"state file is not valid JSON: {exc}",
                "invalid_argument",
                path=str(target),
            )
        if not isinstance(payload, dict):
            return _json_error(
                tool,
                "state file must contain a JSON object",
                "invalid_argument",
                path=str(target),
            )
        version = payload.get("version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            return _json_error(
                tool,
                "unsupported state file version",
                "invalid_argument",
                path=str(target),
                version=version,
            )
        url = payload.get("url", "")
        if not isinstance(url, str) or len(url) > _STATE_URL_MAX:
            return _json_error(
                tool,
                "state file url must be a bounded string",
                "invalid_argument",
                path=str(target),
                field="url",
            )
        cookies = payload.get("cookies", [])
        if not isinstance(cookies, list):
            return _json_error(
                tool,
                "state file cookies must be a list",
                "invalid_argument",
                path=str(target),
                field="cookies",
            )
        local, storage_error = _validate_loaded_storage(
            payload.get("localStorage", {}), tool, "localStorage", str(target),
        )
        if storage_error:
            return storage_error
        session, storage_error = _validate_loaded_storage(
            payload.get("sessionStorage", {}), tool, "sessionStorage", str(target),
        )
        if storage_error:
            return storage_error
        assert local is not None and session is not None

        from kahin.tools import pilot, storage_mirage  # noqa: PLC0415

        if url:
            navigation = _loads(await pilot.navigate(url=url))
            if isinstance(navigation, dict) and navigation.get("error"):
                return orjson.dumps(navigation, option=orjson.OPT_INDENT_2).decode()
        session_id, capture_error = await _capture_page_session(tool)
        if capture_error:
            return capture_error

        if cookies:
            outcome = _loads(await storage_mirage.mirage_cookie_set(
                cookies=_normalize_loaded_cookies(cookies),
            ))
            if isinstance(outcome, dict) and outcome.get("error"):
                return orjson.dumps(outcome, option=orjson.OPT_INDENT_2).decode()
        restore_error = await _restore_storage(tool, "localStorage", local, session_id)
        if restore_error:
            return restore_error
        restore_error = await _restore_storage(tool, "sessionStorage", session, session_id)
        if restore_error:
            return restore_error

        return orjson.dumps({
            "loaded": True,
            "path": str(target),
            "url": url,
            "cookies": len(cookies),
            "localStorage": len(local),
            "sessionStorage": len(session),
        }, option=orjson.OPT_INDENT_2).decode()
