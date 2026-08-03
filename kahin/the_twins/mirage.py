"""the_twins/mirage.py — Mirage: real Camoufox via the Zig IPC sidecar.

The sidecar (camoufox-harness/core/ipc_main.zig) owns the Juggler pipe and
exposes a Juggler-native JSON-over-stdio contract: requests
{"id","method","params","sessionId"?} -> {"id","result"|"error"}; events flow
out as {"method","params","sessionId"} lines, verbatim.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

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
        """Background task: resolve pending responses, dispatch events."""

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

        self._reader = asyncio.create_task(reader())

    async def send_cdp(self, domain: str, command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("Mirage not started")
        self._msg_id += 1
        # Juggler-native wire: domain+command fold into one method token.
        msg = {"id": self._msg_id, "method": f"{domain}.{command}", "params": params or {}}
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[self._msg_id] = fut
        self._process.stdin.write((json.dumps(msg) + "\n").encode())
        await self._process.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout=_REQUEST_TIMEOUT)
        except TimeoutError:
            self._pending.pop(self._msg_id, None)
            raise RuntimeError(f"Mirage: response timeout ({_REQUEST_TIMEOUT:.0f}s) for {domain}.{command}")

    async def screenshot(self, format: str = "png", full_page: bool = False) -> bytes:
        """Page.captureScreenshot passthrough. Juggler captures the viewport;
        full_page falls back to the viewport until the sidecar supports clips."""
        result = await self.send_cdp("Page", "captureScreenshot", {"format": format})
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
