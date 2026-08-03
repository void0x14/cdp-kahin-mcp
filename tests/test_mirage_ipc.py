"""Mirage engine tests: fake-sidecar unit tests + real Camoufox integration."""

import asyncio
from pathlib import Path

import pytest

from kahin.the_twins import mirage as mirage_mod
from kahin.the_twins.chassis import EventData
from kahin.the_twins.mirage import Mirage

FAKE_SIDECAR = """\
#!/usr/bin/env python3
import json, sys, time
for line in sys.stdin:
    req = json.loads(line)
    rid = req["id"]
    params = req.get("params") or {}
    if params.get("slow"):
        time.sleep(5)
        continue
    if params.get("emit_event"):
        print(json.dumps({"method": "Console.messageAdded",
                          "params": {"message": {"type": "log", "args": ["hi"], "url": "", "line": 1, "column": 1}},
                          "sessionId": "sess-1"}), flush=True)
    print(json.dumps({"id": rid, "result": {"echo": req["method"]}}), flush=True)
"""


@pytest.fixture
def fake_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "fake_sidecar.py"
    script.write_text(FAKE_SIDECAR)
    script.chmod(0o755)
    monkeypatch.setattr(mirage_mod, "_sidecar_bin", lambda: script)
    monkeypatch.setattr(mirage_mod, "_camoufox_bin", lambda: script)
    return script


@pytest.mark.asyncio
async def test_send_cdp_id_matching(fake_sidecar: Path) -> None:
    engine = Mirage()
    await engine.start()
    assert engine._process is not None
    try:
        result = await engine.send_cdp("Browser", "createBrowserContext")
        assert result == {"echo": "Browser.createBrowserContext"}
        result = await engine.send_cdp("Runtime", "evaluate", {"expression": "1+1"})
        assert result["echo"] == "Runtime.evaluate"
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_send_cdp_timeout(fake_sidecar: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mirage_mod, "_REQUEST_TIMEOUT", 0.3)
    engine = Mirage()
    await engine.start()
    try:
        with pytest.raises(RuntimeError, match="timeout"):
            await engine.send_cdp("Runtime", "evaluate", {"slow": True})
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_event_dispatch(fake_sidecar: Path) -> None:
    engine = Mirage()
    received: list[EventData] = []
    await engine.on_event(lambda evt: received.append(evt))
    await engine.start()
    try:
        await engine.send_cdp("Runtime", "evaluate", {"emit_event": True})
        await asyncio.sleep(0.2)
        assert len(received) == 1
        assert received[0].method == "Console.messageAdded"
        assert received[0].session_id == "sess-1"
    finally:
        await engine.stop()


def _real_available() -> bool:
    try:
        mirage_mod._sidecar_bin()
        mirage_mod._camoufox_bin()
        return True
    except RuntimeError:
        return False


pytestmark = pytest.mark.skipif(
    not _real_available(), reason="sidecar binary or Camoufox missing; build core/ipc_main.zig first"
)


@pytest.mark.asyncio
async def test_mirage_full_flow_real_camoufox() -> None:
    """End-to-end over the real Camoufox: page, navigate, evaluate, screenshot,
    and idle event forwarding (navigation events; page-context console events
    are not emitted by this Camoufox build — evaluate-triggered ones arrive
    in-flight and are eaten by the driver pump, see ipc_main.zig docstring)."""
    engine = Mirage()
    ctx = await engine.start()
    assert ctx.engine_name == "mirage"
    assert ctx.ws_url == ""

    events: list[EventData] = []
    await engine.on_event(lambda evt: events.append(evt))

    try:
        ctx_id = (await engine.send_cdp("Browser", "createBrowserContext"))["browserContextId"]
        assert ctx_id

        page = await engine.send_cdp("Browser", "newPage", {"browserContextId": ctx_id})
        target_id = page["targetId"]
        assert target_id
        await asyncio.sleep(0.5)  # idle gap: attachedToTarget/frame/context events flow upward

        nav = await engine.send_cdp("Page", "navigate", {"url": "about:blank"})
        assert nav.get("frameId")

        result = await engine.send_cdp("Runtime", "evaluate", {"expression": "1+1"})
        assert result["result"]["value"] == 2

        shot = await engine.screenshot(format="png")
        assert shot[:8] == b"\x89PNG\r\n\x1a\n"

        await asyncio.sleep(0.3)  # post-response events flow upward while idle
        methods = [e.method for e in events]
        assert any(m.startswith(("Page.", "Runtime.")) for m in methods), methods
    finally:
        await engine.stop()
