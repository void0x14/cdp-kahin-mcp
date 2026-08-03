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
import tempfile
from pathlib import Path
from typing import Any, Callable

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
        # targetId -> Juggler sessionId, learned from attachedToTarget events.
        self._sessions: dict[str, str] = {}
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

    async def start(self, headless: bool = True, port: int = 0, **kwargs: Any) -> EngineContext:
        del port  # Juggler pipe: no port.
        # BrowserForge fingerprint -> CAMOU_CONFIG_* env (master plan §2.1.5).
        # Every start() draws a fresh identity; the sidecar passes our
        # environment through to the Camoufox child verbatim (pipe.zig
        # buildEnvp reads /proc/self/environ).
        opts = launch_options() if launch_options is not None else {"env": {}, "firefox_user_prefs": {}}
        env = {**os.environ, **opts["env"]}

        # firefox_user_prefs -> <profile>/user.js (webgl etc. must be set
        # before the browser boots; the sidecar only mkdirs the profile).
        profile_dir = Path(tempfile.mkdtemp(prefix="kahin-fp-"))
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
            await self.stop()
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
                        self._track_context(data)
                        self._track_chooser(data)
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
        elif method == "Browser.detachedFromTarget":
            target_id = params.get("targetId")
            if target_id:
                self._sessions.pop(target_id, None)
                if self._current_target == target_id:
                    self._current_target = next(iter(self._sessions), None)

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
        msg: dict[str, Any] = {"id": self._msg_id, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[self._msg_id] = fut
        self._process.stdin.write((json.dumps(msg) + "\n").encode())
        await self._process.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout=_REQUEST_TIMEOUT)
        except TimeoutError:
            self._pending.pop(self._msg_id, None)
            raise RuntimeError(f"Mirage: response timeout ({_REQUEST_TIMEOUT:.0f}s) for {method}")

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
        deadline = loop.time() + 5.0
        while target_id not in self._sessions:
            if loop.time() > deadline:
                raise RuntimeError(f"session for target {target_id} never attached")
            await asyncio.sleep(0.05)
        self._current_target = target_id
        return {"targetId": target_id, "sessionId": self._sessions[target_id]}

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

    # --- liveness ---

    async def send_cdp(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Removed in Faz 9 Task 3 — the Juggler wire is call() only."""
        raise RuntimeError(
            "Mirage.send_cdp was removed; use Mirage.call(method, params, session_id)"
        )

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
        result = await self.call("Page.captureScreenshot", {"format": format, "fullPage": full_page})
        data = result.get("data")
        if data is None:
            raise RuntimeError(f"captureScreenshot returned no data: {result}")
        return base64.b64decode(data)

    async def stop(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        proc, self._process = self._process, None
        if proc is None:
            return
        if proc.stdin:
            proc.stdin.close()  # stdin EOF -> sidecar stops the browser
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
        if self._stderr_file is not None:
            self._stderr_file.close()
            self._stderr_file = None