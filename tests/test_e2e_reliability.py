"""Tool-layer reliability e2e over real Camoufox (Faz 1, Tasks 4-5).

Covers the locator-engine adoption on query/query_all/type/wait_selector,
the wait_selector ``state=attached|visible|enabled`` contract, and the
actionability waits on click (animation stability + disabled-becomes-enabled).
Every scenario drives the same async functions the MCP server exposes, so the
JSON-string tool contract is what gets verified.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import quote

import pytest
from pytest_asyncio import fixture as async_fixture

from kahin.the_twins import mirage as mirage_mod
from kahin.tools import pilot, pilot_mirage, trainman_mirage


def _real_available() -> bool:
    """True when the sidecar binary and a real Camoufox are both present."""
    try:
        mirage_mod._sidecar_bin()
        mirage_mod._camoufox_bin()
        return True
    except RuntimeError:
        return False


pytestmark = pytest.mark.skipif(
    not _real_available(), reason="sidecar binary or Camoufox missing"
)


def _doc(body: str) -> str:
    """A data: URL carrying an HTML document (no network involved)."""
    return "data:text/html," + quote(body)


def _loads(text: str) -> Any:
    return json.loads(text)


@async_fixture
async def mirage_tools() -> AsyncGenerator[None, None]:
    """Start real Camoufox through kahin_browser_start + one tab, then stop."""
    resp = _loads(await pilot.browser_start(engine="mirage"))
    assert resp["status"] == "started", resp
    try:
        tab = _loads(await trainman_mirage.mirage_tab_new())
        assert tab.get("targetId"), tab
        await asyncio.sleep(0.5)  # session/frame events settle
        yield
    finally:
        await pilot.browser_stop()


async def _navigate(url: str) -> None:
    resp = _loads(await pilot.navigate(url=url))
    assert isinstance(resp, dict) and resp.get("frameId"), resp


@pytest.mark.asyncio
async def test_wait_selector_text_engine_and_state(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><button style='display:none' id='b1'>Hidden</button>"
        "<button id='b2'>Visible</button></body></html>"
    ))
    hidden = _loads(await pilot_mirage.mirage_wait_selector(
        "text=Hidden", timeout=1.0, state="visible",
    ))
    assert hidden.get("found") is False
    visible = _loads(await pilot_mirage.mirage_wait_selector(
        "text=Visible", timeout=3.0, state="visible",
    ))
    assert visible.get("found") is True


@pytest.mark.asyncio
async def test_wait_selector_waits_for_appearance(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><div id='late'></div>"
        "<script>setTimeout(() => { const b = document.createElement('button'); "
        "b.id = 'later'; b.textContent = 'Later'; document.body.appendChild(b); }, 600);</script>"
        "</body></html>"
    ))
    started = time.monotonic()
    result = _loads(await pilot_mirage.mirage_wait_selector("#later", timeout=5.0))
    elapsed = time.monotonic() - started
    assert result.get("found") is True
    assert elapsed >= 0.4, f"did not actually wait: {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_click_waits_for_animation_stability(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><head><style>"
        "#btn { position: absolute; left: 0; top: 0; width: 120px; height: 40px; "
        "transition: left 0.8s ease; }"
        "</style></head><body>"
        "<button id='btn' style='left: 0'>Move</button>"
        "<div id='log'></div>"
        "<script>"
        "const btn = document.getElementById('btn');"
        "setTimeout(() => { btn.style.left = '200px'; }, 50);"
        "btn.addEventListener('click', () => { document.getElementById('log').textContent = 'clicked'; });"
        "</script></body></html>"
    ))
    result = _loads(await pilot_mirage.mirage_click("#btn", timeout=5.0))
    assert not result.get("error"), result
    assert result.get("clicked") == "#btn"
    text = _loads(await pilot_mirage.mirage_get_text("#log"))
    assert "clicked" in text


@pytest.mark.asyncio
async def test_click_waits_for_enabled(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><button id='b' disabled>Go</button>"
        "<script>setTimeout(() => { document.getElementById('b').disabled = false; }, 500);"
        "</script></body></html>"
    ))
    result = _loads(await pilot_mirage.mirage_click("text=Go", timeout=5.0))
    assert not result.get("error"), result
