"""the_twins/mirage.py — Camoufox Engine (serap, stealth)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable
import base64
import httpx

import websockets
import websockets.asyncio.client

from kahin._chrome import find_chrome
from kahin.the_twins.chassis import BrowserEngine, EngineContext, EventData


class Mirage(BrowserEngine):
    """Stealth browser engine via Camoufox (Chromium stealth mode)."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._msg_id = 0
        self._event_callbacks: list[Callable[[EventData], None]] = []

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

        # Connect to a PAGE target WS, not browser-level WS
        page_ws = await self._wait_for_page_ws(port)
        self._ws = await websockets.asyncio.client.connect(page_ws, max_size=2**24)

        await self.send_cdp("Page", "enable")
        await self.send_cdp("Runtime", "enable")

        return EngineContext(
            engine_name="mirage",
            ws_url=page_ws,
        )

    async def _wait_for_page_ws(self, port: int, timeout: float = 15.0) -> str:
        """Wait for Chrome and return the first page target's WebSocket URL."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"http://127.0.0.1:{port}/json", timeout=2)
                    targets = resp.json()
                    for t in targets:
                        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                            return t["webSocketDebuggerUrl"]
            except (httpx.RequestError, ValueError, KeyError):
                pass
            await asyncio.sleep(0.5)
        raise RuntimeError(f"Mirage: no page target found on port {port} after {timeout}s")

    async def stop(self) -> None:
        if self._ws:
            await self._ws.close()
            self._ws = None
        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
            self._process = None

    async def send_cdp(self, domain: str, command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._ws:
            raise RuntimeError("Mirage not started")
        self._msg_id += 1
        msg = json.dumps({
            "id": self._msg_id,
            "method": f"{domain}.{command}",
            "params": params or {},
        })
        await self._ws.send(msg)
        while True:
            raw = await self._ws.recv()
            data = json.loads(raw)
            if "id" in data and data["id"] == self._msg_id:
                if "error" in data:
                    raise RuntimeError(f"CDP error: {data['error']}")
                return data.get("result", {})
            if "method" in data:
                evt = EventData(method=data["method"], params=data.get("params", {}), session_id=data.get("sessionId"))
                for cb in self._event_callbacks:
                    await cb(evt) if asyncio.iscoroutinefunction(cb) else cb(evt)

    async def screenshot(self, format: str = "png", full_page: bool = False) -> bytes:
        params = {"format": format}
        if full_page:
            metrics = await self.send_cdp("Page", "getLayoutMetrics")
            w = int(metrics.get("contentSize", {}).get("width", 1920))
            h = int(metrics.get("contentSize", {}).get("height", 1080))
            await self.send_cdp("Emulation", "setDeviceMetricsOverride", {
                "width": w, "height": h, "deviceScaleFactor": 1, "mobile": False
            })
        result = await self.send_cdp("Page", "captureScreenshot", params)
        return base64.b64decode(result["data"])

    async def on_event(self, callback: Callable[[EventData], None]) -> None:
        self._event_callbacks.append(callback)
