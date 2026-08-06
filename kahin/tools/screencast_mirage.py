"""screencast_mirage.py — Mirage (Camoufox/Juggler) live screencast tools (Gap D).

Camoufox exposes a real-time JPEG screencast over Juggler (verified against
microsoft/playwright ``browser_patches/firefox/juggler/protocol/Protocol.js``
and camoufox's ``nsScreencastService.cpp``):

- ``Page.startScreencast {width, height, quality}`` -> ``{screencastId}`` —
  starts streaming base64-JPEG frames; quality 1..100, sizes are clamped
  to 10..10000 and normalized to even numbers (Playwright's driver does the
  same clamp/normalize).
- ``Page.screencastFrame {data, deviceWidth, deviceHeight}`` event — one
  frame; ``data`` is a base64 JPEG (NOT PNG). The event carries NO
  screencastId, so the stream id from startScreencast is tracked on the
  engine and replayed on every ack.
- ``Page.screencastFrameAck {screencastId}`` — REQUIRED per frame; camoufox
  holds the stream at kMaxFramesInFlight=1 unacked frame, so without an ack
  the stream stalls (a mismatched id is a silent no-op).
- ``Page.stopScreencast {}`` — ends the stream.

The frame tool acks automatically when it hands a frame to the caller; the
ack is awaited in the TOOL call, never inside the reader loop, so event
dispatch is never blocked. ``fresh=true`` drops and ACKs frames already in
the queue, then waits for a frame arriving after the call (useful after a
page mutation). ``Browser.setScreencastOptions`` exists only in upstream
Chrome Juggler — Camoufox removed it, so nothing here uses it.
Video-file recording (``Page.videoRecordingStarted``) is NOT wired: it needs
browser-context video options this server does not create.

Nothing here is faked: every tool maps to a real Juggler method or waits on
the real Page.screencastFrame event.
"""

from __future__ import annotations

import orjson
from numbers import Integral

from kahin._mcp import mcp
from kahin.tools._common import (
    _RO,
    _RW,
    _healer_ref,
    _mirage_engine,
    _require_mirage,
)

_MIN_SIZE = 10
_MAX_SIZE = 10000


