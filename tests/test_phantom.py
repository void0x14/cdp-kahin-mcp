"""Phantom (liveness) tests for the Mirage engine.

Covers the Faz 9 Task 3 liveness contract over a fake sidecar:

- ``start()`` boot validation via ``Browser.health`` (dead sidecar -> start fails)
- ``is_alive()``: process alive + reader healthy; False after the child dies
- ``on_death(cb)``: callback fires when the reader hits EOF (pipe closed)
- ``call()``: Juggler-native wire schema {"id","method","params","sessionId"?}
- session management: create_page/close_page/switch_page/list_pages
- dead engine rejects calls (no hang)
"""

import asyncio
from pathlib import Path

import pytest

from kahin import _state as state
from kahin.oracle import _on_engine_death
from kahin.the_twins import mirage as mirage_mod
from kahin.the_twins.mirage import Mirage
from kahin.tools import pilot
from kahin.tools._common import _require_engine

FAKE_SIDECAR = """\
#!/usr/bin/env python3
import json, sys, time
targets = {}
next_id = [0]
def emit(obj):
    print(json.dumps(obj), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    rid = req["id"]
    method = req["method"]
    params = req.get("params") or {}
    if params.get("slow"):
        time.sleep(5)
        continue
    if method == "Browser.health":
        emit({"id": rid, "result": {"alive": True, "pid": 4242, "state": "running"}})
    elif method == "Browser.newPage":
        next_id[0] += 1
        tid = "target-%d" % next_id[0]
        sid = "session-%d" % next_id[0]
        targets[sid] = tid
        emit({"method": "Browser.attachedToTarget",
              "params": {"sessionId": sid, "targetInfo": {"type": "page", "targetId": tid}},
              "sessionId": sid})
        emit({"id": rid, "result": {"targetId": tid}})
    elif method == "Page.getFrameTree":
        sid = req.get("sessionId")
        tid = targets.get(sid, "unknown")
        emit({"id": rid, "result": {"frameTree": {"frame": {"id": "frame-" + tid}}}})
    elif method == "Page.close":
        sid = req.get("sessionId")
        tid = targets.pop(sid, None)
        if tid:
            emit({"method": "Browser.detachedFromTarget",
                  "params": {"sessionId": sid, "targetId": tid},
                  "sessionId": sid})
        emit({"id": rid, "result": {}})
    else:
        emit({"id": rid, "result": {"echo": method,
                                    "sessionId": req.get("sessionId"),
                                    "params": req.get("params")}})
"""

FAKE_DEAD_SIDECAR = FAKE_SIDECAR.replace(
    '"alive": True', '"alive": False'
)

CONCURRENT_FAKE_SIDECAR = """\
#!/usr/bin/env python3
import json, sys, threading, time
emit_lock = threading.Lock()
def emit(obj):
    with emit_lock:
        print(json.dumps(obj), flush=True)
def handle(req):
    rid = req["id"]
    if req["method"] == "Browser.health":
        emit({"id": rid, "result": {"alive": True}})
        return
    if req["method"] == "Page.getFrameTree":
        emit({"id": rid, "result": {"frameTree": {"frame": {"id": "frame-1"}}}})
        return
    time.sleep(0.4)
    emit({"id": rid, "result": {"echo": req["method"]}})
for line in sys.stdin:
    threading.Thread(target=handle, args=(json.loads(line),), daemon=True).start()
"""


@pytest.fixture
def fake_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "fake_sidecar.py"
    script.write_text(FAKE_SIDECAR)
    script.chmod(0o755)
    monkeypatch.setattr(mirage_mod, "_sidecar_bin", lambda: script)
    monkeypatch.setattr(mirage_mod, "_camoufox_bin", lambda: script)
    return script


@pytest.fixture
def dead_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "fake_dead_sidecar.py"
    script.write_text(FAKE_DEAD_SIDECAR)
    script.chmod(0o755)
    monkeypatch.setattr(mirage_mod, "_sidecar_bin", lambda: script)
    monkeypatch.setattr(mirage_mod, "_camoufox_bin", lambda: script)
    return script


@pytest.fixture
def concurrent_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "concurrent_sidecar.py"
    script.write_text(CONCURRENT_FAKE_SIDECAR)
    script.chmod(0o755)
    monkeypatch.setattr(mirage_mod, "_sidecar_bin", lambda: script)
    monkeypatch.setattr(mirage_mod, "_camoufox_bin", lambda: script)
    return script


@pytest.mark.asyncio
async def test_start_boot_validation_ok(fake_sidecar: Path) -> None:
    """start() validates liveness with Browser.health; healthy sidecar starts."""
    engine = Mirage()
    ctx = await engine.start()
    assert ctx.engine_name == "mirage"
    assert engine.is_alive() is True
    await engine.stop()


@pytest.mark.asyncio
async def test_start_rejects_unhealthy_sidecar(dead_sidecar: Path) -> None:
    """Boot validation must fail when Browser.health answers alive=False."""
    engine = Mirage()
    with pytest.raises(RuntimeError, match="health"):
        await engine.start()
    assert engine.is_alive() is False
    assert engine._process is None  # cleaned up after failed boot


@pytest.mark.asyncio
async def test_is_alive_and_on_death(fake_sidecar: Path) -> None:
    """is_alive() flips False on child death; on_death callback fires."""
    engine = Mirage()
    await engine.start()
    assert engine.is_alive() is True

    deaths: list[str] = []
    engine.on_death(lambda: deaths.append("died"))

    proc = engine._process
    assert proc is not None
    proc.terminate()
    await asyncio.sleep(0.4)  # reader hits EOF

    assert engine.is_alive() is False
    assert deaths == ["died"]
    await engine.stop()


