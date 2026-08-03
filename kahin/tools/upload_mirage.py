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

import orjson

from kahin._mcp import mcp
from kahin.tools._common import (
    _RW,
    _healer_ref,
    _mirage_engine,
    _require_mirage,
)
from kahin.tools.pilot_mirage import _main_frame_id


@mcp.tool(name="kahin_mirage_set_file_chooser_intercept", annotations=_RW)
async def mirage_set_file_chooser_intercept(enabled: bool) -> str:
    """Mirage: enable/disable file chooser interception
    (Page.setInterceptFileChooserDialog). While enabled, clicking a file
    input fires Page.fileChooserOpened instead of the native dialog; feed the
    chooser with kahin_mirage_upload_files."""
    async with _healer_ref.safe("kahin_mirage_set_file_chooser_intercept", enabled=enabled):
        err = await _require_mirage()
        if err:
            return err
        try:
            result = await _mirage_engine().call(
                "Page.setInterceptFileChooserDialog", {"enabled": enabled}
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
    is clicked after this call — then sets the files. ``files`` MUST be
    absolute paths and must exist; multiple files upload in one call. Returns
    an error when no chooser opens within ``timeout``."""
    async with _healer_ref.safe("kahin_mirage_upload_files", files=files[:80], timeout=timeout):
        if not files:
            return '{"error": "files must be a non-empty list of absolute paths"}'
        bad = [f for f in files if not isinstance(f, str) or not os.path.isabs(f)]
        if bad:
            return orjson.dumps({
                "error": "files must be absolute paths (relative paths are rejected)",
                "relative": bad,
            }, option=orjson.OPT_INDENT_2).decode()
        missing = [f for f in files if not os.path.isfile(f)]
        if missing:
            return orjson.dumps({
                "error": "file not found on disk",
                "missing": missing,
            }, option=orjson.OPT_INDENT_2).decode()
        try:
            wait = float(timeout)
        except (TypeError, ValueError):
            return '{"error": "timeout must be a number of seconds"}'
        if wait <= 0:
            return '{"error": "timeout must be positive"}'
        err = await _require_mirage()
        if err:
            return err
        engine = _mirage_engine()
        try:
            chooser = await engine.wait_for_chooser(wait)
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
        frame_id, frame_err = await _main_frame_id()
        if frame_err:
            return frame_err
        if not frame_id:
            return '{"error": "no main frame available"}'
        try:
            result = await engine.call("Page.setFileInputFiles", {
                "frameId": frame_id,
                "objectId": object_id,
                "files": files,
            })
        except RuntimeError as e:
            return orjson.dumps({
                "error": f"Juggler call failed: {e}",
                "hint": "Check the engine with kahin_engine_health.",
            }, option=orjson.OPT_INDENT_2).decode()
        return orjson.dumps({
            "uploaded": True,
            "files": len(files),
            "names": [os.path.basename(f) for f in files],
            "chooser": {
                "executionContextId": chooser.get("executionContextId"),
                "objectId": object_id,
                "elementType": element.get("type"),
                "elementSubtype": element.get("subtype"),
            },
            "result": result,
        }, option=orjson.OPT_INDENT_2).decode()
