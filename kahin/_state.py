"""Internal state — shared browser engine instance."""

import asyncio
from collections import deque
from typing import Any

from kahin.the_twins.chassis import BrowserEngine

_current_engine: BrowserEngine | None = None
# Browser lifecycle calls can arrive concurrently from an MCP client.  Keep
# the check/stop/start sequence atomic so two requests can never boot two
# browser processes before either one publishes its engine to this module.
_lifecycle_lock = asyncio.Lock()
_current_event_log: deque[dict[str, Any]] = deque(maxlen=5000)
_network_requests: deque[dict[str, Any]] = deque(maxlen=10000)
_console_messages: deque[dict[str, Any]] = deque(maxlen=5000)


def clear_state() -> None:
    _current_event_log.clear()
    _network_requests.clear()
    _console_messages.clear()
