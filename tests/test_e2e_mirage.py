"""Tool-layer end-to-end tests over real Camoufox (Faz 9 Task 4).

Drives the MCP TOOL LAYER — the async functions registered as the
kahin_* / kahin_mirage_* tools in ``kahin/tools/*.py`` — against a real
Camoufox browser through the Zig sidecar. Unlike test_mirage_ipc.py (which
talks to the raw Mirage engine), every scenario here goes through the same
functions the MCP server exposes, so the tool contract (JSON-string
answers, error strings instead of raises, engine liveness eviction) is
what gets verified.

KNOWN SIDECAR BUG (reported, NOT fixed — zig is read-only in this task):
no-return Juggler commands (Browser.setCookies / clearCookies /
setUserAgentOverride / setDefaultViewport / Browser.clearCache /
Page.close / Page.insertText / Page.dispatchMouseEvent /
Page.handleDialog) DO run in the browser, but the browser replies
``{"id":N}`` without a ``result`` key and the sidecar's ``respondFromRaw``
(camoufox-harness/core/ipc_main.zig) rejects such replies with
-32603 "Juggler response has no result". Playwright itself tolerates
result-less replies. Scenarios that need those commands assert everything
that works first, then ``pytest.skip`` with this reason.

Other verified build facts (skip reasons below):
- data: URL requests emit NO Network.* events (and Network.enable is not
  supported on this build) -> the network-body scenario cannot run
  network-free and is skipped.
- localStorage is blocked on data: URLs ("The operation is insecure.") —
  the storage scenario therefore uses a file:// page (still network-free).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import quote

import pytest
from pytest_asyncio import fixture as async_fixture

from kahin import _state as state
from kahin.the_twins import mirage as mirage_mod
from kahin.tools import dejavu_mirage, dialog_mirage, emulation_mirage, engine, pilot
from kahin.tools import pilot_mirage, storage_mirage, trainman_mirage

_SIDECAR_MSG = (
    "sidecar bug: no-return Juggler replies ({\"id\":N}) are rejected as -32603 "
    "'Juggler response has no result' (ipc_main.zig respondFromRaw); the command "
    "did run in the browser. Zig is read-only in this task — reported, not fixed."
)
_NETWORK_MSG = (
    "this Camoufox build emits no Network.* events for data: URL requests "
    "(and Network.enable is unsupported) — network-body scenario needs a "
    "server-backed URL, which the suite avoids (no network deps)."
)


def _real_available() -> bool:
    """True when the sidecar binary and a real Camoufox are both present."""
    try:
        mirage_mod._sidecar_bin()
        mirage_mod._camoufox_bin()
        return True
    except RuntimeError:
        return False


pytestmark = pytest.mark.skipif(
    not _real_available(), reason="sidecar binary or Camoufox missing; build core/ipc_main.zig first"
)


def _doc(body: str) -> str:
    """A data: URL carrying an HTML document (no network involved)."""
    return "data:text/html," + quote(body)


def _loads(text: str) -> Any:
    return json.loads(text)


def _sidecar_no_result(text: str) -> bool:
    """True when the tool answer carries the sidecar's -32603 reply bug."""
    return "Juggler response has no result" in text


@async_fixture
async def mirage_tools() -> AsyncGenerator[None, None]:
    """Start real Camoufox through kahin_browser_start + one tab, then stop."""
    resp = _loads(await pilot.browser_start(engine="mirage"))
    assert resp["status"] == "started", resp
    try:
        tab = _loads(await trainman_mirage.mirage_tab_new())
        assert tab.get("targetId"), tab
        await asyncio.sleep(0.5)  # session/frame events settle (mirrors test_mirage_ipc)
        yield
    finally:
        await pilot.browser_stop()


