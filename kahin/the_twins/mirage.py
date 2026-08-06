"""the_twins/mirage.py — Mirage: real Camoufox via the Zig IPC sidecar.

The sidecar (camoufox-harness/core/ipc_main.zig) owns the Juggler pipe and
exposes a Juggler-native JSON-over-stdio contract: requests
{"id","method","params","sessionId"?} -> {"id","result"|"error"}; events flow
out as {"method","params","sessionId"} lines, verbatim.

Faz 9 Task 3 (phantom liveness):
- ``start()`` validates boot with ``Browser.health`` (dead sidecar -> raise)
- ``call(method, params, session_id)`` is THE wire entry point (no send_cdp)
- session map is fed by ``Browser.attachedToTarget`` / ``detachedFromTarget``
  events the sidecar forwards; ``create_page/close_page/switch_page/list_pages``
  manage which target page-scoped calls run on
- reader EOF (pipe closed / child died) marks the engine dead and fires
  ``on_death`` callbacks; dead engines reject calls instead of hanging
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import tempfile
from collections import deque
from pathlib import Path
from typing import Any, Callable

from kahin.dom_stream import DOM_STREAM_BINDING_NAME
from kahin.the_twins.chassis import BrowserEngine, EngineContext, EventData

try:
    from camoufox.utils import launch_options
except ImportError:  # pragma: no cover - harness without the camoflox package
    launch_options = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30.0


def _sidecar_bin() -> Path:
    env = os.environ.get("KAHIN_ZIG_CORE")
    if env:
        path = Path(env) / "zig-out" / "bin" / "kahin-sidecar"
        if path.is_file():
            return path
        raise RuntimeError(f"KAHIN_ZIG_CORE points to a missing sidecar: {path}")
    # Installed wheel ships the sidecar inside the package (kahin/_vendor/);
    # source-tree installs use the repo copy. Prefer the package copy so the
    # pip-installed runtime (pnpm launcher -> PyPI) never needs the repo.
    pkg = Path(__file__).resolve().parents[1] / "_vendor" / "kahin-sidecar"
    if pkg.is_file():
        return pkg
    path = (
        Path(__file__).resolve().parents[2] / "camoufox-harness" / "vendor" / "bin" / "kahin-sidecar"
    )
    if not path.is_file():
        raise RuntimeError(
            f"Vendored sidecar binary missing: {path}. Rebuild it with scripts/build-sidecar.sh"
        )
    return path


def _camoufox_bin() -> Path:
    env = os.environ.get("KAHIN_CAMOUFOX_BIN")
    if env:
        path = Path(env)
        if path.is_file():
            return path
        raise RuntimeError(f"KAHIN_CAMOUFOX_BIN points to a missing file: {path}")
    versions = sorted((Path.home() / ".cache" / "camoufox" / "browsers" / "official").glob("*/camoufox-bin"))
    if not versions:
        raise RuntimeError(
            "Camoufox not found under ~/.cache/camoufox. Run `python -m camoufox fetch` "
            "or set KAHIN_CAMOUFOX_BIN."
        )
    return versions[-1]


class Mirage(BrowserEngine):
    """Stealth Camoufox engine speaking Juggler methods over the Zig sidecar."""

    def __init__(self, engine_name: str = "mirage") -> None:
        super().__init__()
        self._engine_name = engine_name
        self._stderr_file = None
        self._profile_dir: Path | None = None
        self._write_lock = asyncio.Lock()
        self._page_lock = asyncio.Lock()
        # targetId -> Juggler sessionId, learned from attachedToTarget events.
        self._sessions: dict[str, str] = {}
        self._target_infos: dict[str, dict[str, Any]] = {}
        self._current_target: str | None = None
        # sessionId -> {frameId -> executionContextId} (main world only),
        # fed by Runtime.executionContextCreated/Destroyed/ContextsCleared
        # events the sidecar forwards verbatim. DOM tools resolve a frame_id
        # to the main-world context of that frame.
        self._frame_contexts: dict[str, dict[str, str]] = {}
        # Page.fileChooserOpened (file-input click while interception is on):
        # latest params + event, consumed by wait_for_chooser (Gap C upload).
        self._pending_chooser: dict[str, Any] | None = None
        self._chooser_event = asyncio.Event()
        # Page.screencastFrame (live screencast, Gap D): params of every
        # unconsumed frame (data is base64-JPEG, ack'd on consume — see
        # wait_for_screencast_frame). The event carries NO screencastId, so
        # the stream id returned by Page.startScreencast is tracked here and
        # echoed back on every screencastFrameAck.
        self._screencast_frames: deque[dict[str, Any]] = deque(maxlen=64)
        self._screencast_event = asyncio.Event()
        self._screencast_id: str | None = None
        # Real-time DOM observation uses the browser's native binding/event
        # bridge. The page owns the bounded mutation ring; this signal only
        # wakes a waiting tool so the reader never carries DOM payloads.
        self._dom_binding_installed = False
        self._dom_init_script_installed = False
        self._dom_signal = asyncio.Event()

    async def start(self, headless: bool = True, port: int = 0, **kwargs: Any) -> EngineContext:
        del port  # Juggler pipe: no port.
        # A Mirage object is normally single-use, but resetting these fields
        # makes a stop/start cycle deterministic and prevents stale tab or
        # liveness state from leaking into a replacement browser.
        self._dead = False
        self._msg_id = 0
        self._sessions.clear()
        self._target_infos.clear()
        self._frame_contexts.clear()
        self._current_target = None
        self._dom_binding_installed = False
        self._dom_init_script_installed = False
        self._dom_signal.clear()
        # BrowserForge fingerprint -> CAMOU_CONFIG_* env (master plan §2.1.5).
        # Every start() draws a fresh identity; the sidecar passes our
        # environment through to the Camoufox child verbatim (pipe.zig
        # buildEnvp reads /proc/self/environ).
        opts = launch_options() if launch_options is not None else {"env": {}, "firefox_user_prefs": {}}
        env = {**os.environ, **opts["env"]}

        # firefox_user_prefs -> <profile>/user.js (webgl etc. must be set
        # before the browser boots; the sidecar only mkdirs the profile).
        profile_dir = Path(tempfile.mkdtemp(prefix="kahin-fp-"))
        self._profile_dir = profile_dir
        try:
            prefs = opts.get("firefox_user_prefs") or {}
            if prefs:
                lines = ["user_pref({!r}, {!r});".format(k, v) for k, v in prefs.items()]
                (profile_dir / "user.js").write_text("\n".join(lines) + "\n")

            args = [str(_sidecar_bin()), str(_camoufox_bin())]
            if not headless:
                args.append("--visible")  # visible window (stealth vs anti-bot)
            args.append(str(profile_dir))
            log_dir = Path(__file__).resolve().parents[2] / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            # File lives as long as the child process, not a with-block.
            self._stderr_file = await asyncio.to_thread(open, log_dir / "kahin-sidecar.err", "ab")
            self._process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=self._stderr_file,
                env=env,
            )
            self._start_reader()
            # Boot validation: the sidecar must report a live browser. A dead
            # sidecar (or browser that never came up) fails the start.
            health = await self.call("Browser.health")
            if not health.get("alive"):
                raise RuntimeError(
                    "Mirage boot validation failed: Browser.health reports a dead browser"
                )
            return EngineContext(
                engine_name=self._engine_name,
                ws_url="",  # IPC over stdio, not WebSocket
                meta={
                    "sidecar": str(_sidecar_bin()),
                    "camoufox": str(_camoufox_bin()),
                    "transport": "stdio-jsonl",
                },
            )
        except BaseException:
            # asyncio.wait_for(browser_start) cancels start() on a slow boot.
            # Always reap the sidecar in that path; otherwise a retry creates
            # a second Camoufox while the first orphan keeps running.
            await self.stop()
            raise

    def _start_reader(self) -> None:
        """Background task: resolve pending responses, track Juggler sessions
        (attachedToTarget/detachedFromTarget), dispatch events."""

        async def reader() -> None:
            assert self._process is not None and self._process.stdout is not None
            try:
                while True:
                    raw = await self._process.stdout.readline()
                    if not raw:
                        break  # sidecar closed stdout (browser gone)
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("sidecar sent a non-JSON line: %.120s", raw)
                        continue
                    if "id" in data:
                        fut = self._pending.pop(data["id"], None)
                        if fut is not None and not fut.done():
                            if "error" in data:
                                fut.set_exception(RuntimeError(f"CDP error: {data['error']}"))
                            else:
                                fut.set_result(data.get("result", {}))
                    elif "method" in data:
                        self._track_session(data)
                        self._track_target_url(data)
                        self._track_context(data)
                        self._track_chooser(data)
                        self._track_screencast(data)
                        self._track_dom_binding(data)
                        evt = EventData(
                            method=data["method"],
                            params=data.get("params", {}),
                            session_id=data.get("sessionId"),
                        )
                        for cb in self._event_callbacks:
                            try:
                                result = cb(evt)
                                if asyncio.iscoroutine(result):
                                    await result
                            except Exception:
                                logger.exception("event callback failed for %s", evt.method)
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.debug("Mirage reader stopped")
            finally:
                # The browser died; fail every in-flight request.
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(RuntimeError("Mirage sidecar exited"))
                self._pending.clear()
                self._mark_dead()

        self._reader = asyncio.create_task(reader())

    def _track_context(self, data: dict[str, Any]) -> None:
        """Maintain sessionId -> {frameId -> executionContextId} (main world).

        Runtime.executionContextCreated carries the frame in auxData.frameId;
        contexts with a name (e.g. __playwright_utility_world__) do NOT map
        to page DOM, so only unnamed (main world) contexts are kept.
        executionContextDestroyed drops the id; executionContextsCleared
        (full navigation) resets the session's map.
        """
        method = data.get("method")
        params = data.get("params", {}) or {}
        sid = data.get("sessionId")
        if method == "Runtime.executionContextCreated":
            ctx_id = params.get("executionContextId")
            aux = params.get("auxData") or {}
            frame_id = aux.get("frameId")
            if not ctx_id or not frame_id or not sid:
                return
            if aux.get("name"):
                return  # utility world — not page DOM
            self._frame_contexts.setdefault(sid, {})[frame_id] = ctx_id
        elif method == "Runtime.executionContextDestroyed":
            ctx_id = params.get("executionContextId")
            if not sid or not ctx_id:
                return
            for frame_id, cid in list(self._frame_contexts.get(sid, {}).items()):
                if cid == ctx_id:
                    del self._frame_contexts[sid][frame_id]
        elif method == "Runtime.executionContextsCleared":
            if sid:
                self._frame_contexts.pop(sid, None)

    def resolve_context(self, frame_id: str) -> str | None:
        """Main-world executionContextId for a frame on the current target."""
        sid = self._sessions.get(self._current_target or "")
        if not sid:
            return None
        return self._frame_contexts.get(sid, {}).get(frame_id)

    async def install_dom_stream(self, init_script: str) -> None:
        """Install the browser-native binding and init script once.

        Browser-level Juggler methods are used so the observer follows newly
        created tabs and navigated frames. The caller still evaluates the
        script in the current frame because init scripts only affect future
        documents.
        """
        if not self._dom_binding_installed:
            await self.call("Browser.addBinding", {
                "name": DOM_STREAM_BINDING_NAME,
                "script": "function() {}",
            })
            self._dom_binding_installed = True
        if not self._dom_init_script_installed:
            await self.call("Browser.setInitScripts", {
                "scripts": [{"script": init_script}],
            })
            self._dom_init_script_installed = True

    async def wait_for_dom_signal(self, timeout: float) -> bool:
        """Wait until the page reports a DOM mutation through the binding."""
        try:
            await asyncio.wait_for(self._dom_signal.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._dom_signal.clear()

    def _track_dom_binding(self, data: dict[str, Any]) -> None:
        """Wake DOM stream consumers without copying page payloads."""
        if data.get("method") != "Page.bindingCalled":
            return
        params = data.get("params", {}) or {}
        if params.get("name") == DOM_STREAM_BINDING_NAME:
            self._dom_signal.set()

    def _track_chooser(self, data: dict[str, Any]) -> None:
        """Record Page.fileChooserOpened (file input clicked while
        Page.setInterceptFileChooserDialog is enabled) into the pending slot
        and set the event so wait_for_chooser can consume it (Gap C)."""
        if data.get("method") == "Page.fileChooserOpened":
            self._pending_chooser = data.get("params", {}) or {}
            self._chooser_event.set()

    async def wait_for_chooser(self, timeout: float) -> dict[str, Any] | None:
        """Return the fileChooserOpened params, waiting up to ``timeout`` for
        one when nothing is pending yet; None on timeout.

        Covers both orders: the input was already clicked (pending chooser is
        returned immediately) or the click will come after this call (the
        event fires while we wait). The slot is cleared on consume, so a
        second upload waits for a NEW chooser.
        """
        if self._pending_chooser is None:
            try:
                await asyncio.wait_for(self._chooser_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return None
        chooser = self._pending_chooser
        self._pending_chooser = None
        self._chooser_event.clear()
        return chooser

    def _track_screencast(self, data: dict[str, Any]) -> None:
        """Queue Page.screencastFrame params (Gap D live screencast).

        The frame's ``data`` is a base64-encoded JPEG (Camoufox encodes
        JPEG, not PNG — nsScreencastService.cpp). Frames are queued BEFORE
        the client acks them; camoufox's kMaxFramesInFlight=1 stalls the
        stream when an ack is missing, so every consumed frame must be
        ack'd (the screencast_frame tool does this via
        Page.screencastFrameAck). The event params carry no screencastId —
        the stream id is remembered from Page.startScreencast.
        """
        if data.get("method") == "Page.screencastFrame":
            params = data.get("params", {}) or {}
            if params.get("data"):
                self._screencast_frames.append(params)
                self._screencast_event.set()

    async def wait_for_screencast_frame(self, timeout: float) -> dict[str, Any] | None:
        """Return the OLDEST pending screencastFrame params (base64-JPEG in
        ``data``), waiting up to ``timeout`` when nothing is queued yet;
        None on timeout. A frame stays queued until this consumes it — the
        caller must then Page.screencastFrameAck its stream id (mismatched
        ids are a no-op on Camoufox). The queue keeps up to 64 unacked
        frames, so a slow consumer sees the oldest one first.
        """
        if not self._screencast_frames:
            try:
                await asyncio.wait_for(self._screencast_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return None
        frame = self._screencast_frames.popleft()
        if not self._screencast_frames:
            self._screencast_event.clear()
        return frame

    def screencast_pending(self) -> dict[str, Any]:
        """Unacked frame count + newest frame summary + stream id — stream
        health: pending > 0 while frames wait for their ack (Camoufox holds
        at kMaxFramesInFlight=1 unacked frames, so a long-lived non-zero
        count means the consumer stalled)."""
        last = self._screencast_frames[-1] if self._screencast_frames else None
        return {
            "pending": len(self._screencast_frames),
            "screencastId": self._screencast_id,
            "active": self._screencast_id is not None,
            "last": {
                "deviceWidth": last.get("deviceWidth"),
                "deviceHeight": last.get("deviceHeight"),
                "dataLength": len(last.get("data", "")),
            }
            if last
            else None,
        }

    def set_screencast_id(self, screencast_id: str) -> None:
        """Remember the stream id returned by Page.startScreencast so the
        frame tool can ack frames whose event carries no id."""
        self._screencast_id = screencast_id

    def clear_screencast(self) -> int:
        """Drop all queued frames + the stream id (used on stopScreencast so
        no stale frame survives the stream); returns the discarded count."""
        discarded = len(self._screencast_frames)
        self._screencast_frames.clear()
        self._screencast_event.clear()
        self._screencast_id = None
        return discarded

    def _track_session(self, data: dict[str, Any]) -> None:
        """Update the targetId -> sessionId map from Juggler target events."""
        method = data.get("method")
        params = data.get("params", {}) or {}
        if method == "Browser.attachedToTarget":
            target_info = params.get("targetInfo") or {}
            target_id = target_info.get("targetId") or params.get("targetId")
            session_id = params.get("sessionId") or data.get("sessionId")
            if target_id and session_id:
                self._sessions[target_id] = session_id
                self._target_infos[target_id] = dict(target_info)
        elif method == "Browser.detachedFromTarget":
            target_id = params.get("targetId")
            if target_id:
                self._sessions.pop(target_id, None)
                self._target_infos.pop(target_id, None)
                if self._current_target == target_id:
                    self._current_target = next(iter(self._sessions), None)

    def _track_target_url(self, data: dict[str, Any]) -> None:
        """Keep CDP-shaped Target.getTargets URL data current."""
        if data.get("method") != "Page.navigationCommitted":
            return
        session_id = data.get("sessionId")
        url = (data.get("params") or {}).get("url")
        if not isinstance(session_id, str) or not isinstance(url, str):
            return
        target_id = next(
            (tid for tid, sid in self._sessions.items() if sid == session_id),
            None,
        )
        if target_id is not None:
            self._target_infos.setdefault(target_id, {}).update({"url": url})

    async def call(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        """Send one Juggler ``Domain.method`` and await its id-matched reply.

        Page-scoped methods carry the current target's sessionId unless an
        explicit session_id is given (Browser.* methods stay on the root
        session). Dead engines raise immediately instead of hanging.
        """
        if self._dead:
            raise RuntimeError("Mirage is dead")
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("Mirage not started")
        sid = session_id
        if sid is None and not method.startswith("Browser."):
            sid = self._sessions.get(self._current_target or "")
        self._msg_id += 1
        request_id = self._msg_id
        msg: dict[str, Any] = {"id": request_id, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        try:
            # Keep JSONL records intact when several MCP calls arrive at once;
            # pending replies remain fully concurrent behind this tiny write
            # critical section.
            async with self._write_lock:
                if self._process is None or self._process.stdin is None:
                    raise RuntimeError("Mirage not started")
                self._process.stdin.write((json.dumps(msg) + "\n").encode())
                await self._process.stdin.drain()
            return await asyncio.wait_for(fut, timeout=_REQUEST_TIMEOUT)
        except TimeoutError:
            self._pending.pop(request_id, None)
            raise RuntimeError(f"Mirage: response timeout ({_REQUEST_TIMEOUT:.0f}s) for {method}")
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            raise
        except Exception:
            self._pending.pop(request_id, None)
            raise

    # --- session management (Juggler target model) ---

    async def create_page(self, url: str = "about:blank", browser_context_id: str | None = None) -> dict[str, Any]:
        """Open a new tab; returns {"targetId", "sessionId"} and makes it current.

        The sessionId is learned from the Browser.attachedToTarget event the
        sidecar forwards right after Browser.newPage's reply.
        """
        params: dict[str, Any] = {"url": url}
        if browser_context_id:
            params["browserContextId"] = browser_context_id
        result = await self.call("Browser.newPage", params)
        target_id = result.get("targetId")
        if not target_id:
            raise RuntimeError(f"Browser.newPage returned no targetId: {result}")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(_REQUEST_TIMEOUT, 10.0)
        while target_id not in self._sessions:
            if loop.time() > deadline:
                raise RuntimeError(f"session for target {target_id} never attached")
            await asyncio.sleep(0.05)
        self._current_target = target_id
        return {"targetId": target_id, "sessionId": self._sessions[target_id]}

    async def ensure_page(self) -> dict[str, Any]:
        """Return the current tab, creating one lazily inside this browser.

        Browser startup intentionally does not create a second process or an
        eager throw-away page. The first page-oriented operation gets one
        about:blank tab, and subsequent operations reuse it until the caller
        explicitly asks for another tab.
        """
        async with self._page_lock:
            if self._current_target in self._sessions:
                return {
                    "targetId": self._current_target,
                    "sessionId": self._sessions[self._current_target],
                }
            if self._sessions:
                self._current_target = next(iter(self._sessions))
                target_id = self._current_target
                return {"targetId": target_id, "sessionId": self._sessions[target_id]}
            return await self.create_page("about:blank")

    async def close_page(self, target_id: str) -> dict[str, Any]:
        """Close a tab (Page.close) and forget its Juggler session."""
        session_id = self._sessions.get(target_id)
        if session_id is None:
            raise RuntimeError(f"unknown target: {target_id}")
        await self.call("Page.close", session_id=session_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2.0
        while target_id in self._sessions:
            if loop.time() > deadline:
                break
            await asyncio.sleep(0.05)
        self._sessions.pop(target_id, None)
        if self._current_target == target_id:
            self._current_target = next(iter(self._sessions), None)
        return {"closed": target_id}

    async def switch_page(self, target_id: str) -> dict[str, Any]:
        """Route page-scoped calls to an existing tab."""
        if target_id not in self._sessions:
            raise RuntimeError(f"unknown target: {target_id}")
        self._current_target = target_id
        return {"switched": target_id}

    async def list_pages(self) -> list[dict[str, Any]]:
        """All known tabs with their Juggler session ids."""
        return [
            {"targetId": tid, "sessionId": sid, "current": tid == self._current_target}
            for tid, sid in self._sessions.items()
        ]

    async def execute_cdp(
        self, domain: str, command: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Execute a CDP-shaped request through the Mirage equivalent.

        Agents often know the Chrome/CDP surface better than the Juggler
        tool names. Keep that request surface working on Camoufox while
        routing the operation to the real Juggler method or tab primitive.
        The returned object stays CDP-shaped, so callers do not need to know
        that the implementation used Mirage.
        """
        p = dict(params or {})
        method = f"{domain}.{command}"

        # These domains are event-driven in Juggler and do not need an
        # enable/disable handshake. Treat the CDP setup calls as successful
        # no-ops so a raw CDP client can keep its normal bootstrap sequence.
        if method in {
            "Page.enable",
            "Page.disable",
            "Runtime.enable",
            "Runtime.disable",
            "Network.enable",
            "Network.disable",
            "DOM.enable",
            "DOM.disable",
            "Accessibility.enable",
            "Accessibility.disable",
        }:
            return {}

        if domain == "Target":
            return await self._execute_cdp_target(command, p)
        if domain == "Input":
            return await self._execute_cdp_input(command, p)
        if domain == "Emulation":
            return await self._execute_cdp_emulation(command, p)
        if domain == "Network":
            return await self._execute_cdp_network(command, p)
        if domain == "Browser" and command == "getVersion":
            result = await self.call("Browser.getInfo", p)
            return {
                "protocolVersion": result.get("protocolVersion", ""),
                "product": result.get("product", result.get("userAgent", "")),
                "revision": result.get("revision", ""),
                "userAgent": result.get("userAgent", ""),
                "jsVersion": result.get("jsVersion", ""),
            }

        if domain == "Page" and command == "captureScreenshot":
            await self.ensure_page()
            result = await self.call("Page.captureScreenshot", p)
            return {"data": result.get("data", "")}
        if domain == "Page" and command == "close":
            target_id = self._current_target
            if target_id is None:
                raise RuntimeError("no current tab")
            await self.close_page(target_id)
            return {}

        # Page.navigate, Runtime.evaluate, frame tree, and the other
        # already CDP-shaped sidecar handlers remain direct Juggler calls.
        return await self.call(method, p)

    async def _execute_cdp_target(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        if command == "getTargets":
            infos: list[dict[str, Any]] = []
            for target_id in self._sessions:
                info = dict(self._target_infos.get(target_id, {}))
                info.update({
                    "targetId": target_id,
                    "type": info.get("type", "page"),
                    "url": info.get("url", ""),
                })
                infos.append({
                    key: info[key]
                    for key in ("targetId", "type", "browserContextId", "url")
                    if key in info and info[key] is not None
                })
            return {"targetInfos": infos}
        if command == "createTarget":
            page = await self.create_page(
                url=str(params.get("url", "about:blank")),
                browser_context_id=params.get("browserContextId"),
            )
            return {"targetId": page["targetId"]}
        if command == "closeTarget":
            target_id = params.get("targetId")
            if not isinstance(target_id, str) or not target_id:
                raise RuntimeError("Target.closeTarget requires targetId")
            await self.close_page(target_id)
            return {}
        if command == "activateTarget":
            target_id = params.get("targetId")
            if not isinstance(target_id, str):
                raise RuntimeError("Target.activateTarget requires targetId")
            await self.switch_page(target_id)
            await self.call("Page.bringToFront")
            return {}
        if command == "attachToTarget":
            target_id = params.get("targetId")
            if not isinstance(target_id, str):
                raise RuntimeError("Target.attachToTarget requires targetId")
            await self.switch_page(target_id)
            return {"sessionId": self._sessions[target_id]}
        if command == "detachFromTarget":
            return {}
        if command == "disposeBrowserContext":
            context_id = params.get("browserContextId")
            if not isinstance(context_id, str):
                raise RuntimeError("Target.disposeBrowserContext requires browserContextId")
            return await self.call("Browser.removeBrowserContext", {"browserContextId": context_id})
        if command == "setAutoAttach":
            return {}
        return await self.call(f"Target.{command}", params)

    async def _execute_cdp_input(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        if command == "insertText":
            await self.call("Page.insertText", {"text": str(params.get("text", ""))})
            return {}
        if command == "dispatchKeyEvent":
            type_map = {
                "keyDown": "keydown",
                "keyUp": "keyup",
                "rawKeyDown": "rawkeydown",
                "char": "char",
            }
            key_type = type_map.get(params.get("type"))
            if key_type is None:
                raise RuntimeError(f"unknown Input.dispatchKeyEvent type: {params.get('type')}")
            mapped: dict[str, Any] = {
                "type": key_type,
                "key": str(params.get("key", "")),
                "keyCode": int(params.get("windowsVirtualKeyCode", params.get("keyCode", 0)) or 0),
                "location": int(params.get("location", 0) or 0),
                "code": str(params.get("code", "Unidentified")),
                "repeat": bool(params.get("autoRepeat", params.get("repeat", False))),
            }
            if params.get("text") is not None:
                mapped["text"] = params["text"]
            await self.call("Page.dispatchKeyEvent", mapped)
            return {}
        if command == "dispatchMouseEvent":
            event_type = params.get("type")
            x = float(params.get("x", 0) or 0)
            y = float(params.get("y", 0) or 0)
            modifiers = int(params.get("modifiers", 0) or 0)
            if event_type == "mouseWheel":
                await self.call("Page.dispatchWheelEvent", {
                    "x": x,
                    "y": y,
                    "deltaX": float(params.get("deltaX", 0) or 0),
                    "deltaY": float(params.get("deltaY", 0) or 0),
                    "deltaZ": 0.0,
                    "modifiers": modifiers,
                })
                return {}
            type_map = {"mousePressed": "mousedown", "mouseReleased": "mouseup", "mouseMoved": "mousemove"}
            juggler_type = type_map.get(event_type)
            if juggler_type is None:
                raise RuntimeError(f"unknown Input.dispatchMouseEvent type: {event_type}")
            button_name = str(params.get("button", "none"))
            button_number = {"left": 0, "middle": 1, "right": 2, "back": 3, "forward": 4, "none": 0}.get(button_name)
            if button_number is None:
                raise RuntimeError(f"unknown mouse button: {button_name}")
            buttons = params.get("buttons")
            if buttons is None:
                buttons = {"left": 1, "right": 2, "middle": 4, "back": 8, "forward": 16}.get(button_name, 0)
                if juggler_type != "mousedown":
                    buttons = 0
            await self.call("Page.dispatchMouseEvent", {
                "type": juggler_type,
                "button": button_number,
                "x": x,
                "y": y,
                "modifiers": modifiers,
                "clickCount": int(params.get("clickCount", 1) or 1),
                "buttons": int(buttons),
            })
            return {}
        return await self.call(f"Input.{command}", params)

    async def _execute_cdp_emulation(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        if command == "setDeviceMetricsOverride":
            width = params.get("width")
            height = params.get("height")
            if width is None or height is None:
                raise RuntimeError("Emulation.setDeviceMetricsOverride requires width and height")
            viewport: dict[str, Any] = {
                "viewportSize": {"width": width, "height": height},
            }
            if params.get("deviceScaleFactor") is not None:
                viewport["deviceScaleFactor"] = params["deviceScaleFactor"]
            await self.call("Browser.setDefaultViewport", {"viewport": viewport})
            return {}
        if command == "setUserAgentOverride":
            return await self.call("Browser.setUserAgentOverride", {"userAgent": params.get("userAgent", "")})
        if command == "setTouchEmulationEnabled":
            return await self.call("Browser.setTouchOverride", {"hasTouch": bool(params.get("enabled", False))})
        if command == "setEmulatedMedia":
            return await self.call("Page.setEmulatedMedia", {"type": params.get("media", "screen")})
        if command == "setLocaleOverride":
            return await self.call("Browser.setLocaleOverride", {"locale": params.get("locale", "")})
        if command == "setTimezoneOverride":
            return await self.call("Browser.setTimezoneOverride", {"timezoneId": params.get("timezoneId", "")})
        if command == "setGeolocationOverride":
            geo = {k: params[k] for k in ("latitude", "longitude", "accuracy") if k in params}
            return await self.call("Browser.setGeolocationOverride", {"geolocation": geo or None})
        return await self.call(f"Emulation.{command}", params)

    async def _execute_cdp_network(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        if command == "continueInterceptedRequest":
            mapped = {k: params[k] for k in ("requestId", "url", "method", "headers", "postData") if k in params}
            return await self.call("Network.resumeInterceptedRequest", mapped)
        if command == "setRequestInterception":
            return await self.call(
                "Network.setRequestInterception",
                {"enabled": bool(params.get("enabled", True))},
            )
        if command == "clearBrowserCookies":
            return await self.call("Browser.clearCookies", {})
        if command == "clearBrowserCache":
            return await self.call("Browser.clearCache", {})
        if command == "setCacheDisabled":
            return await self.call("Page.setCacheDisabled", {"cacheDisabled": bool(params.get("cacheDisabled", False))})
        return await self.call(f"Network.{command}", params)

    # --- liveness ---

    async def send_cdp(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Removed in Faz 9 Task 3 — the Juggler wire is call() only."""
        raise RuntimeError(
            "Mirage.send_cdp was removed; use Mirage.call(method, params, session_id)"
        )

    async def health(self) -> dict[str, Any]:
        """Probe both the sidecar and its Camoufox child.

        ``is_alive()`` can only see the sidecar process.  Browser.health is
        answered by the sidecar's process manager and catches the stale-state
        case where Firefox has exited but the Python process has not reaped
        the sidecar yet.
        """
        if not self.is_alive():
            return {"alive": False, "state": "dead"}
        try:
            result = await self.call("Browser.health")
        except Exception as exc:  # noqa: BLE001
            self._mark_dead()
            return {"alive": False, "state": "dead", "error": str(exc)}
        if not result.get("alive"):
            self._mark_dead()
        return result

    def is_alive(self) -> bool:
        """Process up and the reader healthy (reader EOF marks death)."""
        return (
            not self._dead
            and self._process is not None
            and self._process.returncode is None
        )

    def on_death(self, callback: Callable[[], Any]) -> None:
        self._death_callbacks.append(callback)  # type: ignore[arg-type]

    async def screenshot(self, format: str = "png", full_page: bool = False) -> bytes:
        """Page.captureScreenshot passthrough. The sidecar translates
        full_page into a full-content clip (size measured via evaluate);
        otherwise the real viewport is captured."""
        await self.ensure_page()
        result = await self.call("Page.captureScreenshot", {"format": format, "fullPage": full_page})
        data = result.get("data")
        if data is None:
            raise RuntimeError(f"captureScreenshot returned no data: {result}")
        return base64.b64decode(data)

    async def stop(self) -> None:
        self._dom_signal.set()
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        proc, self._process = self._process, None
        if proc is None:
            self._sessions.clear()
            self._target_infos.clear()
            self._frame_contexts.clear()
            self._current_target = None
            if self._stderr_file is not None:
                self._stderr_file.close()
                self._stderr_file = None
            self._remove_profile()
            return
        if proc.stdin:
            try:
                proc.stdin.close()  # stdin EOF -> sidecar stops the browser
            except Exception:  # noqa: BLE001
                pass
        wait_task = asyncio.create_task(proc.wait())
        try:
            await asyncio.wait_for(asyncio.shield(wait_task), timeout=10)
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await wait_task
        except asyncio.CancelledError:
            # A cancelled MCP request must still reap the sidecar before the
            # cancellation escapes; otherwise the next tool sees a cleared
            # Python state with a live child underneath it.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await asyncio.shield(wait_task)
            raise
        finally:
            if self._stderr_file is not None:
                self._stderr_file.close()
                self._stderr_file = None
            self._sessions.clear()
            self._target_infos.clear()
            self._frame_contexts.clear()
            self._current_target = None
            self._remove_profile()

    def _remove_profile(self) -> None:
        profile, self._profile_dir = self._profile_dir, None
        if profile is not None:
            shutil.rmtree(profile, ignore_errors=True)
