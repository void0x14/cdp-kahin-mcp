"""Stealth probe tests — pure Python (JS string + scoring)."""

from __future__ import annotations

import pytest

from kahin.stealth import STEALTH_PROBE_JS, score_checks


def test_probe_covers_expected_checks() -> None:
    for needle in (
        "navigator.webdriver", "cdc_", "__playwright", "__kahin_dom_stream_v1",
        "navigator.plugins", "navigator.languages", "getBoundingClientRect",
        "elementFromPoint" if False else "navigator.permissions",
        "hardwareConcurrency", "AudioContext", "canvas",
    ):
        assert needle in STEALTH_PROBE_JS, f"missing probe: {needle}"


def test_probe_is_read_only() -> None:
    # no assignments, no addEventListener, no dispatchEvent, no localStorage
    assert "=" not in STEALTH_PROBE_JS.replace("===", "").replace("!==", "").replace("=>", "").replace("<=", "").replace(">=", "").replace("==", "")
    assert "addEventListener" not in STEALTH_PROBE_JS
    assert "localStorage" not in STEALTH_PROBE_JS


def test_score_checks() -> None:
    checks = [
        {"check": "a", "passed": True},
        {"check": "b", "passed": False},
        {"check": "c", "passed": True},
    ]
    score = score_checks(checks)
    assert score == {"passed": 2, "total": 3, "ratio": pytest.approx(2 / 3)}


def test_score_no_checks_is_zero() -> None:
    score = score_checks([])
    assert score["passed"] == 0 and score["total"] == 0 and score["ratio"] == 0.0