async def _navigate(url: str) -> None:
    resp = _loads(await pilot.navigate(url=url))
    assert isinstance(resp, dict) and resp.get("frameId"), resp


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dom_flow_type_click_read(mirage_tools: None) -> None:
    """query -> type -> click -> get_text sees the post-click DOM.

    query/get_text run on the real evaluate surface; type (Page.insertText)
    and click (Page.dispatchMouseEvent) hit the reported sidecar bug.
    """
    html = """<html><body>
      <button id="go">Go</button>
      <input id="inp">
      <div id="out">initial</div>
      <script>
        document.getElementById('go').addEventListener('click', () => {
          const v = document.getElementById('inp').value;
          document.getElementById('out').textContent = v || 'clicked';
        });
      </script>
    </body></html>"""
    await _navigate(_doc(html))
    await asyncio.sleep(0.5)  # document parsed, listeners attached

    info = _loads(await pilot_mirage.mirage_query("#go"))
    assert info["tag"] == "button", info
    assert info["visible"] is True, info

    typed = await pilot_mirage.mirage_type("#inp", "hello")
    if _sidecar_no_result(typed):
        pytest.skip(_SIDECAR_MSG)
    assert _loads(typed)["typed"] == 5, typed

    clicked = await pilot_mirage.mirage_click("#go")
    if _sidecar_no_result(clicked):
        pytest.skip(_SIDECAR_MSG)
    assert _loads(clicked)["clicked"] == "#go", clicked

    text = _loads(await pilot_mirage.mirage_get_text("#out"))
    assert text == "hello", text


@pytest.mark.asyncio
async def test_multi_tab_lifecycle(mirage_tools: None) -> None:
    """tab_new x2 -> tab_list == 2 -> switch -> close -> tab_list == 1.

    new/list/switch are real; close (Page.close) hits the sidecar bug.
    """
    first = _loads(await trainman_mirage.mirage_tab_list())
    assert len(first) == 1, first

    tab2 = _loads(await trainman_mirage.mirage_tab_new(url="about:blank"))
    assert tab2.get("targetId"), tab2

    tabs = _loads(await trainman_mirage.mirage_tab_list())
    assert len(tabs) == 2, tabs

    target1 = first[0]["targetId"]
    switched = _loads(await trainman_mirage.mirage_tab_switch(target1))
    assert switched.get("switched") == target1, switched
    tabs = _loads(await trainman_mirage.mirage_tab_list())
    assert next(t for t in tabs if t["targetId"] == target1)["current"] is True

    closed = await trainman_mirage.mirage_tab_close(tab2["targetId"])
    if _sidecar_no_result(closed):
        pytest.skip(_SIDECAR_MSG)
    assert _loads(closed).get("closed") == tab2["targetId"], closed
    await asyncio.sleep(0.3)  # detachedFromTarget event lands

    tabs = _loads(await trainman_mirage.mirage_tab_list())
    assert len(tabs) == 1, tabs
    assert tabs[0]["targetId"] == target1, tabs


@pytest.mark.asyncio
async def test_cookie_round_trip(mirage_tools: None) -> None:
    """set -> get matches -> clear -> get empty.

    getCookies is real (verified: the buggy set DOES write the cookie into
    the browser); set/clear hit the sidecar bug.
    """
    cookies = _loads(await storage_mirage.mirage_cookie_get())["cookies"]
    assert cookies == [], cookies

    result = await storage_mirage.mirage_cookie_set(cookies=[
        {"name": "kahin_e2e", "value": "cookie-value", "url": "http://example.com/"},
    ])
    if _sidecar_no_result(result):
        pytest.skip(_SIDECAR_MSG)
    assert _loads(result) == {}, result

    cookies = _loads(await storage_mirage.mirage_cookie_get())["cookies"]
    match = [c for c in cookies if c.get("name") == "kahin_e2e"]
    assert len(match) == 1, cookies
    assert match[0]["value"] == "cookie-value", match

    cleared = await storage_mirage.mirage_cookie_clear()
    if _sidecar_no_result(cleared):
        pytest.skip(_SIDECAR_MSG)
    assert _loads(cleared) == {}, cleared

    cookies = _loads(await storage_mirage.mirage_cookie_get())["cookies"]
    assert all(c.get("name") != "kahin_e2e" for c in cookies), cookies


@pytest.mark.asyncio
async def test_network_body_via_fetch(mirage_tools: None) -> None:
    """fetch() inside a data: page -> request listed -> body retrievable.

    Attempted first; this Camoufox build emits no Network.* events for
    data: URLs (probe: forwarded stream contains only Page./Runtime./
    Browser. events), so the scenario is skipped with the evidence.
    """
    html = """<html><body><script>
      fetch('data:text/plain,hello-fetch-body')
        .then(r => r.text())
        .then(t => { document.body.dataset.fetched = t; });
    </script></body></html>"""
    await _navigate(_doc(html))
    await asyncio.sleep(1.0)  # request events would arrive through the idle drain

    reqs = _loads(await dejavu_mirage.mirage_network_requests())
    fetch_reqs = [
        e for e in reqs
        if e["event"] == "requestWillBeSent"
        and "hello-fetch-body" in (e["params"].get("url") or "")
    ]
    if not fetch_reqs:
        pytest.skip(_NETWORK_MSG)
    request_id = fetch_reqs[0]["params"]["requestId"]

    body = _loads(await dejavu_mirage.mirage_get_response_body(request_id))
    assert "hello-fetch-body" in body["body"], body


