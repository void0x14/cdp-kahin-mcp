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
dispatch is never blocked. ``Browser.setScreencastOptions`` exists only in
upstream Chrome Juggler — Camoufox removed it, so nothing here uses it.
Video-file recording (``Page.videoRecordingStarted``) is NOT wired: it needs
browser-context video options this server does not create.

Nothing here is faked: every tool maps to a real Juggler method or waits on
the real Page.screencastFrame event.
"""

from __future__ import annotations

import orjson

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
        w = _normalize_dim(width)
        h = _normalize_dim(height)
        q = _clamp(quality, 1, 100)
        engine = _mirage_engine()
        try:
            await engine.ensure_page()
            result = await engine.call("Page.startScreencast", {
                "width": w,
                "height": h,
                "quality": q,
            })
        except RuntimeError as e:
            return orjson.dumps({
                "error": f"Juggler call failed: {e}",
                "hint": "Check the engine with kahin_engine_health.",
            }, option=orjson.OPT_INDENT_2).decode()
        except Exception as e:
            return orjson.dumps({"error": f"Connection lost: {e}"}).decode()
        screencast_id = result.get("screencastId")
        if not screencast_id:
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
async def mirage_screencast_frame(screencast_id: str | None = None, timeout: float = 10.0) -> str:
    """Mirage: wait for and return the next screencast frame
    (Page.screencastFrame) as base64-JPEG in ``data`` with deviceWidth /
    deviceHeight. Waits up to ``timeout`` seconds when no frame is queued;
    frames already queued (unacked) are returned oldest-first. On return the
    frame is automatically ACKed (Page.screencastFrameAck) with its stream
    id — Camoufox holds the stream at one unacked frame, so a missing ack
    would stall it; the ack happens in THIS call (never in the reader loop)
    and a failed ack is reported without losing the frame. screencast_id
    defaults to the id from kahin_mirage_screencast_start."""
    async with _healer_ref.safe("kahin_mirage_screencast_frame", screencast_id=screencast_id, timeout=timeout):
        try:
            wait = float(timeout)
        except (TypeError, ValueError):
            return '{"error": "timeout must be a number of seconds"}'
        if wait <= 0:
            return '{"error": "timeout must be positive"}'
        err = await _require_mirage()
        if err:
            return err
        engine = _mirage_engine()
        try:
            frame = await engine.wait_for_screencast_frame(wait)
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
        sid = screencast_id or engine._screencast_id
        ack = {"sent": False, "screencastId": sid, "error": None}
        if sid:
            try:
                await engine.call("Page.screencastFrameAck", {"screencastId": sid})
                ack["sent"] = True
            except RuntimeError as e:
                ack["error"] = str(e)
            except Exception as e:
                ack["error"] = str(e)
        data = frame.get("data", "")
        return orjson.dumps({
            "data": data,
            "dataLength": len(data),
            "frameBytes": int(len(data) * 3 / 4),
            "deviceWidth": frame.get("deviceWidth"),
            "deviceHeight": frame.get("deviceHeight"),
            "format": "jpeg",
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
        try:
            result = await engine.call("Page.stopScreencast")
        except RuntimeError as e:
            return orjson.dumps({
                "error": f"Juggler call failed: {e}",
                "hint": "Check the engine with kahin_engine_health.",
            }, option=orjson.OPT_INDENT_2).decode()
        except Exception as e:
            return orjson.dumps({"error": f"Connection lost: {e}"}).decode()
        discarded = engine.clear_screencast()
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
