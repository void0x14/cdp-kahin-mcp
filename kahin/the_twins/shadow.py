"""the_twins/shadow.py — Obscura Engine (gölge, hızlı CDP)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

import websockets
import websockets.asyncio.client

from kahin.the_twins.chassis import BrowserEngine, EngineContext, EventData


class Obscura(BrowserEngine):
    """Fast CDP browser engine via websockets (Chromium)."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._msg_id = 0
        self._event_callbacks: list[Callable[[EventData], None]] = []

    async def start(self, headless: bool = True, port: int = 9241, **kwargs: Any) -> EngineContext:
        chrome_path = kwargs.get("chrome_path", "chromium-browser")
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

        ws_url = await self._wait_for_debug_url(port)
        self._ws = await websockets.asyncio.client.connect(ws_url, max_size=2**24)

        # Enable events
        await self.send_cdp("Target", "setAutoAttach", {
            "autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True
        })
        await self.send_cdp("Runtime", "runIfWaitingForDebugger")

        return EngineContext(
            engine_name="shadow",
            ws_url=ws_url,
        )

    async def _wait_for_debug_url(self, port: int, timeout: float = 10.0) -> str:
        import httpx
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
                    data = resp.json()
                    if "webSocketDebuggerUrl" in data:
                        return data["webSocketDebuggerUrl"]
            except (httpx.RequestError, ValueError, KeyError):
                pass
            await asyncio.sleep(0.3)
        raise RuntimeError(f"Obscura: Chrome not reachable on port {port} after {timeout}s")

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
            raise RuntimeError("Obscura not started")
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
                event_name = data["method"]
                evt = EventData(method=event_name, params=data.get("params", {}), session_id=data.get("sessionId"))
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
        import base64
        return base64.b64decode(result["data"])

    async def on_event(self, callback: Callable[[EventData], None]) -> None:
        self._event_callbacks.append(callback)
