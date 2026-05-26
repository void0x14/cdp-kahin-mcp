"""the_twins/chassis.py — Browser Engine Base (Şasi)."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
import websockets
import websockets.asyncio.client


@dataclass
class EngineContext:
    engine_name: str
    ws_url: str
    session_id: str | None = None
    target_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class EventData:
    method: str
    params: dict[str, Any]
    session_id: str | None = None


class BrowserEngine(ABC):
    """Shared CDP engine base. Subclasses only override start()."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._msg_id = 0
        self._event_callbacks: list[Callable[[EventData], Awaitable[None] | None]] = []
        self._http: httpx.AsyncClient | None = None

    @abstractmethod
    async def start(self, headless: bool = True, port: int = 0, **kwargs: Any) -> EngineContext:
        ...

    async def _wait_for_page_ws(self, port: int, timeout: float = 15.0) -> str:
        """Wait for Chrome and return the first page target's WebSocket URL."""
        deadline = time.time() + timeout
        delay = 0.1
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=2)
        while time.time() < deadline:
            try:
                resp = await self._http.get(f"http://127.0.0.1:{port}/json")
                targets = resp.json()
                for t in targets:
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        return t["webSocketDebuggerUrl"]
            except (httpx.RequestError, ValueError, KeyError):
                pass
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 1.0)
        raise RuntimeError(f"{type(self).__name__}: no page target found on port {port} after {timeout}s")

    async def stop(self) -> None:
        if self._ws:
            await self._ws.close()
            self._ws = None
        if self._http:
            await self._http.aclose()
            self._http = None
        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
            self._process = None

    async def send_cdp(self, domain: str, command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._ws:
            raise RuntimeError(f"{type(self).__name__} not started")
        self._msg_id += 1
        msg = json.dumps({
            "id": self._msg_id,
            "method": f"{domain}.{command}",
            "params": params or {},
        })
        await self._ws.send(msg)
        while True:
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=30)
            except asyncio.TimeoutError:
                raise RuntimeError(f"{type(self).__name__}: CDP response timeout (30s) for {domain}.{command}")
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

    async def on_event(self, callback: Callable[[EventData], Awaitable[None] | None]) -> None:
        self._event_callbacks.append(callback)
