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
from typing import Any

from kahin._obscura import ensure_obscura
from kahin.the_twins.chassis import BrowserEngine, EngineContext


class Obscura(BrowserEngine):
    """Fast CDP browser engine via the Obscura headless browser."""

    async def start(self, headless: bool = True, port: int = 9241, **kwargs: Any) -> EngineContext:
        binary = ensure_obscura(stealth=bool(kwargs.get("stealth", False)))
        args = [
            binary,
            "serve",
            "--port",
            str(port),
            "--allow-private-network",
        ]
        # Obscura is headless-only; there is no --headless flag.

        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        page_ws = f"ws://127.0.0.1:{port}/devtools/browser"
        await self._connect_retry(page_ws, port)
        await self._create_target("about:blank")
        await self.send_cdp("Page", "enable")
        await self.send_cdp("Runtime", "enable")
        return EngineContext(engine_name="shadow", ws_url=page_ws, session_id=self._session_id)

    async def _connect_retry(self, ws_url: str, port: int, timeout: float = 15.0) -> None:
        """Connect to the Obscura browser WebSocket, retrying until it listens."""
        deadline = asyncio.get_running_loop().time() + timeout
        delay = 0.1
        while True:
            try:
                await self._connect_ws(ws_url)
                return
            except OSError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError(
                        f"Obscura did not open a WebSocket on port {port} after {timeout}s"
                    ) from None
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 1.0)
