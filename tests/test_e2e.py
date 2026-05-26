"""E2E tests for the_twins engines — real Chromium CDP connection."""

import asyncio

import pytest

from kahin.the_twins.shadow import Obscura


@pytest.mark.asyncio
async def test_obscura_start_stop():
    engine = Obscura()
    ctx = await engine.start(headless=True, port=9250)
    assert ctx.engine_name == "shadow"
    assert ctx.ws_url.startswith("ws://")
    await engine.stop()


@pytest.mark.asyncio
async def test_obscura_navigate():
    engine = Obscura()
    ctx = await engine.start(headless=True, port=9251)
    assert ctx.engine_name == "shadow"
    result = await engine.send_cdp("Page", "navigate", {"url": "about:blank"})
    assert "frameId" in result
    assert "loaderId" in result
    await engine.stop()


@pytest.mark.asyncio
async def test_obscura_evaluate():
    engine = Obscura()
    ctx = await engine.start(headless=True, port=9252)
    result = await engine.send_cdp("Runtime", "evaluate", {"expression": "1+1"})
    assert result.get("result", {}).get("value") == 2
    await engine.stop()


@pytest.mark.asyncio
async def test_obscura_screenshot():
    engine = Obscura()
    ctx = await engine.start(headless=True, port=9253)
    await engine.send_cdp("Page", "navigate", {"url": "about:blank"})
    await asyncio.sleep(0.5)
    data = await engine.screenshot()
    assert isinstance(data, bytes)
    assert len(data) > 100
    assert data.startswith(b"\x89PNG")
    await engine.stop()


@pytest.mark.asyncio
async def test_obscura_events():
    engine = Obscura()
    ctx = await engine.start(headless=True, port=9254)
    events = []

    def collector(evt):
        events.append(evt.method)

    await engine.on_event(collector)
    await engine.send_cdp("Network", "enable")
    await engine.send_cdp("Page", "navigate", {"url": "about:blank"})
    await asyncio.sleep(0.5)
    assert len(events) > 0
    await engine.stop()
