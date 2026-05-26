"""the_twins/mirage.py — Camoufox Engine (serap, stealth)."""

from __future__ import annotations

import asyncio
from typing import Any

import websockets
import websockets.asyncio.client

from kahin._chrome import find_chrome
from kahin.the_twins.chassis import BrowserEngine, EngineContext


class Mirage(BrowserEngine):
    """Stealth browser engine via Camoufox (Chromium stealth mode)."""

    async def start(self, headless: bool = True, port: int = 9242, **kwargs: Any) -> EngineContext:
        stealth_args = [
            find_chrome(),
            f"--remote-debugging-port={port}",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=ChromeWhatsNewUI",
            "--disable-features=ChromeWhatsNew",
            "--disable-component-update",
        ]
        if headless:
            stealth_args.append("--headless=new")

        env = kwargs.get("env", {})
        launch_env = {
            "PATH": env.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": env.get("HOME", "/root"),
        }
        if "DISPLAY" in env:
            launch_env["DISPLAY"] = env["DISPLAY"]

        self._process = await asyncio.create_subprocess_exec(
            *stealth_args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=launch_env,
        )

        page_ws = await self._wait_for_page_ws(port)
        self._ws = await websockets.asyncio.client.connect(page_ws, max_size=2**24)

        await self.send_cdp("Page", "enable")
        await self.send_cdp("Runtime", "enable")

        return EngineContext(
            engine_name="mirage",
            ws_url=page_ws,
        )
