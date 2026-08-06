"""Explicit engine capability contract.

The two browser backends are deliberately different:

* Shadow/Obscura is a fast CDP DOM/network engine and has no paint surface.
* Mirage/Camoufox is the visual, input, accessibility and mobile-audit engine.

Keeping this contract in code prevents a generic tool from accidentally
advertising a capability that its current backend cannot execute.  The tool
layer may promote a live Shadow session to Mirage when a capability requires
it; callers never need to reach for a second automation library.
"""

from __future__ import annotations

from typing import Any


_CAPABILITIES: dict[str, dict[str, Any]] = {
    "shadow": {
        "engine": "shadow",
        "backend": "Obscura",
        "transport": "cdp-websocket",
        "visual": False,
        "screenshot": False,
        "mobile_audit": False,
        "dom_stream": False,
        "accessibility": False,
        "screencast": False,
        "auto_promote_to": "mirage",
    },
    "mirage": {
        "engine": "mirage",
        "backend": "Camoufox",
        "transport": "juggler-stdio",
        "visual": True,
        "screenshot": True,
        "mobile_audit": True,
        "dom_stream": True,
        "accessibility": True,
        "screencast": True,
        "auto_promote_to": None,
    },
}


# CDP-shaped calls that cannot be fulfilled by Obscura's non-paint surface or
# are implemented by the Mirage sidecar rather than by raw Obscura CDP.
# Mirage-only MCP tools use the same contract through _require_mirage().
MIRAGE_REQUIRED_CDP: frozenset[tuple[str, str]] = frozenset({
    ("Page", "captureScreenshot"),
    ("Page", "startScreencast"),
    ("Page", "stopScreencast"),
    ("Page", "screencastFrameAck"),
    ("Page", "setFileChooserIntercept"),
    ("Page", "setFileInputFiles"),
    ("Page", "handleDialog"),
    ("Accessibility", "getFullAXTree"),
    ("Browser", "createBrowserContext"),
    ("Browser", "getCookies"),
    ("Browser", "setCookies"),
    ("Browser", "clearCookies"),
    ("Browser", "setDefaultViewport"),
    ("Browser", "setDeviceScaleFactor"),
    ("Browser", "setUserAgentOverride"),
    ("Browser", "setTouchOverride"),
    ("Browser", "setColorScheme"),
    ("Browser", "setReducedMotion"),
    ("Browser", "setLocaleOverride"),
    ("Browser", "setTimezoneOverride"),
    ("Browser", "setGeolocationOverride"),
})


def requires_mirage(domain: str, command: str) -> bool:
    """Return whether a CDP-shaped operation needs the visual backend."""
    return (domain, command) in MIRAGE_REQUIRED_CDP


def capabilities_for(engine_name: str) -> dict[str, Any]:
    """Return a copy safe for including in an MCP response."""
    try:
        return dict(_CAPABILITIES[engine_name])
    except KeyError:
        return {
            "engine": engine_name,
            "backend": "unknown",
            "transport": "unknown",
            "visual": False,
            "screenshot": False,
            "mobile_audit": False,
            "dom_stream": False,
            "accessibility": False,
            "screencast": False,
            "auto_promote_to": None,
        }
