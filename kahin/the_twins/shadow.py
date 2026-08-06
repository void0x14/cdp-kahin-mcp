"""the_twins/shadow.py — Obscura Engine (gölge, hızlı CDP).

Drives the real Obscura headless browser (github.com/h4ckf0r0day/obscura)
over its CDP WebSocket server. The binary is located or auto-installed via
kahin._obscura — this engine never falls back to system Chromium.

Connection model (Obscura-specific): each WS connection owns an isolated
context, so the page is created inside the connection via Target.createTarget
and a session is attached before any page-scoped command runs.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from kahin._obscura import ensure_obscura
from kahin.the_twins.chassis import BrowserEngine, EngineContext


def _pick_free_port() -> int:
    """Ask the kernel for an unused loopback port for one Obscura child."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Obscura(BrowserEngine):
    """Fast CDP browser engine via the Obscura headless browser."""

    def __init__(self) -> None:
        super().__init__()
        self._port: int | None = None

    @property
    def port(self) -> int | None:
        """The loopback port owned by this Obscura child, if started."""
        return self._port

    async def start(self, headless: bool = True, port: int = 0, **kwargs: Any) -> EngineContext:
        del headless  # Obscura is headless-only; it has no --headless flag.
        port = port or _pick_free_port()
        self._port = port
        binary = ensure_obscura(stealth=bool(kwargs.get("stealth", False)))
        args = [
            binary,
            "serve",
            "--port",
            str(port),
            "--allow-private-network",
        ]

        try:
            self._process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._raise_if_process_exited("before WebSocket startup")
            page_ws = f"ws://127.0.0.1:{port}/devtools/browser"
            await self._connect_retry(page_ws, port)
            # A failed child can race with a foreign Obscura already listening
            # on the requested port. Never report success for that connection.
            await self._wait_for_process_stability()
            await self._create_target("about:blank")
            await self.send_cdp("Page", "enable")
            await self.send_cdp("Runtime", "enable")
            self._raise_if_process_exited("during CDP bootstrap")
            return EngineContext(
                engine_name="shadow",
                ws_url=page_ws,
                session_id=self._session_id,
                meta={"port": port},
            )
        except BaseException:
            await self.stop()
            raise

    def _raise_if_process_exited(self, phase: str) -> None:
        process = self._process
        if process is not None and process.returncode is not None:
            raise RuntimeError(
                f"Obscura exited {phase} on port {self._port} "
                f"(return code {process.returncode})"
            )

    async def _wait_for_process_stability(self, timeout: float = 0.25) -> None:
        """Let a bind/startup failure surface before accepting the WS."""
        process = self._process
        if process is None:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return
        self._raise_if_process_exited("during WebSocket startup")

    async def _connect_retry(self, ws_url: str, port: int, timeout: float = 15.0) -> None:
        """Connect to the Obscura browser WebSocket, retrying until it listens."""
        deadline = asyncio.get_running_loop().time() + timeout
        delay = 0.1
        while True:
            try:
                await self._connect_ws(ws_url)
                return
            except OSError:
                if self._process is not None and self._process.returncode is not None:
                    self._raise_if_process_exited("while waiting for WebSocket")
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError(
                        f"Obscura did not open a WebSocket on port {port} after {timeout}s"
                    ) from None
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 1.0)

    async def call(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        """Obscura is CDP: fold the Juggler-native ``Domain.method`` token back
        into the domain/command pair. session_id is owned by the CDP session
        model (``self._session_id``), not the caller for this engine."""
        del session_id
        domain, _, command = method.partition(".")
        return await self.send_cdp(domain, command, params)

    def is_alive(self) -> bool:
        """Process up + WebSocket attached + reader not marked dead."""
        return (
            not self._dead
            and self._process is not None
            and self._process.returncode is None
            and self._ws is not None
        )

    def on_death(self, callback) -> None:
        self._death_callbacks.append(callback)  # type: ignore[arg-type]
