"""Actionability engine tests — pure Python, fake probe."""

from __future__ import annotations

import pytest

from kahin.actionability import ACTIONABILITY_CHECK_JS, wait_for_ready


class FakeProbe:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    async def __call__(self, expression: str) -> dict:
        self.calls.append(expression)
        return self.responses.pop(0) if self.responses else {"code": "element_not_found"}


@pytest.mark.asyncio
async def test_returns_immediately_when_ready() -> None:
    # stability=1: a single stable observation is ready on the first tick.
    # The default stability=2 would need two identical ticks, which a
    # one-response fake probe cannot supply.
    probe = FakeProbe([{"ok": True, "x": 10.0, "y": 20.0, "width": 30.0, "height": 10.0}])
    result = await wait_for_ready(probe, "#btn", timeout=5.0, stability=1)
    assert result["ok"] is True
    assert result["x"] == 10.0 and result["y"] == 20.0
    assert len(probe.calls) == 1


@pytest.mark.asyncio
async def test_waits_for_element_to_appear() -> None:
    probe = FakeProbe([
        {"code": "element_not_found"},
        {"ok": True, "x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0},
    ])
    result = await wait_for_ready(probe, "#late", timeout=5.0, interval=0.01, stability=1)
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_waits_for_stability_across_ticks() -> None:
    # stability=2 fires only after two consecutive identical boxes, so the
    # two "moving" boxes must differ from each other (and from "settled").
    moving_1 = {"ok": True, "x": 10.0, "y": 20.0, "width": 30.0, "height": 10.0}
    moving_2 = {"ok": True, "x": 10.0, "y": 20.0, "width": 30.0, "height": 12.0}
    settled = {"ok": True, "x": 40.0, "y": 20.0, "width": 30.0, "height": 10.0}
    probe = FakeProbe([moving_1, moving_2, settled, settled])
    result = await wait_for_ready(probe, "#anim", timeout=5.0, interval=0.01, stability=2)
    assert result["ok"] is True and result["x"] == 40.0


@pytest.mark.asyncio
async def test_times_out_with_code() -> None:
    probe = FakeProbe([{"code": "element_not_found"}])
    result = await wait_for_ready(probe, "#never", timeout=0.05, interval=0.01)
    assert result["ok"] is False
    assert result["code"] in {"timeout", "element_not_found"}


@pytest.mark.asyncio
async def test_hard_failure_propagates_code() -> None:
    probe = FakeProbe([{"code": "element_not_actionable", "reason": "disabled"}])
    result = await wait_for_ready(probe, "#disabled", timeout=0.05, interval=0.01, stability=0)
    assert result["ok"] is False
    assert result["code"] == "element_not_actionable"
    assert result["reason"] == "disabled"


def test_check_js_contains_expected_guards() -> None:
    assert "elementFromPoint" in ACTIONABILITY_CHECK_JS
    assert "scrollIntoView" in ACTIONABILITY_CHECK_JS
    assert "aria-disabled" in ACTIONABILITY_CHECK_JS
