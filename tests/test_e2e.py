"""E2E tests for the_twins engines — real Chromium CDP connection."""

import asyncio

import pytest

from kahin.the_twins.shadow import Obscura


@pytest.mark.asyncio
async def test_obscura_start_stop() -> None:
    engine = Obscura()
    ctx = await engine.start(headless=True, port=9250)
    assert ctx.engine_name == "shadow"
    assert ctx.ws_url.startswith("ws://")
    await engine.stop()


@pytest.mark.asyncio
async def test_obscura_navigate() -> None:
    engine = Obscura()
    ctx = await engine.start(headless=True, port=9251)
    assert ctx.engine_name == "shadow"
    result = await engine.send_cdp("Page", "navigate", {"url": "about:blank"})
    assert "frameId" in result
    assert "loaderId" in result
    await engine.stop()


@pytest.mark.asyncio
async def test_obscura_evaluate() -> None:
    engine = Obscura()
    await engine.start(headless=True, port=9252)
    result = await engine.send_cdp("Runtime", "evaluate", {"expression": "1+1"})
    # Obscura returns the value as a float (2.0), not an int (2).
    assert float(result["result"]["value"]) == 2.0
    await engine.stop()


@pytest.mark.asyncio
async def test_obscura_screenshot() -> None:
    engine = Obscura()
    await engine.start(headless=True, port=9253)
    # Obscura v0.1.11 has no layout/paint engine — captureScreenshot is
    # unsupported upstream, so this must raise instead of returning PNG bytes.
    with pytest.raises(RuntimeError, match="captureScreenshot is not supported"):
        await engine.screenshot()
    await engine.stop()


@pytest.mark.asyncio
async def test_obscura_events() -> None:
    engine = Obscura()
    await engine.start(headless=True, port=9254)
    events: list[str] = []

    def collector(evt) -> None:
        events.append(evt.method)

    await engine.on_event(collector)
    await engine.send_cdp("Network", "enable")
    await engine.send_cdp("Page", "navigate", {"url": "about:blank"})
    await asyncio.sleep(0.5)
    assert len(events) > 0
    await engine.stop()
