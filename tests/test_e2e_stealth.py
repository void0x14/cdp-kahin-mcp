"""Stealth e2e over real Camoufox (Faz 3, Tasks 1 + 3).

Task 1: the read-only fingerprint audit runs against a live page and must
report every check with a real score. Task 3: humanized input — Bézier
mouse trajectory, humanized click (real DOM click at the element center)
and cadence typing (real keydown/keyup pairs with jittered delays). Every
scenario drives the same async functions the MCP server exposes, so the
JSON-string tool contract is what gets verified.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import quote

import pytest
from pytest_asyncio import fixture as async_fixture

from kahin.the_twins import mirage as mirage_mod
from kahin.tools import pilot, pilot_mirage, stealth_mirage, trainman_mirage


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


@pytest.mark.asyncio
async def test_stealth_audit_reports_all_checks(mirage_tools: None) -> None:
    await pilot.navigate(url=_doc("<html><body>probe</body></html>"))
    await asyncio.sleep(0.2)
    result = _loads(await stealth_mirage.stealth_audit())
    assert result.get("audited") is True, result
    assert result.get("engine") == "mirage"
    assert isinstance(result.get("checks"), list) and len(result["checks"]) >= 10, result
    assert result["score"]["total"] == len(result["checks"]), result


@pytest.mark.asyncio
async def test_mouse_trajectory_moves(mirage_tools: None) -> None:
    await pilot.navigate(url=_doc("<html><body>trajectory</body></html>"))
    await asyncio.sleep(0.2)
    result = _loads(await stealth_mirage.mirage_mouse_trajectory(320, 240, steps=12, jitter=1.0, seed=7))
    assert not result.get("error"), result
    assert result.get("moved") == 12, result
    assert result.get("to") == [320.0, 240.0], result
    assert isinstance(result.get("from"), list) and len(result["from"]) == 2, result


@pytest.mark.asyncio
async def test_humanized_click_fires_event(mirage_tools: None) -> None:
    await pilot.navigate(url=_doc(
        "<html><body><button id='b' style='position:absolute;left:10px;top:10px;width:100px;height:40px'>H</button>"
        "<div id='log'></div>"
        "<script>document.getElementById('b').addEventListener('click', () => { "
        "document.getElementById('log').textContent = 'clicked'; });</script></body></html>"
    ))
    await asyncio.sleep(0.2)
    result = _loads(await stealth_mirage.mirage_click_humanized(
        "#b", steps=20, jitter=1.5, click_delay_ms=60.0, timeout=5.0,
    ))
    assert not result.get("error"), result
    assert result.get("moved", 0) >= 10, result
    assert isinstance(result.get("to"), list) and len(result["to"]) == 2, result
    text = await pilot_mirage.mirage_get_text("#log")
    assert "clicked" in text, text


@pytest.mark.asyncio
async def test_cadence_typing_enters_text(mirage_tools: None) -> None:
    await pilot.navigate(url=_doc(
        "<html><body><input id='i'><script>"
        "document.getElementById('i').addEventListener('input', () => { "
        "document.getElementById('i').dataset.changed = '1'; });</script></body></html>"
    ))
    await asyncio.sleep(0.2)
    focus = await pilot_mirage.mirage_focus("#i")
    assert "focused" in focus, focus
    result = _loads(await stealth_mirage.mirage_key_text(
        "hello", delay_ms=20.0, jitter_ms=10.0, seed=5,
    ))
    assert not result.get("error"), result
    assert result.get("typed") == "hello", result
    assert len(result.get("cadence", [])) == 5, result
    value = await pilot_mirage.mirage_get_value("#i")
    assert "hello" in value, value
