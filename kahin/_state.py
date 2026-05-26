"""Internal state — shared browser engine instance."""

from kahin.the_twins.chassis import BrowserEngine

_current_engine: BrowserEngine | None = None
_current_event_log: list[dict] = []
