"""Internal state — shared browser engine instance."""

from typing import Any

from kahin.the_twins.chassis import BrowserEngine

_current_engine: BrowserEngine | None = None
_current_event_log: list[dict[str, Any]] = []
_network_requests: list[dict[str, Any]] = []
_console_messages: list[dict[str, Any]] = []


def clear_state() -> None:
    _current_event_log.clear()
    _network_requests.clear()
    _console_messages.clear()
