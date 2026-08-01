"""the_twins/mirage.py — Mirage: real Camoufox via the Zig IPC sidecar.

The sidecar (camoufox-harness/core/ipc_main.zig) owns the Juggler pipe and
exposes a CDP-shaped JSON-over-stdio contract: requests
{"id","domain","command","params"} -> {"id","result"|"error"}; events flow
out as {"method","params","sessionId"} lines (Console.messageAdded etc.).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from kahin.the_twins.chassis import BrowserEngine, EngineContext, EventData

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30.0

_SIDECAR_BUILD_ARGS = (
    "build-exe",
    "--dep", "driver",
    "-Mroot=ipc_main.zig",
    "-Mdriver=driver.zig",
    "-O", "ReleaseSafe",
)


def _sidecar_bin() -> Path:
    env = os.environ.get("KAHIN_ZIG_CORE")
    base = Path(env) if env else Path(__file__).resolve().parents[2] / "camoufox-harness" / "core"
    bin_path = base / "zig-out" / "bin" / "kahin-sidecar"
    if not bin_path.is_file():
        _build_sidecar(base, bin_path)
    return bin_path


def _build_sidecar(base: Path, bin_path: Path) -> None:
    """Build the sidecar on demand — an MCP server must not need manual steps."""
    zig = _find_zig()
    logger.info("Building sidecar (first use): %s -> %s", zig, bin_path)
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [str(zig), *_SIDECAR_BUILD_ARGS, f"-femit-bin={bin_path}"],
        cwd=base,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Sidecar build failed (zig {zig}):\n{proc.stderr[-2000:]}")


def _find_zig() -> Path:
    """Locate a 0.16.x zig: KAHIN_ZIG_BIN (explicit), the known 0.16 install,
    or PATH. Non-0.16 zig builds fail (stdlib drift), so verify the version."""
    env = os.environ.get("KAHIN_ZIG_BIN")
    candidates = [Path(env)] if env else []
    candidates.append(Path.home() / ".local" / "opt" / "zig-x86_64-linux-0.16.0" / "zig")
    which = shutil.which("zig")
    if which:
        candidates.append(Path(which))
    for zig in candidates:
        if not zig.is_file():
            continue
        try:
            version = subprocess.run(
                [str(zig), "version"], capture_output=True, text=True, timeout=10, check=False
            ).stdout
        except OSError:
            continue
        if version.startswith("0.16."):
            return zig
    raise RuntimeError(
        "Zig 0.16.x not found (the sidecar build requires it). "
        "Install it or set KAHIN_ZIG_BIN."
    )


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
    """Stealth Camoufox engine speaking CDP-shaped JSON over the Zig sidecar."""

    async def start(self, headless: bool = True, port: int = 0, **kwargs: Any) -> EngineContext:
        del headless, port, kwargs  # Juggler pipe: no headless flag, no port
        log_dir = Path(__file__).resolve().parents[2] / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        # File lives as long as the child process, not a with-block.
        self._stderr_file = await asyncio.to_thread(open, log_dir / "kahin-sidecar.err", "ab")
        self._process = await asyncio.create_subprocess_exec(
            str(_sidecar_bin()),
            str(_camoufox_bin()),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self._stderr_file,
        )
        self._start_reader()
        return EngineContext(
            engine_name="mirage",
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
        msg = {"id": self._msg_id, "domain": domain, "command": command, "params": params or {}}
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
