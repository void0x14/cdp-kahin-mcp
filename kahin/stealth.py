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

from typing import Any

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
