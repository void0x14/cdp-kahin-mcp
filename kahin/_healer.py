"""_healer.py — Error logging, tracking, and self-healing."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import traceback
from collections import Counter, defaultdict
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"
LOG_FILE = LOG_DIR / "kahin.log"


class ErrorCode:
    ENGINE_START_FAILED = "ENGINE_START_FAILED"
    ENGINE_TIMEOUT = "ENGINE_TIMEOUT"
    ENGINE_ALREADY_RUNNING = "ENGINE_ALREADY_RUNNING"
    CDP_COMMAND_FAILED = "CDP_COMMAND_FAILED"
    CONNECTION_LOST = "CONNECTION_LOST"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    SESSION_CREATE_FAILED = "SESSION_CREATE_FAILED"
    NAVIGATE_FAILED = "NAVIGATE_FAILED"
    EVALUATE_FAILED = "EVALUATE_FAILED"
    SCREENSHOT_FAILED = "SCREENSHOT_FAILED"
    EXTRACT_FAILED = "EXTRACT_FAILED"
    CLICK_FAILED = "CLICK_FAILED"
    INVALID_PARAMS = "INVALID_PARAMS"
    SCHEMA_NOT_LOADED = "SCHEMA_NOT_LOADED"
    SCHEMA_QUERY_FAILED = "SCHEMA_QUERY_FAILED"
    PATTERN_DB_FAILED = "PATTERN_DB_FAILED"
    RESERVED_PORT = "RESERVED_PORT"
    UNKNOWN_ENGINE = "UNKNOWN_ENGINE"
    UNKNOWN = "UNKNOWN"


class RecoveryAction:
    NONE = "NONE"
    RESTART_ENGINE = "RESTART_ENGINE"
    RETRY = "RETRY"
    RECREATE_SESSION = "RECREATE_SESSION"
    CLEAR_STATE = "CLEAR_STATE"
    NOTIFY_USER = "NOTIFY_USER"


RECOVERY_MAP: dict[str, str] = {
    ErrorCode.ENGINE_START_FAILED: RecoveryAction.RETRY,
    ErrorCode.ENGINE_TIMEOUT: RecoveryAction.RESTART_ENGINE,
    ErrorCode.CDP_COMMAND_FAILED: RecoveryAction.RETRY,
    ErrorCode.CONNECTION_LOST: RecoveryAction.RESTART_ENGINE,
    ErrorCode.SESSION_NOT_FOUND: RecoveryAction.RECREATE_SESSION,
    ErrorCode.NAVIGATE_FAILED: RecoveryAction.RETRY,
    ErrorCode.EVALUATE_FAILED: RecoveryAction.RETRY,
    ErrorCode.SCREENSHOT_FAILED: RecoveryAction.RETRY,
    ErrorCode.CLICK_FAILED: RecoveryAction.RETRY,
    ErrorCode.EXTRACT_FAILED: RecoveryAction.RETRY,
}


@dataclass
class ErrorEntry:
    timestamp: str = ""
    level: str = "ERROR"
    tool: str = ""
    error_code: str = ErrorCode.UNKNOWN
    message: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    recovery: str = RecoveryAction.NONE
    traceback_str: str = ""
    duration_ms: float = 0.0


class ErrorTracker:
    def __init__(self) -> None:
        self._total_errors = 0
        self._total_recoveries = 0
        self._success_count = 0
        self._by_code: Counter[str] = Counter()
        self._by_tool: Counter[str] = Counter()
        self._recent: list[ErrorEntry] = []
        self._tool_timings: dict[str, list[float]] = defaultdict(list)

    def record_error(self, entry: ErrorEntry) -> None:
        self._total_errors += 1
        self._by_code[entry.error_code] += 1
        self._by_tool[entry.tool] += 1
        self._recent.append(entry)
        if len(self._recent) > 100:
            self._recent.pop(0)

    def record_success(self, tool: str, duration_ms: float) -> None:
        self._success_count += 1
        self._tool_timings[tool].append(duration_ms)
        if len(self._tool_timings[tool]) > 100:
            self._tool_timings[tool].pop(0)

    def record_recovery(self) -> None:
        self._total_recoveries += 1

    def get_stats(self) -> dict[str, Any]:
        avg_timings = {}
        for tool, timings in self._tool_timings.items():
            if timings:
                avg_timings[tool] = round(sum(timings) / len(timings), 1)
        return {
            "total_errors": self._total_errors,
            "total_successes": self._success_count,
            "total_recoveries": self._total_recoveries,
            "errors_by_code": dict(self._by_code.most_common(10)),
            "errors_by_tool": dict(self._by_tool.most_common(10)),
            "avg_timing_ms": avg_timings,
            "recent_errors": [
                {
                    "tool": e.tool,
                    "code": e.error_code,
                    "message": e.message[:100],
                    "time": e.timestamp,
                    "recovery": e.recovery,
                }
                for e in self._recent[-5:]
            ],
        }


_tracker = ErrorTracker()


def get_tracker() -> ErrorTracker:
    return _tracker


class Healer:
    def __init__(self, log_path: str | Path | None = None) -> None:
        self._log_path = Path(log_path) if log_path else LOG_FILE
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine_ref = None
        self._state_ref = None

    def bind_engine(self, engine_ref: Any) -> None:
        self._engine_ref = engine_ref

    def bind_state(self, state_ref: dict[str, Any]) -> None:
        self._state_ref = state_ref

    def _write_log(self, entry: ErrorEntry) -> None:
        data = {
            "timestamp": entry.timestamp or datetime.now(timezone.utc).isoformat(),
            "level": entry.level,
            "tool": entry.tool,
            "error_code": entry.error_code,
            "message": entry.message,
            "context": entry.context,
            "recovery": entry.recovery,
            "duration_ms": round(entry.duration_ms, 1),
        }
        if entry.traceback_str:
            data["traceback"] = entry.traceback_str[:2000]
        try:
            with open(self._log_path, "a") as f:
                f.write(json.dumps(data) + "\n")
        except OSError:
            pass

    async def _execute_recovery(self, action: str, tool: str, context: dict[str, Any]) -> str | None:
        if action == RecoveryAction.NONE:
            return None
        _tracker.record_recovery()
        self._write_log(ErrorEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            level="INFO",
            tool=tool,
            error_code="RECOVERY",
            message=f"Executing recovery: {action}",
            context=context,
        ))

        if action == RecoveryAction.CLEAR_STATE:
            if self._state_ref:
                from kahin._state import clear_state
                clear_state()
            return "state cleared"

        if action == RecoveryAction.RESTART_ENGINE:
            if self._engine_ref is not None:
                try:
                    await self._engine_ref.stop()
                except Exception:
                    pass
                self._engine_ref = None
            if self._state_ref:
                self._state_ref["_current_engine"] = None
            from kahin._state import clear_state
            clear_state()
            return "engine stopped, state cleared"

        if action == RecoveryAction.RETRY:
            return "retry suggested"

        return None

    def _determine_recovery(self, error_code: str) -> str:
        return RECOVERY_MAP.get(error_code, RecoveryAction.NONE)

    @asynccontextmanager
    async def safe(
        self, tool: str, **context: Any
    ) -> AsyncGenerator[dict[str, Any], None]:
        t0 = time.time()
        entry = ErrorEntry(tool=tool, context=context)
        try:
            yield {"ok": True}
            duration = (time.time() - t0) * 1000
            _tracker.record_success(tool, duration)
        except asyncio.TimeoutError:
            duration = (time.time() - t0) * 1000
            entry.level = "ERROR"
            entry.error_code = ErrorCode.ENGINE_TIMEOUT
            entry.message = f"Timeout in {tool}"
            entry.duration_ms = duration
            entry.recovery = self._determine_recovery(entry.error_code)
            entry.traceback_str = traceback.format_exc()
            self._write_log(entry)
            _tracker.record_error(entry)
            recovery_msg = await self._execute_recovery(entry.recovery, tool, context)
            raise RuntimeError(f"{entry.error_code}: {entry.message}" + (f" [{recovery_msg}]" if recovery_msg else ""))
        except ConnectionError as e:
            duration = (time.time() - t0) * 1000
            entry.level = "ERROR"
            entry.error_code = ErrorCode.CONNECTION_LOST
            entry.message = str(e)
            entry.duration_ms = duration
            entry.recovery = self._determine_recovery(entry.error_code)
            entry.traceback_str = traceback.format_exc()
            self._write_log(entry)
            _tracker.record_error(entry)
            recovery_msg = await self._execute_recovery(entry.recovery, tool, context)
            raise RuntimeError(f"{entry.error_code}: {entry.message}" + (f" [{recovery_msg}]" if recovery_msg else ""))
        except RuntimeError as e:
            duration = (time.time() - t0) * 1000
            entry.level = "ERROR"
            entry.error_code = ErrorCode.UNKNOWN
            msg = str(e)
            if "not found" in msg.lower() or "no session" in msg.lower():
                entry.error_code = ErrorCode.SESSION_NOT_FOUND
            elif "timeout" in msg.lower():
                entry.error_code = ErrorCode.ENGINE_TIMEOUT
            elif "connection" in msg.lower() or "closed" in msg.lower() or "ws" in msg.lower():
                entry.error_code = ErrorCode.CONNECTION_LOST
            elif "start" in msg.lower() or "failed" in msg.lower():
                entry.error_code = ErrorCode.ENGINE_START_FAILED
            elif "CDP error" in msg:
                entry.error_code = ErrorCode.CDP_COMMAND_FAILED
            entry.message = msg
            entry.duration_ms = duration
            entry.recovery = self._determine_recovery(entry.error_code)
            entry.traceback_str = traceback.format_exc()
            self._write_log(entry)
            _tracker.record_error(entry)
            recovery_msg = await self._execute_recovery(entry.recovery, tool, context)
            if entry.recovery != RecoveryAction.NONE:
                raise RuntimeError(f"{entry.error_code}: {entry.message} [auto-recovery: {recovery_msg}]")
            raise
        except Exception as e:
            duration = (time.time() - t0) * 1000
            entry.level = "ERROR"
            entry.error_code = ErrorCode.UNKNOWN
            entry.message = str(e)
            entry.duration_ms = duration
            entry.recovery = RecoveryAction.NOTIFY_USER
            entry.traceback_str = traceback.format_exc()
            self._write_log(entry)
            _tracker.record_error(entry)
            raise


_healer: Healer | None = None


def get_healer() -> Healer:
    global _healer
    if _healer is None:
        _healer = Healer()
    return _healer
