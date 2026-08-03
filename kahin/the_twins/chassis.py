"""the_twins/chassis.py — Browser Engine Base (Şasi)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets.asyncio.client
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)


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
    """Shared browser engine base. Subclasses implement start() and the
    liveness trio call()/is_alive()/on_death().

    Faz 9 Task 3: ``_dead`` (reader EOF / WS close) and death callbacks live
    here so both engines report liveness the same way; ``_mark_dead()`` is
    invoked from the shared background reader.
    """

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._ws: ClientConnection | None = None
        self._msg_id = 0
        self._session_id: str | None = None
        self._event_callbacks: list[Callable[[EventData], Awaitable[None] | None]] = []
        self._http: httpx.AsyncClient | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._dead: bool = False
        self._death_callbacks: list[Callable[[], Awaitable[None] | None]] = []

    @abstractmethod
    async def start(self, headless: bool = True, port: int = 0, **kwargs: Any) -> EngineContext:
        ...

    @abstractmethod
    async def call(self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None) -> dict[str, Any]:
        """Run one protocol method (Juggler-native ``Domain.method`` token)."""

    @abstractmethod
    def is_alive(self) -> bool:
        """True while the engine process is up and the transport is healthy."""

    @abstractmethod
    def on_death(self, callback: Callable[[], Awaitable[None] | None]) -> None:
        """Register a callback fired when the transport dies (reader EOF)."""

    def _mark_dead(self) -> None:
        """Reader EOF / WS close: record death and fire death callbacks once."""
        if self._dead:
            return
        self._dead = True
        for cb in self._death_callbacks:
            try:
                result = cb()
                if asyncio.iscoroutine(result):
                    asyncio.get_running_loop().create_task(result)
            except Exception:  # noqa: BLE001
                logger.exception("death callback failed")

    async def _connect_ws(self, ws_url: str) -> ClientConnection:
        """Connect to a CDP WebSocket and start the background reader task."""
        self._ws = await websockets.asyncio.client.connect(ws_url, max_size=2**24)
        self._start_reader()
        return self._ws

    def _start_reader(self) -> None:
        """Background task that drains the WS: resolves pending responses and
        dispatches events. Needed because some engines (Obscura) emit events
        after the command response, so draining only inside send_cdp loses them."""

        async def reader() -> None:
            assert self._ws is not None
            try:
                while True:
                    raw = await self._ws.recv()
                    data = json.loads(raw)
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
                # WS closed or the engine was stopped; the connection is dead.
                logger.debug("CDP reader stopped: %s", type(self).__name__)
            finally:
                self._mark_dead()

        self._reader = asyncio.create_task(reader())

    async def _init_engine(
        self,
        args: list[str],
        env: dict[str, str] | None = None,
        *,
        engine_name: str,
        port: int,
    ) -> EngineContext:
        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        page_ws = await self._wait_for_page_ws(port)
        await self._connect_ws(page_ws)
        await self.send_cdp("Page", "enable")
        await self.send_cdp("Runtime", "enable")
        return EngineContext(engine_name=engine_name, ws_url=page_ws, session_id=self._session_id)

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

    async def _create_target(self, url: str = "about:blank") -> str:
        """Open a page in the current browser connection and attach a session.

        Obscura gives every WS connection its own isolated context, so the
        page must be created inside the connection (Target.createTarget) and
        a session attached before any page-scoped command works.
        """
        result = await self.send_cdp("Target", "createTarget", {"url": url})
        target_id = result.get("targetId")
        if not target_id:
            raise RuntimeError(f"Target.createTarget returned no targetId: {result}")
        attached = await self.send_cdp("Target", "attachToTarget", {"targetId": target_id, "flatten": True})
        self._session_id = attached.get("sessionId") or f"{target_id}-session"
        return target_id

    async def stop(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        if self._ws:
            await self._ws.close()
            self._ws = None
        if self._http:
            await self._http.aclose()
            self._http = None
        if self._process:
            try:
                self._process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except TimeoutError:
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
            self._process = None

    async def send_cdp(self, domain: str, command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._ws:
            raise RuntimeError(f"{type(self).__name__} not started")
        self._msg_id += 1
        msg: dict[str, Any] = {
            "id": self._msg_id,
            "method": f"{domain}.{command}",
            "params": params or {},
        }
        if self._session_id:
            msg["sessionId"] = self._session_id
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[self._msg_id] = fut
        await self._ws.send(json.dumps(msg))
        try:
            return await asyncio.wait_for(fut, timeout=30)
        except TimeoutError:
            self._pending.pop(self._msg_id, None)
            raise RuntimeError(f"{type(self).__name__}: CDP response timeout (30s) for {domain}.{command}")

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
        data = result.get("data")
        if data is None:
            raise RuntimeError(f"captureScreenshot returned no data: {result}")
        return base64.b64decode(data)

    async def on_event(self, callback: Callable[[EventData], Awaitable[None] | None]) -> None:
        self._event_callbacks.append(callback)
