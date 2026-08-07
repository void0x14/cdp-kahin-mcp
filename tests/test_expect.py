"""Retrying assertion engine tests — pure Python, fake probe."""

from __future__ import annotations

import pytest

from kahin.expect import (
    ExpectationError,
    attribute_js,
    enabled,
    expect_until,
    has_attribute,
    has_text,
    has_value,
    text_js,
    value_js,
    visible,
)


class FakeProbe:
    def __init__(self, states: list[dict]) -> None:
        self.states = list(states)

    async def __call__(self, expression: str) -> dict:
        return self.states.pop(0) if self.states else {"text": "", "value": "", "ok": False}


@pytest.mark.asyncio
async def test_expect_until_returns_first_match() -> None:
    probe = FakeProbe([{"text": "Loading", "value": "", "ok": True}])
    result = await expect_until(probe, "#status", has_text("Loading"), timeout=5.0, interval=0.01)
    assert result["text"] == "Loading"


@pytest.mark.asyncio
async def test_expect_until_retries_until_match() -> None:
    probe = FakeProbe(
        [
            {"text": "Loading", "value": "", "ok": True},
            {"text": "Loading", "value": "", "ok": True},
            {"text": "Done", "value": "", "ok": True},
        ]
    )
    result = await expect_until(probe, "#status", has_text("Done"), timeout=5.0, interval=0.01)
    assert result["text"] == "Done"


@pytest.mark.asyncio
async def test_expect_until_raises_on_timeout() -> None:
    probe = FakeProbe([{"text": "Loading", "value": "", "ok": True}])
    with pytest.raises(ExpectationError) as exc:
        await expect_until(probe, "#status", has_text("Never"), timeout=0.05, interval=0.01)
    assert "Never" in str(exc.value)
    assert exc.value.selector == "#status"
    assert exc.value.timeout == 0.05


@pytest.mark.asyncio
async def test_probe_failure_is_a_state_not_a_raise() -> None:
    async def boom(expression: str) -> dict:
        raise RuntimeError("page gone")

    with pytest.raises(ExpectationError) as exc:
        await expect_until(boom, "#status", has_text("Never"), timeout=0.05, interval=0.01)
    assert exc.value.actual == {"error": "probe_failed"}


def test_predicates() -> None:
    assert visible()({"ok": True}) is True
    assert visible()({"ok": False, "code": "element_not_found"}) is False
    assert enabled()({"ok": True}) is True
    assert enabled()({"ok": False, "code": "element_not_found"}) is False
    assert has_text("Done")({"text": "all Done now"}) is True
    assert has_text("Done")({"text": "Loading"}) is False
    assert has_value("42")({"value": "42"}) is True
    assert has_value("42")({"value": "7"}) is False
    assert has_attribute("href")({"attributes": {"href": "https://x"}}) is True
    assert has_attribute("href")({"attributes": {}}) is False
    assert has_attribute("href", "https://x")({"attributes": {"href": "https://x"}}) is True
    assert has_attribute("href", "https://x")({"attributes": {"href": "https://y"}}) is False
    assert has_attribute("href")({"text": "no attributes key"}) is False


def test_js_builders() -> None:
    assert "textContent" in text_js("#x")
    assert "value" in value_js("#x")
    assert "getAttribute" in attribute_js("#x", "href")
