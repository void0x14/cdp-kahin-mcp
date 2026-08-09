"""upload_mirage.py — Mirage (Camoufox/Juggler) file upload tools (Gap C).

Juggler supports file uploads through file-chooser interception (verified
against microsoft/playwright ``browser_patches/firefox/juggler/protocol/
Protocol.js`` and the Playwright driver):

- ``Page.setInterceptFileChooserDialog {enabled}`` — while enabled, clicking
  a <input type="file"> fires ``Page.fileChooserOpened {executionContextId,
  element: RemoteObject}`` instead of opening the native dialog. The element
  RemoteObject is the clicked node and carries an objectId — no query needed.
- ``Page.setFileInputFiles {frameId, objectId, files: [absolute paths]}`` —
  feeds the open chooser. paths MUST be absolute (Playwright's driver
  rejects relative paths); multiple files go in ONE call. There is NO dismiss
  RPC in the protocol — not calling setFileInputFiles simply leaves the page
  waiting on the chooser, so no fake cancel tool is offered.

The chooser event is tracked on the Mirage engine (``_track_chooser`` /
``wait_for_chooser`` in kahin/the_twins/mirage.py), so upload_files works
whether the input was already clicked or the click comes after the call.

Nothing here is faked: every tool maps to a real Juggler method or waits on
the real Page.fileChooserOpened event.
"""

from __future__ import annotations

import os
import math

import orjson

from kahin._mcp import mcp
from kahin.tools._common import (
    _RW,
    _healer_ref,
    _mirage_engine,
    _require_mirage,
)

_MAX_FILES = 100
_MAX_PATH_LENGTH = 4096
_MAX_FILE_PAYLOAD = 512 * 1024
_MAX_TIMEOUT = 120.0