@pytest.mark.asyncio
async def test_dead_engine_remains_reachable_for_stop(fake_sidecar: Path) -> None:
    """A dead transport remains in state until explicit cleanup reaps it."""
    engine = Mirage()
    await engine.start()
    previous = state._current_engine
    state._current_engine = engine
    engine.on_death(lambda: _on_engine_death(engine))
    try:
        proc = engine._process
        assert proc is not None
        proc.terminate()
        await asyncio.sleep(0.4)

        assert engine.is_alive() is False
        assert state._current_engine is engine
        error = await _require_engine()
        assert error is not None and "kahin_browser_stop" in error
        assert await pilot.browser_stop() == '{"status": "stopped"}'
        assert state._current_engine is None
    finally:
        state._current_engine = previous
        state.clear_state()
        await engine.stop()


@pytest.mark.asyncio
async def test_call_schema(fake_sidecar: Path) -> None:
    """call() sends the Juggler-native wire and returns the matching result."""
    engine = Mirage()
    await engine.start()
    try:
        # Browser.* -> root session (no sessionId on the wire)
        result = await engine.call("Browser.getInfo")
        assert result == {"echo": "Browser.getInfo", "sessionId": None, "params": {}}

        # explicit sessionId is forwarded for page-scoped methods
        result = await engine.call(
            "Runtime.evaluate", {"expression": "1+1"}, session_id="session-9"
        )
        assert result["echo"] == "Runtime.evaluate"
        assert result["sessionId"] == "session-9"
        assert result["params"] == {"expression": "1+1"}
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_call_timeout(fake_sidecar: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mirage_mod, "_REQUEST_TIMEOUT", 0.3)
    engine = Mirage()
    await engine.start()
    try:
        with pytest.raises(RuntimeError, match="timeout"):
            await engine.call("Runtime.evaluate", {"slow": True})
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_concurrent_timeout_removes_own_pending_request(
    concurrent_sidecar: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out request must not remove a newer concurrent request."""
    monkeypatch.setattr(mirage_mod, "_REQUEST_TIMEOUT", 0.2)
    engine = Mirage()
    await engine.start()
    try:
        slow = asyncio.create_task(engine.call("Runtime.evaluate", {"slow": True}))
        await asyncio.sleep(0.05)
        newer = asyncio.create_task(engine.call("Runtime.evaluate", {"slow": False}))
        results = await asyncio.gather(slow, newer, return_exceptions=True)
        assert all(isinstance(result, RuntimeError) for result in results)
        assert engine._pending == {}
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_call_after_death_raises(fake_sidecar: Path) -> None:
    """Calls against a dead engine must raise immediately, not hang."""
    engine = Mirage()
    await engine.start()
    proc = engine._process
    assert proc is not None
    proc.terminate()
    await asyncio.sleep(0.4)
    assert engine.is_alive() is False
    with pytest.raises(RuntimeError, match="dead"):
        await engine.call("Runtime.evaluate", {"expression": "1"})
    await engine.stop()


@pytest.mark.asyncio
async def test_session_management(fake_sidecar: Path) -> None:
    """create_page/switch_page/close_page/list_pages track Juggler sessions."""
    engine = Mirage()
    await engine.start()
    try:
        page1 = await engine.create_page("about:blank")
        assert page1["targetId"] == "target-1"
        assert page1["sessionId"] == "session-1"

        page2 = await engine.create_page("about:blank")
        assert page2["targetId"] == "target-2"

        pages = await engine.list_pages()
        assert [p["targetId"] for p in pages] == ["target-1", "target-2"]
        assert [p["current"] for p in pages] == [False, True]  # newest is current

        await engine.switch_page("target-1")
        pages = await engine.list_pages()
        assert next(p for p in pages if p["targetId"] == "target-1")["current"] is True

        await engine.close_page("target-2")
        pages = await engine.list_pages()
        assert [p["targetId"] for p in pages] == ["target-1"]

        # closing the current page promotes another target
        await engine.close_page("target-1")
        assert await engine.list_pages() == []
        assert engine._current_target is None
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_ensure_page_is_single_flight(fake_sidecar: Path) -> None:
    """Concurrent first page operations create one tab, not one per call."""
    engine = Mirage()
    await engine.start()
    try:
        pages = await asyncio.gather(*(engine.ensure_page() for _ in range(8)))
        assert {page["targetId"] for page in pages} == {"target-1"}
        assert len(await engine.list_pages()) == 1
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_cdp_equivalents_use_juggler_methods(fake_sidecar: Path) -> None:
    """CDP-shaped Target/Input/Emulation calls route through Mirage."""
    engine = Mirage()
    await engine.start()
    try:
        assert await engine.execute_cdp("Target", "getTargets") == {"targetInfos": []}
        created = await engine.execute_cdp(
            "Target", "createTarget", {"url": "about:blank"}
        )
        assert created == {"targetId": "target-1"}
        assert await engine.execute_cdp("Input", "insertText", {"text": "hello"}) == {}
        assert await engine.execute_cdp(
            "Emulation", "setDeviceMetricsOverride", {"width": 800, "height": 600}
        ) == {}
        targets = await engine.execute_cdp("Target", "getTargets")
        assert targets["targetInfos"][0]["targetId"] == "target-1"
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_switch_page_unknown_target_raises(fake_sidecar: Path) -> None:
    engine = Mirage()
    await engine.start()
    try:
        with pytest.raises(RuntimeError, match="unknown target"):
            await engine.switch_page("target-nope")
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_send_cdp_removed(fake_sidecar: Path) -> None:
    """send_cdp is gone — the wire is call(method, params, session_id)."""
    engine = Mirage()
    await engine.start()
    try:
        with pytest.raises(RuntimeError, match="removed"):
            await engine.send_cdp("Page", "reload")
    finally:
        await engine.stop()
