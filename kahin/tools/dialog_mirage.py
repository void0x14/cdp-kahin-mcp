"""dialog_mirage.py — Mirage (Camoufox/Juggler) dialog/download/worker/ws tools.

Faz 9 Task 3: Juggler has NO list methods for dialogs, downloads, workers or
web sockets — they are event-driven (verified against Protocol.js):

- dialogs   -> Page.dialogOpened / dialogClosed events; act with
               Page.handleDialog {dialogId, accept, promptText?}
- downloads -> Browser.downloadCreated {uuid, pageTargetId, url,
               suggestedFileName} / downloadFinished {uuid, canceled?, error?};
               behavior via Browser.setDownloadOptions
               {downloadOptions: {behavior: saveToDisk|cancel, downloadsDir?}}
- workers   -> Page.workerCreated {workerId, frameId, url} /
               workerDestroyed {workerId}
- websocket -> Page.webSocketCreated {wsid, requestURL} / webSocketOpened /
               webSocketClosed {error} / webSocketFrameSent|Received

The list tools therefore read the forwarded event buffer
(``state._current_event_log``) and merge lifecycle events into live objects.
"""

from __future__ import annotations

from typing import Any

import orjson

from kahin import _state as state
from kahin.oracle import mcp
from kahin.tools._common import _RO, _RW, _healer_ref, _mirage_call


@mcp.tool(name="kahin_mirage_dialog_list", annotations=_RO)
async def mirage_dialog_list() -> str:
    """Mirage: dialogs currently open (Page.dialogOpened minus dialogClosed),
    read from the forwarded event buffer."""
    async with _healer_ref.safe("kahin_mirage_dialog_list"):
        open_dialogs: dict[str, dict[str, Any]] = {}
        for e in state._current_event_log:
            if e["event"] == "Page.dialogOpened":
                params = e.get("params") or {}
                open_dialogs[params.get("dialogId", "?")] = {
                    "dialogId": params.get("dialogId"),
                    "type": params.get("type"),
                    "message": params.get("message"),
                    "defaultValue": params.get("defaultValue"),
                }
            elif e["event"] == "Page.dialogClosed":
                open_dialogs.pop((e.get("params") or {}).get("dialogId", "?"), None)
        return orjson.dumps(list(open_dialogs.values()), option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_dialog_accept", annotations=_RW)
async def mirage_dialog_accept(dialog_id: str, prompt_text: str | None = None) -> str:
    """Mirage: accept a dialog (Page.handleDialog accept=true). prompt_text is
    used for prompt() dialogs."""
    async with _healer_ref.safe("kahin_mirage_dialog_accept", dialog_id=dialog_id[:80]):
        params: dict[str, Any] = {"dialogId": dialog_id, "accept": True}
        if prompt_text is not None:
            params["promptText"] = prompt_text
        return await _mirage_call("Page.handleDialog", params)


@mcp.tool(name="kahin_mirage_dialog_dismiss", annotations=_RW)
async def mirage_dialog_dismiss(dialog_id: str) -> str:
    """Mirage: dismiss (cancel) a dialog (Page.handleDialog accept=false)."""
    async with _healer_ref.safe("kahin_mirage_dialog_dismiss", dialog_id=dialog_id[:80]):
        return await _mirage_call("Page.handleDialog", {"dialogId": dialog_id, "accept": False})


@mcp.tool(name="kahin_mirage_download_list", annotations=_RO)
async def mirage_download_list() -> str:
    """Mirage: downloads seen so far (Browser.downloadCreated/downloadFinished
    events), with finished/canceled/error status merged per uuid."""
    async with _healer_ref.safe("kahin_mirage_download_list"):
        downloads: dict[str, dict[str, Any]] = {}
        for e in state._current_event_log:
            params = e.get("params") or {}
            if e["event"] == "Browser.downloadCreated":
                downloads[params.get("uuid", "?")] = {
                    "uuid": params.get("uuid"),
                    "pageTargetId": params.get("pageTargetId"),
                    "frameId": params.get("frameId"),
                    "url": params.get("url"),
                    "suggestedFileName": params.get("suggestedFileName"),
                    "status": "in-progress",
                }
            elif e["event"] == "Browser.downloadFinished":
                d = downloads.get(params.get("uuid", ""))
                if d:
                    d["status"] = "canceled" if params.get("canceled") else (
                        f"failed: {params.get('error')}" if params.get("error") else "finished"
                    )
        return orjson.dumps(list(downloads.values()), option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_download_save", annotations=_RW)
async def mirage_download_save(downloads_dir: str | None = None) -> str:
    """Mirage: save downloads to disk instead of canceling them
    (Browser.setDownloadOptions behavior=saveToDisk, optional downloadsDir)."""
    async with _healer_ref.safe("kahin_mirage_download_save", downloads_dir=downloads_dir or ""):
        options: dict[str, Any] = {"behavior": "saveToDisk"}
        if downloads_dir:
            options["downloadsDir"] = downloads_dir
        return await _mirage_call("Browser.setDownloadOptions", {"downloadOptions": options})


@mcp.tool(name="kahin_mirage_worker_list", annotations=_RO)
async def mirage_worker_list() -> str:
    """Mirage: web workers alive on the page (Page.workerCreated minus
    workerDestroyed), read from the forwarded event buffer."""
    async with _healer_ref.safe("kahin_mirage_worker_list"):
        workers: dict[str, dict[str, Any]] = {}
        for e in state._current_event_log:
            params = e.get("params") or {}
            if e["event"] == "Page.workerCreated":
                workers[params.get("workerId", "?")] = {
                    "workerId": params.get("workerId"),
                    "frameId": params.get("frameId"),
                    "url": params.get("url"),
                }
            elif e["event"] == "Page.workerDestroyed":
                workers.pop((e.get("params") or {}).get("workerId", "?"), None)
        return orjson.dumps(list(workers.values()), option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_websocket_list", annotations=_RO)
async def mirage_websocket_list() -> str:
    """Mirage: web sockets seen on the page (Page.webSocketCreated/Opened/
    Closed events), merged per wsid with opcode/data of the last frame."""
    async with _healer_ref.safe("kahin_mirage_websocket_list"):
        sockets: dict[str, dict[str, Any]] = {}
        for e in state._current_event_log:
            params = e.get("params") or {}
            name = e["event"]
            if name == "Page.webSocketCreated":
                sockets[params.get("wsid", "?")] = {
                    "wsid": params.get("wsid"),
                    "frameId": params.get("frameId"),
                    "requestURL": params.get("requestURL"),
                    "effectiveURL": None,
                    "status": "created",
                    "error": None,
                    "lastOpcode": None,
                    "lastDataSize": None,
                }
            elif name == "Page.webSocketOpened":
                s = sockets.get(params.get("wsid", ""))
                if s:
                    s["effectiveURL"] = params.get("effectiveURL")
                    s["status"] = "open"
            elif name == "Page.webSocketClosed":
                s = sockets.get(params.get("wsid", ""))
                if s:
                    s["status"] = "closed"
                    s["error"] = params.get("error")
            elif name in ("Page.webSocketFrameSent", "Page.webSocketFrameReceived"):
                s = sockets.get(params.get("wsid", ""))
                if s:
                    s["lastOpcode"] = params.get("opcode")
                    s["lastDataSize"] = len(params.get("data", ""))
        return orjson.dumps(list(sockets.values()), option=orjson.OPT_INDENT_2).decode()