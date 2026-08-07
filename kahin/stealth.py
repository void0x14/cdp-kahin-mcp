"""stealth.py — Self-audit probe package for the anti-detect surface.

Read-only page probes that surface automation leaks and fingerprint
inconsistencies. Every check is plain page JavaScript; nothing writes to
the DOM, listens to events, or persists state — running the audit never
contaminates the audited page.

The probe list mirrors the vectors CreepJS/botd-style detectors use:
webdriver flag, CDP/automation markers, injected binding visibility,
plugin/language sanity, native-prototype integrity, platform/timezone/
screen consistency, and WebGL/audio presence.

The probe is a single expression (no assignments, no event wiring) so the
read-only guarantee holds literally: there is nothing in the script that
could mutate the page.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import orjson

STEALTH_PROBE_JS = r"""
((r) => ({
  checks: [
    r("webdriver", navigator.webdriver === undefined || navigator.webdriver === false, navigator.webdriver),
    ((found) => r("cdp-markers", found.length === 0, found.join(",")))((["cdc_", "__selenium", "__webdriver_evaluate", "__playwright", "__pw_", "__lastWatcher"]).filter((m) => Object.getOwnPropertyNames(window).some((p) => p.includes(m)))),
    r("binding-hidden", typeof window.__kahin_dom_stream_v1 === "undefined" || window.__kahin_dom_stream_v1 === null, typeof window.__kahin_dom_stream_v1),
    r("plugins", (navigator.plugins && navigator.plugins.length) > 0, navigator.plugins && navigator.plugins.length),
    r("languages", Array.isArray(navigator.languages) && navigator.languages.length > 0, navigator.languages && navigator.languages.join(",")),
    r("platform", typeof navigator.platform === "string" && navigator.platform.length > 0, navigator.platform),
    r("oscpu", typeof navigator.oscpu === "string" && navigator.oscpu.length > 0, navigator.oscpu),
    ((tz) => r("timezone-sane", typeof tz === "string" && tz.length > 0 && tz.includes("/"), tz))((() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { return ""; } })()),
    r("screen-sane", screen && screen.width > 0 && screen.height > 0 && window.innerWidth > 0 && window.innerHeight > 0, screen.width + "x" + screen.height),
    ((text) => r("prototype-integrity", text.includes("[native code]"), "native toString"))((() => { try { return Element.prototype.getBoundingClientRect.toString(); } catch (e) { return ""; } })()),
    r("permissions-api", typeof navigator.permissions !== "undefined", typeof navigator.permissions),
    ((gl) => r("webgl", !!gl, gl ? "webgl context" : "no webgl context"))(((c) => { try { return c.getContext("webgl") || c.getContext("experimental-webgl"); } catch (e) { return null; } })(document.createElement("canvas"))),
    r("audio", typeof (window.AudioContext || window.webkitAudioContext) !== "undefined", "AudioContext present"),
    r("hardware-concurrency", typeof navigator.hardwareConcurrency === "number" && navigator.hardwareConcurrency > 0, navigator.hardwareConcurrency),
  ],
}))((check, passed, detail) => ({check, passed: !!passed, detail: String(detail == null ? "" : detail).slice(0, 200)}))
"""


def score_checks(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Score a list of ``{check, passed, detail}`` results.

    Never skips a check: unknown or malformed entries simply count as not
    passed, and the ratio is exact (no rounding) so callers can gate on a
    threshold without surprises.
    """
    total = len(checks)
    passed = sum(1 for check in checks if bool(check.get("passed")))
    return {
        "passed": passed,
        "total": total,
        "ratio": (passed / total) if total else 0.0,
    }


# Identity rotation policy (Faz 3 Task 4): a bounded, validated pin store
# mapping canonical domains to saved Faz 2 identity names. The store is
# plain JSON under the user config dir, exactly like the identity files;
# every read and write is bounded (entry count, name length, file size)
# and validated (canonical DNS keys only), so a tampered file can never
# smuggle in an arbitrary key or an oversized value.
_DOMAIN_RE = re.compile(
    r"^(localhost|[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+)$"
)
_PINS_FILE = Path.home() / ".config" / "kahin" / "pins.json"
_PINS_MAX_ENTRIES = 10_000
_PINS_MAX_BYTES = 1024 * 1024
_PINS_KEY_MAX = 253
_PIN_NAME_MAX = 64


