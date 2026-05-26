"""the_twins/shadow.py — Obscura Engine (gölge, hızlı CDP)."""

from __future__ import annotations

import asyncio
from typing import Any

import websockets
import websockets.asyncio.client

from kahin._chrome import find_chrome
from kahin.the_twins.chassis import BrowserEngine, EngineContext


class Obscura(BrowserEngine):
    """Fast CDP browser engine via websockets (Chromium)."""

    async def start(self, headless: bool = True, port: int = 9241, **kwargs: Any) -> EngineContext:
        chrome_path = kwargs.get("chrome_path") or find_chrome()
        args = [
            chrome_path,
            f"--remote-debugging-port={port}",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
        ]
        if headless:
            args.append("--headless=new")

        self._process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )

        page_ws = await self._wait_for_page_ws(port)
        self._ws = await websockets.asyncio.client.connect(page_ws, max_size=2**24)

        await self.send_cdp("Page", "enable")
        await self.send_cdp("Runtime", "enable")

        return EngineContext(
            engine_name="shadow",
            ws_url=page_ws,
        )
