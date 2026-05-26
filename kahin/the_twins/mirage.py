"""the_twins/mirage.py — Camoufox Engine (serap, stealth)."""

from __future__ import annotations

import os
from typing import Any

from kahin._chrome import find_chrome
from kahin.the_twins.chassis import BrowserEngine, EngineContext


class Mirage(BrowserEngine):
    """Stealth browser engine via Camoufox (Chromium stealth mode)."""

    async def start(self, headless: bool = True, port: int = 9242, **kwargs: Any) -> EngineContext:
        args = [
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
            args.append("--headless=new")

        env = kwargs.get("env", {})
        launch_env = dict(os.environ)
        launch_env.update(env)

        return await self._init_engine(args, env=launch_env, engine_name="mirage", port=port)
