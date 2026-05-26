"""the_twins/shadow.py — Obscura Engine (gölge, hızlı CDP)."""

from __future__ import annotations

from typing import Any

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

        return await self._init_engine(args, engine_name="shadow", port=port)
