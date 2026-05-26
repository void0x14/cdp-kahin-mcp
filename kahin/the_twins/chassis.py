"""the_twins/chassis.py — Abstract Browser Engine (Şasi)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable


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
    @abstractmethod
    async def start(self, headless: bool = True, port: int = 0, **kwargs: Any) -> EngineContext:
        ...

    @abstractmethod
    async def stop(self) -> None:
        ...

    @abstractmethod
    async def send_cdp(self, domain: str, command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        ...

    @abstractmethod
    async def on_event(self, callback: Callable[[EventData], None]) -> None:
        ...

    @abstractmethod
    async def screenshot(self, format: str = "png", full_page: bool = False) -> bytes:
        ...