def normalize_domain(domain: str) -> str | None:
    """Strip scheme/path/port, lowercase, and validate the hostname shape.

    Accepts ``localhost`` and dotted DNS names (single-char labels allowed,
    no empty labels, no leading/trailing hyphens, at least two labels for
    non-localhost). Anything with spaces, userinfo, underscores, an IPv6
    literal or a missing TLD is rejected, so a stored key can never carry
    injection payloads.
    """
    text = (domain or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split(":", 1)[0]
    if not text or len(text) > _PINS_KEY_MAX:
        return None
    if not _DOMAIN_RE.match(text):
        return None
    return text


def pins_path() -> Path:
    """The canonical pin store location (~/.config/kahin/pins.json)."""
    return _PINS_FILE


def load_pins() -> dict[str, str]:
    """Load the pinned domain → identity map, validated and bounded.

    A missing, unreadable, oversized or malformed store degrades to an
    empty map; a tampered store cannot smuggle in non-domain keys or
    oversized names.
    """
    try:
        if not _PINS_FILE.is_file():
            return {}
        if _PINS_FILE.stat().st_size > _PINS_MAX_BYTES:
            return {}
        payload = orjson.loads(_PINS_FILE.read_text(encoding="utf-8"))
    except (OSError, orjson.JSONDecodeError):
        return {}
    pins = payload.get("pins") if isinstance(payload, dict) else None
    if not isinstance(pins, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in pins.items():
        if len(result) >= _PINS_MAX_ENTRIES:
            break
        normalized = normalize_domain(str(key))
        if normalized is None:
            continue
        if not isinstance(value, str) or not value or len(value) > _PIN_NAME_MAX:
            continue
        result[normalized] = value
    return result


def save_pins(pins: dict[str, str]) -> None:
    """Persist the pinned map as ``{"version": 1, "pins": {...}}``.

    Only canonical domain keys with bounded string values are written;
    invalid entries are dropped so the file on disk always round-trips
    through ``load_pins`` unchanged. Raises ``OSError`` on write failure.
    """
    if not isinstance(pins, dict):
        raise ValueError("pins must be a dict")
    clean: dict[str, str] = {}
    for key, value in pins.items():
        if len(clean) >= _PINS_MAX_ENTRIES:
            break
        normalized = normalize_domain(str(key))
        if normalized is None:
            continue
        if not isinstance(value, str) or not value or len(value) > _PIN_NAME_MAX:
            continue
        clean[normalized] = value
    _PINS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PINS_FILE.write_text(
        orjson.dumps({"version": 1, "pins": clean}, option=orjson.OPT_INDENT_2).decode(),
        encoding="utf-8",
    )


def pin_identity(domain: str, name: str) -> dict[str, str] | None:
    """Pin ``name`` to a canonical ``domain``.

    Returns None on success or ``{"error", "code": "invalid_argument"}``
    when the domain cannot be normalized. The caller is responsible for
    proving the identity exists (the tool layer checks the Faz 2 store).
    """
    normalized = normalize_domain(domain)
    if normalized is None:
        return {"error": "invalid domain", "code": "invalid_argument"}
    if not isinstance(name, str) or not name or len(name) > _PIN_NAME_MAX:
        return {"error": "invalid identity name", "code": "invalid_argument"}
    pins = load_pins()
    pins[normalized] = name
    save_pins(pins)
    return None


def unpin_identity(domain: str) -> dict[str, str] | None:
    """Remove any pin for a canonical ``domain`` (idempotent).

    Returns None on success or ``{"error", "code": "invalid_argument"}``
    when the domain cannot be normalized.
    """
    normalized = normalize_domain(domain)
    if normalized is None:
        return {"error": "invalid domain", "code": "invalid_argument"}
    pins = load_pins()
    pins.pop(normalized, None)
    save_pins(pins)
    return None