@pytest.mark.asyncio
async def test_dialog_accept(mirage_tools: None) -> None:
    """alert() -> dialog_list shows it -> accept resolves it (no hang).

    dialog_list (event buffer) is real and verified; accept
    (Page.handleDialog) hits the sidecar bug.
    """
    html = """<html><body><script>
      setTimeout(() => alert('kahin-dialog'), 200);
    </script><div>page</div></body></html>"""
    await _navigate(_doc(html))
    await asyncio.sleep(1.2)  # dialogOpened event arrives while idle

    dialogs = _loads(await dialog_mirage.mirage_dialog_list())
    assert dialogs, dialogs  # Page.dialogOpened IS emitted by this build
    assert dialogs[0]["message"] == "kahin-dialog", dialogs
    dialog_id = dialogs[0]["dialogId"]

    accepted = await dialog_mirage.mirage_dialog_accept(dialog_id)
    if _sidecar_no_result(accepted):
        pytest.skip(_SIDECAR_MSG)
    assert _loads(accepted) == {}, accepted
    await asyncio.sleep(0.5)  # dialogClosed event lands

    assert _loads(await dialog_mirage.mirage_dialog_list()) == []


@pytest.mark.asyncio
async def test_kill_detection_clean_tool_error(mirage_tools: None) -> None:
    """Engine death -> is_alive False -> tools answer clean errors, no hang."""
    eng = state._current_engine
    assert eng is not None and eng.is_alive() is True
    proc = eng._process
    assert proc is not None
    proc.terminate()
    await asyncio.sleep(0.8)  # reader hits EOF -> on_death evicts the engine

    assert eng.is_alive() is False

    health = _loads(await engine.engine_health())
    assert health["engine"] is None, health  # evicted via _on_engine_death
    assert "No browser engine" in health["error"], health

    out = await pilot_mirage.mirage_query("#anything")
    assert "No browser engine" in out, out  # clean error string, no crash


@pytest.mark.asyncio
async def test_local_storage_round_trip(mirage_tools: None, tmp_path: Any) -> None:
    """Page JS writes localStorage -> storage_local_get reads it back.

    localStorage is blocked on data: URLs ("The operation is insecure."),
    so the page is served from a file:// URL — still network-free.
    """
    page = tmp_path / "ls_page.html"
    page.write_text("""<html><body><script>
      localStorage.setItem('kahin_ls', 'ls-value');
    </script></body></html>""")
    await _navigate(page.as_uri())
    await asyncio.sleep(0.5)

    entries = _loads(await storage_mirage.mirage_storage_local_get())
    match = [e for e in entries if e.get("key") == "kahin_ls"]
    assert len(match) == 1, entries
    assert match[0]["value"] == "ls-value", match


@pytest.mark.asyncio
async def test_emulation_ua_round_trip(mirage_tools: None) -> None:
    """set_user_agent -> fresh document -> navigator.userAgent matches.

    Browser.setUserAgentOverride hits the sidecar bug.
    """
    ua = "KahinE2E/9.9"
    result = await emulation_mirage.mirage_set_user_agent(ua)
    if _sidecar_no_result(result):
        pytest.skip(_SIDECAR_MSG)
    assert _loads(result) == {}, result

    await _navigate(_doc("<html><body>ua</body></html>"))
    await asyncio.sleep(0.5)

    got = _loads(await pilot.evaluate(expression="navigator.userAgent"))
    assert got["result"]["value"] == ua, got


@pytest.mark.asyncio
async def test_engine_health_live(mirage_tools: None) -> None:
    """engine_health reports a live Mirage with the sidecar payload."""
    health = _loads(await engine.engine_health())
    assert health["engine"] == "mirage", health
    assert health["alive"] is True, health
    assert health["health"]["alive"] is True, health