def _clamp(value: int, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = lo
    return max(lo, min(hi, v))


def _normalize_dim(value: int) -> int:
    """Clamp to 10..10000 and round down to an even number (Juggler's
    encoded size must stay even; Playwright's driver does the same)."""
    v = _clamp(value, _MIN_SIZE, _MAX_SIZE)
    if v % 2:
        v -= 1
    return v


def _checked_int(value: object, field: str, minimum: int, maximum: int) -> tuple[int | None, str | None]:
    if isinstance(value, bool) or not isinstance(value, Integral):
        return None, orjson.dumps({
            "error": f"{field} must be an integer",
            "code": "invalid_argument",
            "field": field,
        }).decode()
    return max(minimum, min(maximum, int(value))), None


@mcp.tool(name="kahin_mirage_screencast_start", annotations=_RW)
async def mirage_screencast_start(width: int = 1280, height: int = 720, quality: int = 90) -> str:
    """Mirage: start a live JPEG screencast (Page.startScreencast). Streams
    base64-JPEG frames at up to a few fps; grab them with
    kahin_mirage_screencast_frame (which acks automatically so the stream
    keeps flowing) and end with kahin_mirage_screencast_stop. width/height
    are clamped to 10..10000 and normalized to even values, quality to
    1..100. Returns the screencastId plus the effective (clamped) size and
    quality."""
    async with _healer_ref.safe("kahin_mirage_screencast_start", width=width, height=height, quality=quality):
        err = await _require_mirage()
        if err:
            return err
        checked_width, validation_error = _checked_int(width, "width", _MIN_SIZE, _MAX_SIZE)
        if validation_error:
            return validation_error
        checked_height, validation_error = _checked_int(height, "height", _MIN_SIZE, _MAX_SIZE)
        if validation_error:
            return validation_error
        checked_quality, validation_error = _checked_int(quality, "quality", 1, 100)
        if validation_error:
            return validation_error
        assert checked_width is not None and checked_height is not None and checked_quality is not None
        w = _normalize_dim(checked_width)
        h = _normalize_dim(checked_height)
        q = checked_quality
        engine = _mirage_engine()
        async with engine._screencast_lock:
            try:
                await engine.ensure_page()
                # A page has one screencast stream.  Starting another stream
                # without stopping the first one silently replaces the id while
                # old frames remain queued and can never be acknowledged.
                if engine._screencast_id is not None:
                    await engine.call(
                        "Page.stopScreencast", session_id=engine.screencast_session_id,
                    )
                    engine.clear_screencast(wake_waiters=True)
                result = await engine.call("Page.startScreencast", {
                    "width": w,
                    "height": h,
                    "quality": q,
                })
            except RuntimeError as e:
                engine.clear_screencast(wake_waiters=True)
                return orjson.dumps({
                    "error": f"Juggler call failed: {e}",
                    "hint": "Check the engine with kahin_engine_health.",
                }, option=orjson.OPT_INDENT_2).decode()
            except Exception as e:
                engine.clear_screencast(wake_waiters=True)
                return orjson.dumps({"error": f"Connection lost: {e}"}).decode()
            screencast_id = result.get("screencastId")
            if not screencast_id:
                engine.clear_screencast(wake_waiters=True)
                return orjson.dumps({
                    "error": "Page.startScreencast returned no screencastId",
                    "result": result,
                }, option=orjson.OPT_INDENT_2).decode()
            engine.set_screencast_id(screencast_id)
        return orjson.dumps({
            "screencastId": screencast_id,
            "width": w,
            "height": h,
            "quality": q,
            "format": "jpeg",
            "result": result,
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_screencast_frame", annotations=_RW)
async def mirage_screencast_frame(
    screencast_id: str | None = None, timeout: float = 10.0, fresh: bool = False
) -> str:
    """Mirage: wait for and return the next screencast frame
    (Page.screencastFrame) as base64-JPEG in ``data`` with deviceWidth /
    deviceHeight. Waits up to ``timeout`` seconds when no frame is queued;
    frames already queued (unacked) are returned oldest-first unless fresh is
    true. With fresh=true, queued frames are discarded and ACKed, then the
    tool waits for a frame arriving after this call. On return the frame is
    automatically ACKed (Page.screencastFrameAck) with its stream id —
    Camoufox holds the stream at one unacked frame, so a missing ack would
    stall it; the ack happens in THIS call (never in the reader loop) and a
    failed ack is reported without losing the frame. screencast_id defaults
    to the id from kahin_mirage_screencast_start."""
    async with _healer_ref.safe(
        "kahin_mirage_screencast_frame",
        screencast_id=screencast_id,
        timeout=timeout,
        fresh=fresh,
    ):
        if isinstance(timeout, bool):
            return '{"error": "timeout must be a finite number of seconds", "code": "invalid_argument"}'
        try:
            wait = float(timeout)
        except (TypeError, ValueError, OverflowError):
            return '{"error": "timeout must be a number of seconds"}'
        if not isinstance(wait, float) or not wait == wait or wait in (float("inf"), float("-inf")) or wait <= 0:
            return '{"error": "timeout must be positive"}'
        wait = min(wait, 120.0)
        if not isinstance(fresh, bool):
            return '{"error": "fresh must be a boolean", "code": "invalid_argument"}'
        err = await _require_mirage()
        if err:
            return err
        engine = _mirage_engine()
        dropped = 0
        async with engine._screencast_lock:
            sid = screencast_id or engine._screencast_id
            active_sid = engine._screencast_id
            owner_session = engine.screencast_session_id
            generation = engine.screencast_generation
        if screencast_id is not None and active_sid is not None and screencast_id != active_sid:
            return orjson.dumps({
                "error": "screencastId does not match the active stream",
                "activeScreencastId": active_sid,
            }, option=orjson.OPT_INDENT_2).decode()
        if sid is None and engine.screencast_pending()["pending"]:
            return '{"error": "queued screencast frames have no active screencastId"}'
        if fresh:
            async with engine._screencast_lock:
                dropped = engine.drain_screencast_frames()
            if dropped and not sid:
                return '{"error": "cannot ACK fresh screencast frames without a screencastId"}'
            try:
                for _ in range(dropped):
                    await engine.call(
                        "Page.screencastFrameAck", {"screencastId": sid},
                        session_id=owner_session,
                    )
            except Exception as e:  # noqa: BLE001
                return orjson.dumps({
                    "error": f"could not ACK discarded screencast frames: {e}",
                    "droppedFrames": dropped,
                }).decode()
        try:
            frame = await engine.wait_for_screencast_frame(wait, generation=generation)
        except Exception as e:  # noqa: BLE001
            return orjson.dumps({"error": f"Connection lost: {e}"}).decode()
        if frame is None:
            if not engine.is_alive():
                return '{"error": "browser engine died while waiting for a screencast frame"}'
            return orjson.dumps({
                "error": f"no screencast frame within {wait:g}s",
                "hint": "Is a screencast running? Start one with "
                "kahin_mirage_screencast_start; an animating page emits frames.",
            }, option=orjson.OPT_INDENT_2).decode()
        ack = {"sent": False, "screencastId": sid, "error": None}
        if sid:
            try:
                await engine.call(
                    "Page.screencastFrameAck", {"screencastId": sid},
                    session_id=owner_session,
                )
                ack["sent"] = True
            except RuntimeError as e:
                ack["error"] = str(e)
            except Exception as e:
                ack["error"] = str(e)
            if ack["error"]:
                # Keep the frame available for a retry.  Camoufox also keeps
                # its corresponding frame in-flight until a valid ACK.
                async with engine._screencast_lock:
                    if engine.screencast_generation == generation:
                        engine._screencast_frames.appendleft(frame)
                        engine._screencast_event.set()
        data = frame.get("data", "")
        return orjson.dumps({
            "data": data,
            "dataLength": len(data),
            "frameBytes": int(len(data) * 3 / 4),
            "deviceWidth": frame.get("deviceWidth"),
            "deviceHeight": frame.get("deviceHeight"),
            "format": "jpeg",
            "droppedFrames": dropped,
            "ack": ack,
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_screencast_stop", annotations=_RW)
async def mirage_screencast_stop() -> str:
    """Mirage: stop the live screencast (Page.stopScreencast). Queued
    (unconsumed) frames are discarded so no stale frame survives the stream;
    the next kahin_mirage_screencast_frame call times out."""
    async with _healer_ref.safe("kahin_mirage_screencast_stop"):
        err = await _require_mirage()
        if err:
            return err
        engine = _mirage_engine()
        async with engine._screencast_lock:
            owner_session = engine.screencast_session_id
            try:
                result = await engine.call(
                    "Page.stopScreencast", session_id=owner_session,
                )
            except RuntimeError as e:
                engine.clear_screencast(wake_waiters=True)
                return orjson.dumps({
                    "error": f"Juggler call failed: {e}",
                    "hint": "Check the engine with kahin_engine_health.",
                }, option=orjson.OPT_INDENT_2).decode()
            except Exception as e:
                engine.clear_screencast(wake_waiters=True)
                return orjson.dumps({"error": f"Connection lost: {e}"}).decode()
            discarded = engine.clear_screencast(wake_waiters=True)
        return orjson.dumps({
            "stopped": True,
            "discardedFrames": discarded,
            "result": result,
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_screencast_pending", annotations=_RO)
async def mirage_screencast_pending() -> str:
    """Mirage: screencast stream health — count of queued (not yet consumed
    / ack'd) frames, the newest frame's size, and the active screencastId.
    A long-lived non-zero count means the consumer stalled (Camoufox holds
    the stream at one unacked frame); zero means the stream is idle or
    stopped."""
    async with _healer_ref.safe("kahin_mirage_screencast_pending"):
        err = await _require_mirage()
        if err:
            return err
        return orjson.dumps(
            _mirage_engine().screencast_pending(), option=orjson.OPT_INDENT_2
        ).decode()
