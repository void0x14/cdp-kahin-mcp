"""emulation_mirage.py — Mirage (Camoufox/Juggler) emulation tools.

Faz 9 Task 3: every emulation override maps 1:1 to a real Juggler method
(verified against Protocol.js):

- set_user_agent            -> Browser.setUserAgentOverride
- set_viewport / dsf        -> Browser.setDefaultViewport
- set_media / color / motion-> Page.setEmulatedMedia / Browser.setColorScheme
                               / Browser.setReducedMotion
- set_touch                 -> Browser.setTouchOverride
- set_locale / timezone     -> Browser.setLocaleOverride / Browser.setTimezoneOverride
- set_geolocation           -> Browser.setGeolocationOverride

Juggler has NO CDP-style Emulation.* domain — the overrides live on the
Browser (context level) and Page domains, and that is what these tools use.
"""

from __future__ import annotations

from typing import Any


from kahin.oracle import mcp
from kahin.tools._common import (
    _DW,
    _RW,
    _healer_ref,
    _mirage_call,
    _mirage_engine,
    _require_mirage,
)

# Last viewportSize sent by mirage_set_viewport — lets
# mirage_set_device_scale_factor rebuild a full viewport without needing a
# loaded document (Runtime.evaluate requires a page).
_last_viewport_size: dict[str, Any] | None = None


async def _read_viewport_size() -> dict[str, Any] | None:
    """Current viewport size via Runtime.evaluate (innerWidth/innerHeight)."""
    err = await _require_mirage()
    if err:
        return None
    try:
        raw = await _mirage_engine().call("Runtime.evaluate", {
            "expression": "(() => ({width: window.innerWidth, height: window.innerHeight}))()",
        })
    except RuntimeError:
        return None
    size = (raw.get("result") or {}).get("value")
    if not isinstance(size, dict) or not size.get("width") or not size.get("height"):
        return None
    return {"width": int(size["width"]), "height": int(size["height"])}


@mcp.tool(name="kahin_mirage_set_user_agent", annotations=_RW)
async def mirage_set_user_agent(user_agent: str) -> str:
    """Mirage: override the User-Agent header (Browser.setUserAgentOverride).
    Empty string resets to the real UA."""
    async with _healer_ref.safe("kahin_mirage_set_user_agent", user_agent=user_agent[:160]):
        return await _mirage_call("Browser.setUserAgentOverride", {"userAgent": user_agent})


@mcp.tool(name="kahin_mirage_set_viewport", annotations=_RW)
async def mirage_set_viewport(
    width: int, height: int, device_scale_factor: float | None = None,
) -> str:
    """Mirage: set the default viewport size (Browser.setDefaultViewport)."""
    async with _healer_ref.safe("kahin_mirage_set_viewport", width=width, height=height):
        global _last_viewport_size
        _last_viewport_size = {"width": width, "height": height}
        viewport: dict[str, Any] = {"viewportSize": _last_viewport_size}
        if device_scale_factor is not None:
            viewport["deviceScaleFactor"] = device_scale_factor
        return await _mirage_call("Browser.setDefaultViewport", {"viewport": viewport})


@mcp.tool(name="kahin_mirage_set_device_scale_factor", annotations=_RW)
async def mirage_set_device_scale_factor(device_scale_factor: float) -> str:
    """Mirage: override devicePixelRatio (Browser.setDefaultViewport with the
    current viewportSize + the new deviceScaleFactor)."""
    async with _healer_ref.safe("kahin_mirage_set_device_scale_factor", device_scale_factor=device_scale_factor):
        size = _last_viewport_size or await _read_viewport_size()
        viewport: dict[str, Any] = {"deviceScaleFactor": float(device_scale_factor)}
        if size:
            viewport["viewportSize"] = size
        return await _mirage_call("Browser.setDefaultViewport", {"viewport": viewport})


@mcp.tool(name="kahin_mirage_set_media", annotations=_RW)
async def mirage_set_media(media: str = "screen") -> str:
    """Mirage: emulate a media type (Page.setEmulatedMedia type=screen|print)."""
    async with _healer_ref.safe("kahin_mirage_set_media", media=media[:40]):
        return await _mirage_call("Page.setEmulatedMedia", {"type": media})


@mcp.tool(name="kahin_mirage_set_touch", annotations=_RW)
async def mirage_set_touch(has_touch: bool) -> str:
    """Mirage: emulate touch support (Browser.setTouchOverride hasTouch)."""
    async with _healer_ref.safe("kahin_mirage_set_touch", has_touch=has_touch):
        return await _mirage_call("Browser.setTouchOverride", {"hasTouch": has_touch})


@mcp.tool(name="kahin_mirage_set_color_scheme", annotations=_RW)
async def mirage_set_color_scheme(color_scheme: str = "dark") -> str:
    """Mirage: emulate prefers-color-scheme (Browser.setColorScheme:
    dark|light|no-preference)."""
    async with _healer_ref.safe("kahin_mirage_set_color_scheme", color_scheme=color_scheme[:40]):
        return await _mirage_call("Browser.setColorScheme", {"colorScheme": color_scheme})


@mcp.tool(name="kahin_mirage_set_reduced_motion", annotations=_RW)
async def mirage_set_reduced_motion(reduced_motion: str = "reduce") -> str:
    """Mirage: emulate prefers-reduced-motion (Browser.setReducedMotion:
    reduce|no-preference)."""
    async with _healer_ref.safe("kahin_mirage_set_reduced_motion", reduced_motion=reduced_motion[:40]):
        return await _mirage_call("Browser.setReducedMotion", {"reducedMotion": reduced_motion})


@mcp.tool(name="kahin_mirage_set_locale", annotations=_RW)
async def mirage_set_locale(locale: str) -> str:
    """Mirage: override the browser locale (Browser.setLocaleOverride)."""
    async with _healer_ref.safe("kahin_mirage_set_locale", locale=locale[:40]):
        return await _mirage_call("Browser.setLocaleOverride", {"locale": locale})


@mcp.tool(name="kahin_mirage_set_timezone", annotations=_RW)
async def mirage_set_timezone(timezone_id: str) -> str:
    """Mirage: override the timezone, e.g. Europe/Istanbul
    (Browser.setTimezoneOverride timezoneId)."""
    async with _healer_ref.safe("kahin_mirage_set_timezone", timezone_id=timezone_id[:60]):
        return await _mirage_call("Browser.setTimezoneOverride", {"timezoneId": timezone_id})


@mcp.tool(name="kahin_mirage_set_geolocation", annotations=_DW)
async def mirage_set_geolocation(
    latitude: float = 0.0, longitude: float = 0.0, accuracy: float | None = None, clear: bool = False,
) -> str:
    """Mirage: override geolocation (Browser.setGeolocationOverride). Set
    clear=true to reset to the real position."""
    async with _healer_ref.safe("kahin_mirage_set_geolocation", latitude=latitude, longitude=longitude, clear=clear):
        if clear:
            return await _mirage_call("Browser.setGeolocationOverride", {"geolocation": None})
        geolocation: dict[str, Any] = {"latitude": latitude, "longitude": longitude}
        if accuracy is not None:
            geolocation["accuracy"] = accuracy
        return await _mirage_call("Browser.setGeolocationOverride", {"geolocation": geolocation})