@mcp.tool(name="kahin_mirage_set_file_chooser_intercept", annotations=_RW)
async def mirage_set_file_chooser_intercept(enabled: bool) -> str:
    """Mirage: enable/disable file chooser interception
    (Page.setInterceptFileChooserDialog). While enabled, clicking a file
    input fires Page.fileChooserOpened instead of the native dialog; feed the
    chooser with kahin_mirage_upload_files."""
    async with _healer_ref.safe("kahin_mirage_set_file_chooser_intercept", enabled=enabled):
        if not isinstance(enabled, bool):
            return '{"error": "enabled must be a boolean", "code": "invalid_argument", "field": "enabled"}'
        err = await _require_mirage()
        if err:
            return err
        engine = _mirage_engine()
        page = await engine.ensure_page()
        session_id = page.get("sessionId") if isinstance(page, dict) else None
        if not isinstance(session_id, str) or not session_id:
            return '{"error": "selected page has no live session", "code": "session_unavailable"}'
        try:
            result = await engine.call(
                "Page.setInterceptFileChooserDialog", {"enabled": enabled}, session_id=session_id,
            )
        except RuntimeError as e:
            return orjson.dumps({
                "error": f"Juggler call failed: {e}",
                "hint": "Check the engine with kahin_engine_health.",
            }, option=orjson.OPT_INDENT_2).decode()
        return orjson.dumps({"enabled": enabled, "result": result}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_upload_files", annotations=_RW)
async def mirage_upload_files(files: list[str], timeout: float = 15.0) -> str:
    """Mirage: set files on the open (or soon-to-open) file chooser
    (Page.setFileInputFiles). Requires file chooser interception enabled
    (kahin_mirage_set_file_chooser_intercept). Waits up to ``timeout`` for
    Page.fileChooserOpened — works whether the input was already clicked or
    is clicked concurrently after this call — then sets the files. The tool
    does not guess which file input to click; use
    ``kahin_mirage_click(selector=...)`` in parallel when no chooser is open.
    ``files`` MUST be absolute paths and must exist; multiple files upload in
    one call. Returns an error when no chooser opens within ``timeout``."""
    context_files = files[:10] if isinstance(files, list) else None
    async with _healer_ref.safe("kahin_mirage_upload_files", files=context_files, timeout=timeout):
        if not isinstance(files, list) or not files:
            return '{"error": "files must be a non-empty list of absolute paths"}'
        if len(files) > _MAX_FILES:
            return orjson.dumps({
                "error": f"files cannot contain more than {_MAX_FILES} paths",
                "code": "invalid_argument",
                "field": "files",
            }).decode()
        bad = [
            f for f in files
            if not isinstance(f, str) or len(f) > _MAX_PATH_LENGTH or not os.path.isabs(f)
        ]
        if bad:
            return orjson.dumps({
                "error": "files must be absolute paths of bounded length",
                "code": "invalid_argument",
                "invalid": [str(f)[:200] for f in bad[:20]],
            }, option=orjson.OPT_INDENT_2).decode()
        if sum(len(f.encode()) for f in files) > _MAX_FILE_PAYLOAD:
            return '{"error": "file path payload exceeds the 512 KiB limit", "code": "argument_too_large"}'
        missing = [f for f in files if not os.path.isfile(f)]
        if missing:
            return orjson.dumps({
                "error": "file not found on disk",
                "missing": missing[:20],
                "missingCount": len(missing),
            }, option=orjson.OPT_INDENT_2).decode()
        if isinstance(timeout, bool):
            return '{"error": "timeout must be a finite number of seconds", "code": "invalid_argument"}'
        try:
            wait = float(timeout)
        except (TypeError, ValueError, OverflowError):
            return '{"error": "timeout must be a finite number of seconds", "code": "invalid_argument"}'
        if not math.isfinite(wait) or wait <= 0:
            return '{"error": "timeout must be positive and finite", "code": "invalid_argument"}'
        wait = min(wait, _MAX_TIMEOUT)
        err = await _require_mirage()
        if err:
            return err
        engine = _mirage_engine()
        page = await engine.ensure_page()
        page_session_id = page.get("sessionId") if isinstance(page, dict) else None
        if not isinstance(page_session_id, str) or not page_session_id:
            return '{"error": "selected page has no live session", "code": "session_unavailable"}'
        try:
            chooser = await engine.wait_for_chooser(wait, session_id=page_session_id)
        except Exception as e:  # noqa: BLE001
            return orjson.dumps({"error": f"Connection lost: {e}"}).decode()
        if chooser is None:
            if not engine.is_alive():
                return '{"error": "browser engine died while waiting for the file chooser"}'
            return orjson.dumps({
                "error": f"no file chooser opened within {wait:g}s",
                "hint": "Enable interception (kahin_mirage_set_file_chooser_intercept) "
                "and click the <input type='file'>.",
            }, option=orjson.OPT_INDENT_2).decode()
        element = chooser.get("element") or {}
        object_id = element.get("objectId")
        if not object_id:
            return orjson.dumps({
                "error": "fileChooserOpened element carries no objectId",
                "chooser": chooser,
            }, option=orjson.OPT_INDENT_2).decode()
        session_id = chooser.get("_kahinSessionId")
        if not isinstance(session_id, str) or not session_id:
            return orjson.dumps({
                "error": "file chooser has no owning Juggler session",
                "code": "chooser_owner_unknown",
                "hint": "The chooser event is stale or came from an old sidecar; click the file input again.",
            }, option=orjson.OPT_INDENT_2).decode()
        if session_id not in engine._sessions.values():
            return orjson.dumps({
                "error": "file chooser session is no longer attached",
                "code": "stale_chooser",
                "sessionId": session_id,
            }, option=orjson.OPT_INDENT_2).decode()

        execution_context_id = chooser.get("executionContextId")
        frame_id = None
        if execution_context_id is not None:
            frame_id = engine.frame_id_for_execution_context(session_id, execution_context_id)
            if frame_id is None:
                return orjson.dumps({
                    "error": "file chooser execution context is stale or not mapped to a live frame",
                    "code": "stale_chooser_context",
                    "executionContextId": execution_context_id,
                    "sessionId": session_id,
                    "hint": "Refresh the frame tree and click the file input again.",
                }, option=orjson.OPT_INDENT_2).decode()
        else:
            # A legacy event may omit executionContextId. Resolve the root
            # frame on the chooser's own session; never read the mutable
            # current tab here.
            try:
                frame_tree = await engine.call("Page.getFrameTree", session_id=session_id)
            except Exception as e:  # noqa: BLE001
                return orjson.dumps({"error": f"could not resolve chooser frame: {e}"}).decode()
            root = frame_tree.get("frameTree") if isinstance(frame_tree, dict) else None
            root_frame = root.get("frame") if isinstance(root, dict) else None
            frame_id = root_frame.get("id") if isinstance(root_frame, dict) else None
        if not isinstance(frame_id, str) or not frame_id:
            return orjson.dumps({
                "error": "no live frame for the file chooser",
                "code": "frame_unavailable",
                "sessionId": session_id,
            }, option=orjson.OPT_INDENT_2).decode()
        try:
            result = await engine.call("Page.setFileInputFiles", {
                "frameId": frame_id,
                "objectId": object_id,
                "files": files,
            }, session_id=session_id)
        except RuntimeError as e:
            return orjson.dumps({
                "error": f"Juggler call failed: {e}",
                "hint": "Check the engine with kahin_engine_health.",
            }, option=orjson.OPT_INDENT_2).decode()
        return orjson.dumps({
            "uploaded": True,
            "files": len(files),
            "names": [os.path.basename(f)[:512] for f in files],
            "chooser": {
                "executionContextId": execution_context_id,
                "sessionId": session_id,
                "frameId": frame_id,
                "objectId": str(object_id)[:1024],
                "elementType": str(element.get("type", ""))[:128],
                "elementSubtype": str(element.get("subtype", ""))[:128],
            },
            "result": result,
        }, option=orjson.OPT_INDENT_2).decode()
