# Faz 3 — Stealth & Anti-Detect (Playwright'ın Yapamadığı) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Anti-detect iddiasını ÖLÇÜLEBİLİR yapmak — self-audit (leak probe'ları), insansı girdi (trajectory + cadence), identity yaşam döngüsü (üret/sakla/pinle/döndür), proxy/geo tutarlılığı ve CI'da stealth regresyon kapısı. Bunlar Playwright'ın hiçbirinin yapamadığı yüzeydir — Camoufox'un C++-seviyesi fingerprint enjeksiyonunun üstüne inşa edilir.

**Architecture:** Yeni saf modül `kahin/humanize.py` (trajectory/cadence üreteçleri); `kahin/stealth.py` (probe paketi + skorlama); tool'lar `kahin/tools/stealth_mirage.py`; engine'e tek küçük ek: son fare konumu takibi (`_last_mouse`) + proxy env helper. Zig sidecar'a hiçbir dokunuş yok. CI gate'i mevcut `KAHIN_REQUIRE_*` env pattern'ini takip eder.

**Tech Stack:** Python ≥3.12, orjson, httpx (proxy resolve), pytest-asyncio, real Camoufox (e2e).

## Global Constraints

- Faz 1+2 kısıtları aynen geçer
- Stealth probe'ları SALT-OKUNUR: sayfa DOM'una yazmaz, event dinlemez, kalıcı değişiklik yapmaz — audit çalıştırmak sayfayı kirletmez
- `kahin_stealth_audit` skoru her zaman `{score: {passed, total, ratio}, checks: [...]}` raporlar; hiçbir check atlanamaz (bilinmeyen → fail + detail="unsupported")
- Identity dosyaları: `~/.config/kahin/identities/*.json` (Faz 2 kuralı); pins: `~/.config/kahin/pins.json`
- Proxy env helper saf fonksiyondur (test edilebilir); gerçek proxy akışı Camoufox'un kendi env'ine bırakılır
- İnsansı girdi tool'ları `_MAX_WAIT_TIMEOUT` ve `_MAX_COORDINATE` sınırlarına uyar; trajectory steps ≤ 200, jitter ≤ 20px

---

### Task 1: `kahin/stealth.py` + `kahin_stealth_audit` tool

**Files:**
- Create: `kahin/stealth.py`
- Create: `kahin/tools/stealth_mirage.py`
- Modify: `kahin/tools/__init__.py`
- Test: `tests/test_stealth.py` (saf) + `tests/test_e2e_stealth.py` (real)

**Interfaces:**
- Produces: `STEALTH_PROBE_JS` (string, tam probe paketi — tek evaluate'de döner), `score_checks(checks: list[dict]) -> dict`, `kahin_stealth_audit(frame_id=None) -> str` (JSON: `{audited: true, score, checks, engine}`)
- Probe paketi kontrolleri: webdriver, cdp-markers (cdc_/__selenium/__playwright), kahin-binding-gizlilik (DOM stream binding sayfada görünür mü), plugins, languages, platform/oscpu varlığı, timezone-sane, screen-sane, prototype-integrity (native toString), permissions-api, webgl-varlık (canvas konsistansı), audio-varlık (AudioContext), hardwareConcurrency>0

- [ ] **Step 1: Write the failing unit test** — `tests/test_stealth.py`

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_stealth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kahin.stealth'`

- [ ] **Step 3: Write the implementation** — `kahin/stealth.py`

```python
"""stealth.py — Self-audit probe package for the anti-detect surface.

Read-only page probes that surface automation leaks and fingerprint
inconsistencies. Every check is plain page JavaScript; nothing writes to
the DOM, listens to events, or persists state — running the audit never
contaminates the audited page.

The probe list mirrors the vectors CreepJS/botd-style detectors use:
webdriver flag, CDP/automation markers, injected binding visibility,
plugin/language sanity, native-prototype integrity, platform/timezone/
screen consistency, and WebGL/audio presence.
"""

from __future__ import annotations

from typing import Any

STEALTH_PROBE_JS = r"""
(() => {
  const checks = [];
  const r = (check, passed, detail) => checks.push({check, passed: !!passed, detail: String(detail == null ? "" : detail).slice(0, 200)});
  r("webdriver", navigator.webdriver === undefined || navigator.webdriver === false, navigator.webdriver);
  const markers = ["cdc_", "__selenium", "__webdriver_evaluate", "__playwright", "__pw_", "__lastWatcher"];
  const found = markers.filter((m) => Object.getOwnPropertyNames(window).some((p) => p.includes(m)));
  r("cdp-markers", found.length === 0, found.join(","));
  r("binding-hidden", typeof window.__kahin_dom_stream_v1 === "undefined" || window.__kahin_dom_stream_v1 === null, typeof window.__kahin_dom_stream_v1);
  r("plugins", (navigator.plugins && navigator.plugins.length) > 0, navigator.plugins && navigator.plugins.length);
  r("languages", Array.isArray(navigator.languages) && navigator.languages.length > 0, navigator.languages && navigator.languages.join(","));
  r("platform", typeof navigator.platform === "string" && navigator.platform.length > 0, navigator.platform);
  r("oscpu", typeof navigator.oscpu === "string" && navigator.oscpu.length > 0, navigator.oscpu);
  r("timezone-sane", (() => { try { const tz = Intl.DateTimeFormat().resolvedOptions().timeZone; return typeof tz === "string" && tz.length > 0 && tz.includes("/"); } catch (e) { return false; } })(), (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { return ""; } })());
  r("screen-sane", screen && screen.width > 0 && screen.height > 0 && window.innerWidth > 0 && window.innerHeight > 0, screen.width + "x" + screen.height);
  r("prototype-integrity", (() => { try { return Element.prototype.getBoundingClientRect.toString().includes("[native code]"); } catch (e) { return false; } })(), "native toString");
  r("permissions-api", typeof navigator.permissions !== "undefined", typeof navigator.permissions);
  r("webgl", (() => { try { const c = document.createElement("canvas"); const gl = c.getContext("webgl") || c.getContext("experimental-webgl"); return !!gl; } catch (e) { return false; } })(), "webgl context");
  r("audio", typeof (window.AudioContext || window.webkitAudioContext) !== "undefined", "AudioContext present");
  r("hardware-concurrency", typeof navigator.hardwareConcurrency === "number" && navigator.hardwareConcurrency > 0, navigator.hardwareConcurrency);
  return {score: score_checks_placeholder(), checks};
})()
"""

# NOTE: score_checks_placeholder() is a marker the tool layer replaces with
# the real scoring call — see score_checks() below and the tool wiring.


def score_checks(checks: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(checks)
    passed = sum(1 for check in checks if bool(check.get("passed")))
    return {
        "passed": passed,
        "total": total,
        "ratio": round(passed / total, 4) if total else 0.0,
    }
```

The tool layer replaces the placeholder before evaluating:

```python
_SCORE_INLINE = (
    "({passed: checks.filter(c => c.passed).length, "
    "total: checks.length, ratio: checks.length ? checks.filter(c => c.passed).length / checks.length : 0})"
)
```

- [ ] **Step 4: Implement the tool** — `kahin/tools/stealth_mirage.py`

```python
"""stealth_mirage.py — Stealth & anti-detect tools (Faz 3)."""

from __future__ import annotations

from typing import Any

import orjson

from kahin._mcp import mcp
from kahin.stealth import STEALTH_PROBE_JS, score_checks
from kahin.tools._common import _RO, _healer_ref
from kahin.tools.pilot_mirage import (
    _capture_page_session,
    _json_error,
    _safe_mirage_eval_result,
)

_MIN_AUDIT_RATIO = 0.0  # audit itself never fails; the CI gate sets the bar


def _probe_with_scoring() -> str:
    return STEALTH_PROBE_JS.replace(
        "score_checks_placeholder()",
        "({passed: checks.filter(c => c.passed).length, total: checks.length, "
        "ratio: checks.length ? checks.filter(c => c.passed).length / checks.length : 0})",
    )


@mcp.tool(name="kahin_stealth_audit", annotations=_RO)
async def stealth_audit(frame_id: str | None = None) -> str:
    """Mirage: run the read-only stealth probe package on the current page.
    Returns {audited, engine, score: {passed, total, ratio}, checks: [{check,
    passed, detail}]}. A low ratio pinpoints leak vectors to fix."""
    async with _healer_ref.safe("kahin_stealth_audit", frame_id=frame_id):
        session_id, capture_error = await _capture_page_session("kahin_stealth_audit")
        if capture_error:
            return capture_error
        assert session_id is not None
        result = await _safe_mirage_eval_result(
            "kahin_stealth_audit", _probe_with_scoring(), frame_id, session_id=session_id,
        )
        if isinstance(result, str):
            return result
        value = result.get("result") or {}
        parsed = value.get("value") if isinstance(value, dict) else None
        if not isinstance(parsed, dict):
            return _json_error("kahin_stealth_audit", "probe returned no value", "invalid_engine_response")
        checks = parsed.get("checks") or []
        score = parsed.get("score")
        if not isinstance(score, dict):
            score = score_checks(checks)
        return orjson.dumps({
            "audited": True,
            "engine": "mirage",
            "score": score,
            "checks": checks,
        }, option=orjson.OPT_INDENT_2).decode()
```

Register in `kahin/tools/__init__.py`:

```python
from kahin.tools import stealth_mirage  # noqa: F401
```

- [ ] **Step 5: Write the e2e test** — `tests/test_e2e_stealth.py`

```python
"""Stealth e2e — real Camoufox audit + rotation checks."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import quote

import pytest
from pytest_asyncio import fixture as async_fixture

from kahin.the_twins import mirage as mirage_mod
from kahin.tools import pilot, pilot_mirage, stealth_mirage, trainman_mirage

_KAHIN_REQUIRE_STEALTH = os.environ.get("KAHIN_REQUIRE_STEALTH") == "1"


def _real_available() -> bool:
    try:
        mirage_mod._sidecar_bin()
        mirage_mod._camoufox_bin()
        return True
    except RuntimeError:
        return False


pytestmark = pytest.mark.skipif(
    not _real_available() and not _KAHIN_REQUIRE_STEALTH,
    reason="sidecar/Camoufox missing or KAHIN_REQUIRE_STEALTH not set",
)


def _doc(body: str) -> str:
    return "data:text/html," + quote(body)


def _loads(text: str) -> Any:
    return json.loads(text)


@async_fixture
async def mirage_tools() -> AsyncGenerator[None, None]:
    resp = _loads(await pilot.browser_start(engine="mirage"))
    assert resp["status"] == "started", resp
    try:
        tab = _loads(await trainman_mirage.mirage_tab_new())
        assert tab.get("targetId"), tab
        await asyncio.sleep(0.5)
        yield
    finally:
        await pilot.browser_stop()


@pytest.mark.asyncio
async def test_stealth_audit_runs_and_scores(mirage_tools: None) -> None:
    await pilot.navigate(url=_doc("<html><body>audit me</body></html>"))
    result = _loads(await stealth_mirage.stealth_audit())
    assert result.get("audited") is True, result
    score = result.get("score", {})
    assert score.get("total", 0) >= 10, result
    checks = result.get("checks", [])
    names = {c["check"] for c in checks}
    for expected in ("webdriver", "cdp-markers", "binding-hidden", "timezone-sane", "prototype-integrity"):
        assert expected in names, names
    if _KAHIN_REQUIRE_STEALTH:
        assert score.get("ratio", 0.0) >= 0.8, result
```

- [ ] **Step 6: Run tests**

```bash
uv run pytest tests/test_stealth.py -v
KAHIN_REQUIRE_STEALTH=1 uv run pytest tests/test_e2e_stealth.py -v
```

Expected: unit PASS; e2e PASS with the audit scoring ≥0.8. (If a specific check leaks on the Camoufox build in use — e.g. `binding-hidden` when the DOM stream is installed — the ratio gate still passes at ≥0.8; log the failing check to `logs/kahin.log` via the healer and keep it in the report. The gate bar can be raised in Task 6 only after the leak is fixed, never by lowering the threshold silently.)

- [ ] **Step 7: Commit**

```bash
uv run ruff check kahin/stealth.py kahin/tools/stealth_mirage.py kahin/tools/__init__.py tests/test_stealth.py tests/test_e2e_stealth.py
git add kahin/stealth.py kahin/tools/stealth_mirage.py kahin/tools/__init__.py tests/test_stealth.py tests/test_e2e_stealth.py
git commit -m "feat(stealth): read-only self-audit probe + kahin_stealth_audit"
```

---

### Task 2: `kahin/humanize.py` — trajectory + cadence generators

**Files:**
- Create: `kahin/humanize.py`
- Test: `tests/test_humanize.py`

**Interfaces:**
- Produces: `bezier_trajectory(x0, y0, x1, y1, *, steps=24, jitter=2.0, seed=None) -> list[tuple[float, float]]`, `jittered_delay(base_ms: float, jitter_ms: float, seed=None) -> float`, `typing_cadence(length: int, base_ms=45.0, jitter_ms=25.0, seed=None) -> list[float]` (per-char delays, ms)
- Guarantees: first point == (x0,y0), last == (x1,y1); len == steps; monotonic parameter t; all points within bounds + jitter

- [ ] **Step 1: Write the failing test** — `tests/test_humanize.py`

```python
"""Humanized input generator tests (pure Python)."""

from __future__ import annotations

import math

import pytest

from kahin.humanize import bezier_trajectory, jittered_delay, typing_cadence


def test_trajectory_endpoints_and_length() -> None:
    points = bezier_trajectory(10, 20, 310, 220, steps=24, jitter=0.0)
    assert len(points) == 24
    assert points[0] == (10.0, 20.0)
    assert points[-1] == (310.0, 220.0)


def test_trajectory_progresses_toward_target() -> None:
    points = bezier_trajectory(0, 0, 1000, 0, steps=10, jitter=0.0)
    xs = [p[0] for p in points]
    assert xs == sorted(xs)
    assert xs[-1] == 1000.0


def test_trajectory_jitter_stays_bounded() -> None:
    points = bezier_trajectory(0, 0, 100, 100, steps=40, jitter=2.0, seed=7)
    for x, y in points:
        assert -2.0 <= x <= 102.0
        assert -2.0 <= y <= 102.0


def test_trajectory_deterministic_with_seed() -> None:
    a = bezier_trajectory(5, 5, 200, 90, steps=20, jitter=3.0, seed=42)
    b = bezier_trajectory(5, 5, 200, 90, steps=20, jitter=3.0, seed=42)
    assert a == b


def test_jittered_delay_range_and_determinism() -> None:
    for _ in range(200):
        delay = jittered_delay(50.0, 10.0, seed=1)
        assert 40.0 <= delay <= 60.0
    assert jittered_delay(50.0, 10.0, seed=9) == jittered_delay(50.0, 10.0, seed=9)


def test_typing_cadence_length_and_range() -> None:
    delays = typing_cadence(5, base_ms=45.0, jitter_ms=25.0, seed=3)
    assert len(delays) == 5
    for delay in delays:
        assert 20.0 <= delay <= 70.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_humanize.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kahin.humanize'`

- [ ] **Step 3: Write the implementation** — `kahin/humanize.py`

```python
"""humanize.py — Human-like input generation (deterministic with seed).

A quadratic Bézier with a random control point plus bounded Gaussian
jitter produces mouse paths that look human: curved, accelerating at the
ends, never perfectly straight, never a single teleport. Typing cadence
returns per-character delays with realistic jitter. Everything is seed-
able so tests and replays are reproducible.
"""

from __future__ import annotations

import math
import random

_DEFAULT_STEPS = 24
_DEFAULT_JITTER = 2.0
_JITTER_MAX = 20.0


def bezier_trajectory(
    x0: float, y0: float, x1: float, y1: float, *,
    steps: int = _DEFAULT_STEPS, jitter: float = _DEFAULT_JITTER, seed: int | None = None,
) -> list[tuple[float, float]]:
    """Points from (x0,y0) to (x1,y1) along a jittered quadratic Bézier."""
    steps = max(2, min(int(steps), 200))
    jitter = max(0.0, min(float(jitter), _JITTER_MAX))
    rng = random.Random(seed)
    # control point: biased toward the midpoint with perpendicular offset
    mid_x, mid_y = (x0 + x1) / 2, (y0 + y1) / 2
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy) or 1.0
    perp_x, perp_y = -dy / length, dx / length
    bend = rng.uniform(-0.35, 0.35) * length
    control = (mid_x + perp_x * bend, mid_y + perp_y * bend)
    points: list[tuple[float, float]] = []
    for step in range(steps):
        t = step / (steps - 1)
        inv = 1.0 - t
        x = inv * inv * x0 + 2 * inv * t * control[0] + t * t * x1
        y = inv * inv * y0 + 2 * inv * t * control[1] + t * t * y1
        if 0 < t < 1 and jitter > 0:
            x += rng.gauss(0.0, jitter)
            y += rng.gauss(0.0, jitter)
        points.append((round(x, 2), round(y, 2)))
    points[0] = (float(x0), float(y0))
    points[-1] = (float(x1), float(y1))
    return points


def jittered_delay(base_ms: float, jitter_ms: float, seed: int | None = None) -> float:
    """base ± jitter, clamped to [0.25*base, 4*base]."""
    rng = random.Random(seed)
    delay = base_ms + rng.uniform(-jitter_ms, jitter_ms)
    return round(max(base_ms * 0.25, min(delay, base_ms * 4.0)), 1)


def typing_cadence(
    length: int, base_ms: float = 45.0, jitter_ms: float = 25.0, seed: int | None = None,
) -> list[float]:
    """Per-character delays (ms) for typing ``length`` characters."""
    rng = random.Random(seed)
    delays: list[float] = []
    for index in range(max(0, int(length))):
        # slight slowdown after every 4-7 chars mimics hand pauses
        burst = rng.randint(4, 7)
        extra = rng.uniform(0.0, 90.0) if index > 0 and index % burst == 0 else 0.0
        delays.append(round(max(5.0, base_ms + rng.uniform(-jitter_ms, jitter_ms) + extra), 1))
    return delays
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_humanize.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
uv run ruff check kahin/humanize.py tests/test_humanize.py
git add kahin/humanize.py tests/test_humanize.py
git commit -m "feat(stealth): humanized trajectory and typing cadence generators"
```

---

### Task 3: Humanized input tools — trajectory mouse + humanized click + typing cadence

**Files:**
- Modify: `kahin/the_twins/mirage.py` (`_last_mouse` tracking in `_dispatch_mouse` equivalent / reader; expose `get_mouse_position()`)
- Modify: `kahin/tools/stealth_mirage.py` (tools)
- Modify: `kahin/tools/pilot_mirage.py` (`mirage_key_text` cadence params)
- Test: `tests/test_e2e_stealth.py` (extend)

**Interfaces:**
- Produces: `kahin_mirage_mouse_trajectory(x, y, steps=24, jitter=2.0, seed=None)` → plays mousemove sequence from the last mouse position; `{"moved": steps, "from": [x0,y0], "to": [x,y]}`; `kahin_mirage_click_humanized(selector, steps=24, jitter=2.0, click_delay_ms=80.0, click_jitter_ms=20.0, timeout=10.0, frame_id=None)` → actionability wait → trajectory → mousedown → delay → mouseup; `kahin_mirage_key_text(text, delay_ms=45.0, jitter_ms=25.0, seed=None, frame_id=None)` — NEW tool (cadence-typed); existing `mirage_key_text` keeps its behavior (fast path)
- Engine: `Mirage._last_mouse: tuple[float, float] = (0.0, 0.0)`; updated on every `Page.dispatchMouseEvent` with x/y (add update in the `_dispatch_mouse` helper in pilot_mirage, engine-agnostic: simplest place is the tool-layer `_dispatch_mouse` — update a module-global in `kahin/tools/_common.py` or pilot_mirage; plan uses pilot_mirage module global `_last_mouse_position`)

- [ ] **Step 1: Write the failing test** — extend `tests/test_e2e_stealth.py`

```python
@pytest.mark.asyncio
async def test_humanized_click_fires_event(mirage_tools: None) -> None:
    await pilot.navigate(url=_doc(
        "<html><body><button id='b' style='position:absolute;left:10px;top:10px;width:100px;height:40px'>H</button>"
        "<div id='log'></div>"
        "<script>document.getElementById('b').addEventListener('click', () => { "
        "document.getElementById('log').textContent = 'clicked'; });</script></body></html>"
    ))
    result = _loads(await stealth_mirage.mirage_click_humanized(
        "#b", steps=20, jitter=1.5, click_delay_ms=60.0, timeout=5.0,
    ))
    assert not result.get("error"), result
    assert result.get("moved", 0) >= 10, result
    text = _loads(await pilot_mirage.mirage_get_text("#log"))
    assert "clicked" in text.get("text", ""), text


@pytest.mark.asyncio
async def test_cadence_typing_enters_text(mirage_tools: None) -> None:
    await pilot.navigate(url=_doc(
        "<html><body><input id='i'><script>"
        "document.getElementById('i').addEventListener('input', () => { "
        "document.getElementById('i').dataset.changed = '1'; });</script></body></html>"
    ))
    result = _loads(await stealth_mirage.mirage_key_text("hello", delay_ms=20.0, jitter_ms=10.0, seed=5))
    assert not result.get("error"), result
    assert result.get("typed") == "hello"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_e2e_stealth.py::test_humanized_click_fires_event tests/test_e2e_stealth.py::test_cadence_typing_enters_text -v`
Expected: FAIL — attribute missing

- [ ] **Step 3: Track last mouse position** — `pilot_mirage.py`

Add a module global and update `_dispatch_mouse` (pilot_mirage.py:282-299):

```python
_last_mouse_position: tuple[float, float] = (0.0, 0.0)


def get_last_mouse_position() -> tuple[float, float]:
    return _last_mouse_position
```

In `_dispatch_mouse`, after clamping x/y (line ~287-288), add:

```python
global _last_mouse_position
if kind == "mousemove" or kind in ("mousedown", "mouseup"):
    _last_mouse_position = (x, y)
```

- [ ] **Step 4: Implement the tools** — `stealth_mirage.py` append

```python
import math as _math

from kahin.humanize import bezier_trajectory, jittered_delay, typing_cadence
from kahin.tools.pilot_mirage import (
    _MAX_COORDINATE,
    _bounded_float,
    _dispatch_mouse,
    _is_error_response,
    _strict_float,
    _strict_int,
    get_last_mouse_position,
)


@mcp.tool(name="kahin_mirage_mouse_trajectory", annotations=_DW)
async def mirage_mouse_trajectory(
    x: float, y: float, steps: int = 24, jitter: float = 2.0, seed: int | None = None,
) -> str:
    """Mirage: move the mouse from its last position to (x, y) along a
    jittered Bézier path (mousemove per step). Humanizes cursor travel."""
    x_value, error = _strict_float(x, tool="kahin_mirage_mouse_trajectory", field="x", minimum=0.0, maximum=_MAX_COORDINATE)
    if error:
        return error
    y_value, error = _strict_float(y, tool="kahin_mirage_mouse_trajectory", field="y", minimum=0.0, maximum=_MAX_COORDINATE)
    if error:
        return error
    steps_value = _bounded_int(steps, minimum=2, maximum=200, default=24)
    jitter_value = _bounded_float(jitter, minimum=0.0, maximum=20.0, default=2.0)
    async with _healer_ref.safe("kahin_mirage_mouse_trajectory", x=x_value, y=y_value, steps=steps_value, jitter=jitter_value, seed=seed):
        session_id, capture_error = await _capture_page_session("kahin_mirage_mouse_trajectory")
        if capture_error:
            return capture_error
        assert session_id is not None
        start_x, start_y = get_last_mouse_position()
        points = bezier_trajectory(
            start_x, start_y, x_value, y_value, steps=steps_value, jitter=jitter_value, seed=seed,
        )
        for px, py in points:
            move = await _dispatch_mouse("mousemove", px, py, button=0, buttons=0, session_id=session_id)
            if _is_error_response(move):
                return move
        return orjson.dumps({
            "moved": len(points), "from": [start_x, start_y], "to": [x_value, y_value],
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_click_humanized", annotations=_DW)
async def mirage_click_humanized(
    selector: str,
    steps: int = 24,
    jitter: float = 2.0,
    click_delay_ms: float = 80.0,
    click_jitter_ms: float = 20.0,
    seed: int | None = None,
    timeout: float = 10.0,
    frame_id: str | None = None,
) -> str:
    """Mirage: humanized click — actionability wait, Bézier mouse travel,
    then mousedown, a jittered press delay, mouseup."""
    from kahin.actionability import wait_for_ready
    from kahin.tools.pilot_mirage import _safe_mirage_eval_result as _eval_result  # reuse below

    selector_value, error = _text_arg(selector, tool="kahin_mirage_click_humanized", field="selector", maximum=_MAX_SELECTOR_LENGTH)
    if error:
        return error
    steps_value = _bounded_int(steps, minimum=2, maximum=200, default=24)
    jitter_value = _bounded_float(jitter, minimum=0.0, maximum=20.0, default=2.0)
    delay_value = _bounded_float(click_delay_ms, minimum=10.0, maximum=2_000.0, default=80.0)
    delay_jitter = _bounded_float(click_jitter_ms, minimum=0.0, maximum=500.0, default=20.0)
    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=10.0)
    async with _healer_ref.safe(
        "kahin_mirage_click_humanized", selector=selector_value[:80], steps=steps_value,
        jitter=jitter_value, click_delay_ms=delay_value, timeout=timeout_value, frame_id=frame_id,
    ):
        session_id, capture_error = await _capture_page_session("kahin_mirage_click_humanized")
        if capture_error:
            return capture_error
        assert session_id is not None

        async def probe(expression: str) -> dict[str, Any]:
            result = await _eval_result("kahin_mirage_click_humanized", expression, frame_id, session_id=session_id)
            if isinstance(result, str):
                return {"error": "probe_failed"}
            value = result.get("result") or {}
            parsed = value.get("value") if isinstance(value, dict) else None
            return parsed if isinstance(parsed, dict) else {"error": "probe_failed"}

        ready = await wait_for_ready(probe, selector_value, timeout=timeout_value)
        if not ready.get("ok"):
            return _json_error("kahin_mirage_click_humanized", f"element is not actionable: {ready.get('reason') or ready.get('code')}", str(ready.get("code") or "timeout"), selector=selector_value)
        start_x, start_y = get_last_mouse_position()
        target_x, target_y = float(ready["x"]), float(ready["y"])
        points = bezier_trajectory(
            start_x, start_y, target_x, target_y, steps=steps_value, jitter=jitter_value, seed=seed,
        )
        for index, (px, py) in enumerate(points):
            down = index == len(points) - 1
            kind = "mousemove"
            button = 0
            buttons = 0 if not down else 1
            if down:
                kind = "mousedown"
            move = await _dispatch_mouse(kind, px, py, button=button, buttons=buttons, session_id=session_id)
            if _is_error_response(move):
                return move
        await asyncio.sleep(jittered_delay(delay_value, delay_jitter, seed=seed) / 1000.0)
        up = await _dispatch_mouse("mouseup", target_x, target_y, button=0, buttons=0, session_id=session_id)
        if _is_error_response(up):
            return up
        return orjson.dumps({
            "clicked": selector_value, "moved": len(points),
            "from": [start_x, start_y], "to": [target_x, target_y],
            "press_delay_ms": delay_value,
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_key_text", annotations=_RW)
async def mirage_key_text(
    text: str, delay_ms: float = 45.0, jitter_ms: float = 25.0, seed: int | None = None, frame_id: str | None = None,
) -> str:
    """Mirage: type text with a humanized per-character cadence (existing
    key_text now takes cadence params; delay_ms=0 disables the cadence)."""
    text_value, error = _text_arg(text, tool="kahin_mirage_key_text", field="text", maximum=_MAX_KEY_TEXT_LENGTH)
    if error:
        return error
    base = _bounded_float(delay_ms, minimum=0.0, maximum=500.0, default=45.0)
    jitter = _bounded_float(jitter_ms, minimum=0.0, maximum=200.0, default=25.0)
    async with _healer_ref.safe("kahin_mirage_key_text", text=text_value[:80], delay_ms=base, jitter_ms=jitter, seed=seed, frame_id=frame_id):
        session_id, capture_error = await _capture_page_session("kahin_mirage_key_text")
        if capture_error:
            return capture_error
        assert session_id is not None
        # focus the active element is not needed: raw key events go to the
        # focused element — typing still requires a focused field (use
        # mirage_type/fill_form for selector-driven typing).
        cadence = typing_cadence(len(text_value), base_ms=base or 5.0, jitter_ms=jitter, seed=seed) if base > 0 else [0.0] * len(text_value)
        from kahin.tools.pilot_mirage import _key_defaults  # noqa: PLC0415

        for index, char in enumerate(text_value):
            key, key_code = _key_defaults(char)
            down = await _safe_mirage_call(
                "kahin_mirage_key_text", "Page.dispatchKeyEvent",
                {"type": "keydown", "key": key, "code": "Unidentified", "keyCode": key_code, "text": char, "repeat": False},
                session_id=session_id,
            )
            if _is_error_response(down):
                return down
            char_event = await _safe_mirage_call(
                "kahin_mirage_key_text", "Page.dispatchKeyEvent",
                {"type": "char", "key": char, "code": "Unidentified", "keyCode": 0, "text": char, "repeat": False},
                session_id=session_id,
            )
            if _is_error_response(char_event):
                return char_event
            up = await _safe_mirage_call(
                "kahin_mirage_key_text", "Page.dispatchKeyEvent",
                {"type": "keyup", "key": key, "code": "Unidentified", "keyCode": key_code, "text": "", "repeat": False},
                session_id=session_id,
            )
            if _is_error_response(up):
                return up
            if cadence[index] > 0:
                await asyncio.sleep(cadence[index] / 1000.0)
        return orjson.dumps({"typed": text_value, "cadence": cadence}, option=orjson.OPT_INDENT_2).decode()
```

Note: existing `mirage_key_text` (pilot_mirage.py:986) keeps its fast path — the NEW `kahin_mirage_key_text` in `stealth_mirage.py` REPLACES it in the tool registry (same name, module-level registration order decides; move/rename the old one to `mirage_key_text_fast` in pilot_mirage or delete it — plan: delete the old registration and keep only the stealth version; CHANGELOG notes the cadence params as the new contract).

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_e2e_stealth.py -v
```

Expected: both new tests PASS. (If raw dispatchKeyEvent char typing does not enter text on this Camoufox build, fall back to per-char `Page.insertText` + cadence sleeps — the cadence is the deliverable, the event flavor is an implementation detail; keep the change inside the same function.)

- [ ] **Step 6: Commit**

```bash
uv run ruff check kahin/the_twins/mirage.py kahin/tools/stealth_mirage.py kahin/tools/pilot_mirage.py
git add kahin/the_twins/mirage.py kahin/tools/stealth_mirage.py kahin/tools/pilot_mirage.py tests/test_e2e_stealth.py
git commit -m "feat(stealth): humanized mouse trajectory + cadence typing"
```

---

### Task 4: Identity rotation policy — per-domain pins

**Files:**
- Modify: `kahin/tools/stealth_mirage.py` (pin tools) — or `kahin/tools/agent_mirage.py`; plan: stealth_mirage
- Test: `tests/test_stealth.py` (extend — pin store logic) + `tests/test_e2e_stealth.py` (extend)

**Interfaces:**
- Produces: `kahin_identity_pin(domain, name)` → `{pinned: true, domain, name}`; `kahin_identity_unpin(domain)`; `kahin_identity_pins()` → `{pins: {domain: name}}`; `kahin_identity_for_domain(domain)` → `{domain, name: str|null, hint}`. Store: `~/.config/kahin/pins.json` (`{"version": 1, "pins": {domain: name}}`). Domain validation: regex `^([a-z0-9-]+\.)+[a-z]{2,}$` (veya `localhost`), lowercased; name must be a saved identity (Faz 2 check).

- [ ] **Step 1: Write the failing unit test** — extend `tests/test_stealth.py`

```python
from kahin.stealth import normalize_domain, pins_path, load_pins, save_pins, pin_identity, unpin_identity


def test_normalize_domain() -> None:
    assert normalize_domain("HTTPS://Example.COM/Path") == "example.com"
    assert normalize_domain("localhost") == "localhost"
    assert normalize_domain("bad domain!") is None


def test_pin_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kahin.stealth._PINS_FILE", tmp_path / "pins.json")
    pin_identity("example.com", "id1")
    assert load_pins() == {"example.com": "id1"}
    unpin_identity("example.com")
    assert load_pins() == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_stealth.py::test_normalize_domain tests/test_stealth.py::test_pin_roundtrip -v`
Expected: FAIL — import error

- [ ] **Step 3: Implement pin store** — add to `kahin/stealth.py`

```python
import re
from pathlib import Path

_DOMAIN_RE = re.compile(r"^(localhost|[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+)$")
_PINS_FILE = Path.home() / ".config" / "kahin" / "pins.json"


def normalize_domain(domain: str) -> str | None:
    """Strip scheme/path/port, lowercase; validate hostname shape."""
    text = (domain or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split(":", 1)[0]
    if not _DOMAIN_RE.match(text):
        return None
    return text


def load_pins() -> dict[str, str]:
    try:
        payload = orjson.loads(_PINS_FILE.read_text(encoding="utf-8"))
        pins = payload.get("pins") or {}
        return {str(k): str(v) for k, v in pins.items()} if isinstance(pins, dict) else {}
    except (OSError, orjson.JSONDecodeError):
        return {}


def save_pins(pins: dict[str, str]) -> None:
    _PINS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PINS_FILE.write_text(
        orjson.dumps({"version": 1, "pins": pins}, option=orjson.OPT_INDENT_2).decode(),
        encoding="utf-8",
    )


def pin_identity(domain: str, name: str) -> dict[str, str] | None:
    """Return None on success, or {error, code} on invalid domain."""
    normalized = normalize_domain(domain)
    if normalized is None:
        return {"error": "invalid domain", "code": "invalid_argument"}
    pins = load_pins()
    pins[normalized] = name
    save_pins(pins)
    return None


def unpin_identity(domain: str) -> dict[str, str] | None:
    normalized = normalize_domain(domain)
    if normalized is None:
        return {"error": "invalid domain", "code": "invalid_argument"}
    pins = load_pins()
    pins.pop(normalized, None)
    save_pins(pins)
    return None
```

- [ ] **Step 4: Implement the tools** — append to `stealth_mirage.py`

```python
from kahin.stealth import load_pins, normalize_domain, pin_identity, unpin_identity
from kahin.tools.agent_mirage import _identity_path


@mcp.tool(name="kahin_identity_pin", annotations=_RO)
async def identity_pin(domain: str, name: str) -> str:
    """Pin a saved identity to a domain (rotation policy)."""
    domain_value, error = _text_arg(domain, tool="kahin_identity_pin", field="domain", maximum=253)
    if error:
        return error
    name_value, error = _text_arg(name, tool="kahin_identity_pin", field="name", maximum=64)
    if error:
        return error
    path = _identity_path(name_value)
    if path is None or not path.is_file():
        return _json_error("kahin_identity_pin", f"unknown identity: {name_value!r}", "invalid_argument", field="name")
    async with _healer_ref.safe("kahin_identity_pin", domain=domain_value, name=name_value):
        failed = pin_identity(domain_value, name_value)
        if failed:
            return _json_error("kahin_identity_pin", failed["error"], failed["code"], field="domain")
        return orjson.dumps({"pinned": True, "domain": normalize_domain(domain_value), "name": name_value}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_identity_unpin", annotations=_RO)
async def identity_unpin(domain: str) -> str:
    domain_value, error = _text_arg(domain, tool="kahin_identity_unpin", field="domain", maximum=253)
    if error:
        return error
    async with _healer_ref.safe("kahin_identity_unpin", domain=domain_value):
        failed = unpin_identity(domain_value)
        if failed:
            return _json_error("kahin_identity_unpin", failed["error"], failed["code"], field="domain")
        return orjson.dumps({"unpinned": True, "domain": normalize_domain(domain_value)}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_identity_pins", annotations=_RO)
async def identity_pins() -> str:
    async with _healer_ref.safe("kahin_identity_pins"):
        return orjson.dumps({"pins": load_pins()}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_identity_for_domain", annotations=_RO)
async def identity_for_domain(domain: str) -> str:
    domain_value, error = _text_arg(domain, tool="kahin_identity_for_domain", field="domain", maximum=253)
    if error:
        return error
    async with _healer_ref.safe("kahin_identity_for_domain", domain=domain_value):
        normalized = normalize_domain(domain_value)
        if normalized is None:
            return _json_error("kahin_identity_for_domain", "invalid domain", "invalid_argument", field="domain")
        name = load_pins().get(normalized)
        return orjson.dumps({
            "domain": normalized,
            "name": name,
            "hint": f"start with: kahin_browser_start(identity={name!r})" if name else "no pin — rotate freely",
        }, option=orjson.OPT_INDENT_2).decode()
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_stealth.py -v
```

Expected: unit tests PASS.

- [ ] **Step 6: Commit**

```bash
uv run ruff check kahin/stealth.py kahin/tools/stealth_mirage.py
git add kahin/stealth.py kahin/tools/stealth_mirage.py tests/test_stealth.py
git commit -m "feat(stealth): per-domain identity pinning policy"
```

---

### Task 5: Proxy + geo/locale sync + `kahin_fingerprint_report`

**Files:**
- Modify: `kahin/stealth.py` (`proxy_env`, `resolve_proxy_geo`)
- Modify: `kahin/the_twins/mirage.py` (start: proxy env merge)
- Modify: `kahin/tools/pilot.py` (`browser_start(proxy=...)` + identity summary in result)
- Create tool: `kahin_fingerprint_report` in `stealth_mirage.py`
- Test: `tests/test_stealth.py` (extend) + `tests/test_e2e_stealth.py` (extend)

**Interfaces:**
- Produces: `proxy_env(proxy_url: str) -> dict[str, str]` (HTTPS_PROXY/HTTP_PROXY/ALL_PROXY/NO_PROXY), `resolve_proxy_geo(proxy_url, timeout=5.0) -> dict` (httpx through-proxy GET to `https://ipapi.co/json/`; returns `{ip, timezone, country_code, city, latitude, longitude}` or `{"error", "code": "proxy_resolve_failed"}`), `kahin_proxy_resolve(proxy_url)` tool → geo payload + recommended sync (`browser_start(proxy=..., timezone=..., locale=..., geolocation=...)`), `kahin_fingerprint_report(frame_id=None)` → active identity summary from the page (UA, platform, languages, hardwareConcurrency, screen, timezone, locale, webgl renderer via evaluate), `browser_start(proxy=...)` passes proxy into the child env.

- [ ] **Step 1: Write the failing unit test** — extend `tests/test_stealth.py`

```python
from kahin.stealth import proxy_env


def test_proxy_env_mapping() -> None:
    env = proxy_env("socks5://user:pass@127.0.0.1:1080")
    assert env["HTTPS_PROXY"] == "socks5://user:pass@127.0.0.1:1080"
    assert env["HTTP_PROXY"] == "socks5://user:pass@127.0.0.1:1080"
    assert env["ALL_PROXY"] == "socks5://user:pass@127.0.0.1:1080"
    assert env["NO_PROXY"] == "localhost,127.0.0.1,::1"


def test_proxy_env_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        proxy_env("not a proxy")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_stealth.py::test_proxy_env_mapping tests/test_stealth.py::test_proxy_env_rejects_nonsense -v`
Expected: FAIL — import error

- [ ] **Step 3: Implement** — add to `kahin/stealth.py`

```python
from urllib.parse import urlparse


def proxy_env(proxy_url: str) -> dict[str, str]:
    """Map a proxy URL to the env variables Camoufox respects. Raises
    ValueError for unparseable URLs (scheme http/socks4/socks5)."""
    parsed = urlparse(proxy_url.strip())
    if parsed.scheme not in ("http", "https", "socks4", "socks5") or not parsed.hostname:
        raise ValueError(f"unsupported proxy URL: {proxy_url!r}")
    return {
        "HTTPS_PROXY": proxy_url.strip(),
        "HTTP_PROXY": proxy_url.strip(),
        "ALL_PROXY": proxy_url.strip(),
        "NO_PROXY": "localhost,127.0.0.1,::1",
    }
```

Add `resolve_proxy_geo` (httpx through the proxy):

```python
def resolve_proxy_geo(proxy_url: str, timeout: float = 5.0) -> dict[str, Any]:
    """Resolve the proxy's exit IP geo (ipapi.co) THROUGH the proxy."""
    import httpx

    try:
        with httpx.Client(proxy=proxy_url.strip(), timeout=timeout) as client:
            response = client.get("https://ipapi.co/json/")
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"proxy geo resolution failed: {exc}", "code": "proxy_resolve_failed"}
    return {
        "ip": payload.get("ip", ""),
        "timezone": payload.get("timezone", ""),
        "country_code": payload.get("country_code", ""),
        "country_name": payload.get("country_name", ""),
        "city": payload.get("city", ""),
        "latitude": payload.get("latitude"),
        "longitude": payload.get("longitude"),
    }
```

- [ ] **Step 4: Wire proxy into engine start** — `mirage.py` + `pilot.py`

In `Mirage.start` (mirage.py:345), after the existing `launch_options()` env merge, add:

```python
proxy_url = kwargs.get("proxy")
if isinstance(proxy_url, str) and proxy_url.strip():
    from kahin.stealth import proxy_env

    for key, value in proxy_env(proxy_url).items():
        child_env[key] = value
```

(Find the actual `child_env` variable in the existing start flow and merge into it — the env dict passed to `subprocess.Popen`/spawn.)

In `pilot.browser_start` (pilot.py:139), add `proxy: str | None = None` and forward it via kwargs to the engine start. Also append the identity summary to the started result — after a successful start, when the engine is Mirage, run the fingerprint report inline (Task 5 Step 5 below) and add `payload["identity"] = report["summary"]`.

- [ ] **Step 5: Implement `kahin_fingerprint_report` + `kahin_proxy_resolve`** — append to `stealth_mirage.py`

```python
_FINGERPRINT_REPORT_JS = r"""
(() => {
  const tz = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { return ""; } })();
  const gl = (() => { try { const c = document.createElement("canvas"); const g = c.getContext("webgl") || c.getContext("experimental-webgl"); if (!g) return {}; const e = g.getExtension("WEBGL_debug_renderer_info"); return {vendor: e ? g.getParameter(e.UNMASKED_VENDOR_WEBGL) : "", renderer: e ? g.getParameter(e.UNMASKED_RENDERER_WEBGL) : ""}; } catch (e) { return {}; } })();
  return {
    userAgent: navigator.userAgent,
    platform: navigator.platform,
    oscpu: navigator.oscpu || "",
    languages: navigator.languages || [],
    hardwareConcurrency: navigator.hardwareConcurrency,
    deviceMemory: navigator.deviceMemory,
    timezone: tz,
    locale: (navigator.language || ""),
    screen: {width: screen.width, height: screen.height, colorDepth: screen.colorDepth},
    viewport: {width: innerWidth, height: innerHeight},
    webgl: gl,
  };
})()
"""


@mcp.tool(name="kahin_fingerprint_report", annotations=_RO)
async def fingerprint_report(frame_id: str | None = None) -> str:
    """Mirage: current page-visible fingerprint of the active engine —
    UA, platform, languages, screen, timezone, locale, WebGL. This is what
    a site would see; compare across identities to verify rotation."""
    async with _healer_ref.safe("kahin_fingerprint_report", frame_id=frame_id):
        session_id, capture_error = await _capture_page_session("kahin_fingerprint_report")
        if capture_error:
            return capture_error
        assert session_id is not None
        result = await _safe_mirage_eval_result("kahin_fingerprint_report", _FINGERPRINT_REPORT_JS, frame_id, session_id=session_id)
        if isinstance(result, str):
            return result
        value = result.get("result") or {}
        parsed = value.get("value") if isinstance(value, dict) else None
        if not isinstance(parsed, dict):
            return _json_error("kahin_fingerprint_report", "report evaluate returned no value", "invalid_engine_response")
        return orjson.dumps({"engine": "mirage", "summary": parsed}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_proxy_resolve", annotations=_RO)
async def proxy_resolve(proxy_url: str, timeout: float = 5.0) -> str:
    """Resolve a proxy's exit IP geo THROUGH the proxy and suggest matching
    timezone/locale/geolocation overrides for browser_start."""
    proxy_value, error = _text_arg(proxy_url, tool="kahin_proxy_resolve", field="proxy_url", maximum=1_024)
    if error:
        return error
    timeout_value = _bounded_float(timeout, minimum=1.0, maximum=30.0, default=5.0)
    async with _healer_ref.safe("kahin_proxy_resolve", proxy_url=proxy_value, timeout=timeout_value):
        try:
            env = proxy_env(proxy_value)
        except ValueError as exc:
            return _json_error("kahin_proxy_resolve", str(exc), "invalid_argument", field="proxy_url")
        geo = resolve_proxy_geo(proxy_value, timeout=timeout_value)
        if geo.get("error"):
            return orjson.dumps({"proxy": proxy_value, **geo}, option=orjson.OPT_INDENT_2).decode()
        recommendation = {
            "proxy": proxy_value,
            "geo": geo,
            "recommended": {
                "timezone": geo.get("timezone"),
                "locale": geo.get("country_code"),
                "geolocation": {
                    "latitude": geo.get("latitude"),
                    "longitude": geo.get("longitude"),
                },
            },
            "hint": "pass proxy=... to kahin_browser_start; set timezone/locale/geolocation with the emulation tools",
        }
        return orjson.dumps(recommendation, option=orjson.OPT_INDENT_2).decode()
```

- [ ] **Step 6: Run tests**

```bash
uv run pytest tests/test_stealth.py -v
uv run pytest tests/test_e2e_stealth.py -v
```

Expected: unit PASS; e2e still PASS.

- [ ] **Step 7: Commit**

```bash
uv run ruff check kahin/stealth.py kahin/the_twins/mirage.py kahin/tools/pilot.py kahin/tools/stealth_mirage.py
git add kahin/stealth.py kahin/the_twins/mirage.py kahin/tools/pilot.py kahin/tools/stealth_mirage.py tests/test_stealth.py
git commit -m "feat(stealth): proxy env + geo sync + fingerprint report"
```

---

### Task 6: Stealth CI gate + drift-watcher WAF regression

**Files:**
- Modify: `tests/test_e2e_stealth.py` (rotation + audit gate scenarios)
- Modify: `.gitlab-ci.yml` (stealth job)
- Modify: `kahin/tools/stealth_mirage.py` (no code — policy only)
- Test: `tests/test_e2e_stealth.py` (extend)

**Interfaces:**
- Gate env: `KAHIN_REQUIRE_STEALTH=1` — CI job runs the stealth suite; audit ratio ≥ 0.8 hard-fails; identity rotation test requires 2 distinct fingerprints
- Drift-watcher: new script `scripts/stealth-regression.py` — runs the audit against a fixed probe page and diffs the check results vs a pinned baseline JSON (`camoufox-harness/tests/perf/stealth-baseline.json`); exit 0 = no new leaks, 1 = new leak, 2 = environment issue

- [ ] **Step 1: Write the failing test** — extend `tests/test_e2e_stealth.py`

```python
@pytest.mark.asyncio
async def test_identity_rotation_changes_fingerprint(mirage_tools: None) -> None:
    from kahin.tools import agent_mirage

    first = _loads(await stealth_mirage.fingerprint_report())
    ua1 = first.get("summary", {}).get("userAgent", "")
    screen1 = first.get("summary", {}).get("screen", {})
    assert ua1, first
    await pilot.browser_stop()
    created = _loads(await agent_mirage.kahin_identity_new("rot-a"))
    assert created.get("saved"), created
    started = _loads(await pilot.browser_start(engine="mirage", identity="rot-a"))
    assert started.get("status") == "started", started
    tab = _loads(await trainman_mirage.mirage_tab_new())
    assert tab.get("targetId"), tab
    await asyncio.sleep(0.5)
    second = _loads(await stealth_mirage.fingerprint_report())
    ua2 = second.get("summary", {}).get("userAgent", "")
    screen2 = second.get("summary", {}).get("screen", {})
    if _KAHIN_REQUIRE_STEALTH:
        assert ua1 != ua2 or screen1 != screen2, "rotation did not change the fingerprint"
    _ = _loads(await agent_mirage.kahin_identity_delete("rot-a"))
```

- [ ] **Step 2: Run test to verify it passes (or exposes the leak)**

Run: `KAHIN_REQUIRE_STEALTH=1 uv run pytest tests/test_e2e_stealth.py -v`
Expected: PASS. If the rotation test fails (same UA twice — possible when BrowserForge generates the same device class twice in a row), fix by passing a seed parameter through `kahin_identity_new` (`seed` param) and asserting full-summary difference; do not weaken the assertion to UA-only without checking the full summary first.

- [ ] **Step 3: Write the drift-watcher regression script** — `scripts/stealth-regression.py`

```python
#!/usr/bin/env python3
"""Stealth regression: audit vs pinned baseline. Exit 0 (clean), 1 (new
leak), 2 (environment). Run with a live Kahin Mirage engine available."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASELINE = ROOT / "camoufox-harness" / "tests" / "perf" / "stealth-baseline.json"


def main() -> int:
    if os.environ.get("KAHIN_REQUIRE_STEALTH") != "1":
        print("KAHIN_REQUIRE_STEALTH=1 required"); return 2
    # The audit is executed through the MCP tool layer (the same function
    # the agent sees), so the regression tracks the REAL surface.
    from kahin.tools import pilot, stealth_mirage, trainman_mirage

    async def run() -> dict:
        import json as _json
        import asyncio
        started = _json.loads(await pilot.browser_start(engine="mirage"))
        assert started["status"] == "started", started
        try:
            tab = _json.loads(await trainman_mirage.mirage_tab_new())
            assert tab.get("targetId"), tab
            await asyncio.sleep(0.5)
            await pilot.navigate(url="about:blank")
            await asyncio.sleep(0.5)
            return _json.loads(await stealth_mirage.stealth_audit())
        finally:
            await pilot.browser_stop()

    import asyncio
    result = asyncio.run(run())
    checks = {c["check"]: bool(c["passed"]) for c in result.get("checks", [])}
    if BASELINE.exists():
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    else:
        baseline = {}
    new_failures = {name: passed for name, passed in checks.items()
                    if not passed and baseline.get(name, True) is True}
    failures = {name: passed for name, passed in checks.items() if not passed}
    ratio = result.get("score", {}).get("ratio", 0.0)
    print(json.dumps({"ratio": ratio, "failures": failures, "new_failures": new_failures}, indent=2))
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(json.dumps(checks, indent=2) + "\n", encoding="utf-8")
    if new_failures:
        print(f"NEW STEALTH LEAKS: {sorted(new_failures)}")
        return 1
    if ratio < 0.8:
        print(f"ratio below gate: {ratio}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Add the CI job** — `.gitlab-ci.yml`

Append a job mirroring the existing e2e job pattern (check the file for the Camoufox install steps — reuse them):

```yaml
stealth-regression:
  stage: test
  script:
    - uv sync
    - uv run python -m camoufox fetch || true
    - KAHIN_REQUIRE_STEALTH=1 KAHIN_REQUIRE_REAL_E2E=1 uv run pytest tests/test_e2e_stealth.py -v
    - KAHIN_REQUIRE_STEALTH=1 uv run python scripts/stealth-regression.py
  rules:
    - if: '$CI_PIPELINE_SOURCE == "schedule" || $CI_COMMIT_BRANCH == "main"'
```

- [ ] **Step 5: Run the full verification**

```bash
KAHIN_REQUIRE_STEALTH=1 uv run pytest tests/test_e2e_stealth.py -v
uv run ruff check .
uv run pyright
KAHIN_REQUIRE_STEALTH=1 uv run python scripts/stealth-regression.py; echo "exit=$?"
```

Expected: first regression run creates the baseline and exits 0; subsequent runs stay 0 until a new leak appears.

- [ ] **Step 6: Docs + commit**

Update `AGENTS.md` (stealth tool list) and `CHANGELOG.md` (Faz 3 entry), then:

```bash
git add scripts/stealth-regression.py camoufox-harness/tests/perf/stealth-baseline.json .gitlab-ci.yml tests/test_e2e_stealth.py AGENTS.md CHANGELOG.md
git commit -m "feat(stealth): CI gate + drift-watcher regression for anti-detect surface"
```

---

### Task 7: Faz 3 acceptance run

**Files:** none (verification)

- [ ] **Step 1: Full suite**

```bash
KAHIN_REQUIRE_REAL_E2E=1 uv run pytest tests/ -v
```

Expected: all PASS including Faz 1-3 e2e files.

- [ ] **Step 2: Acceptance checklist**

- `kahin_stealth_audit` runs on a live page: score ratio reported, no exception paths
- `kahin_mirage_click_humanized` completes a real click through a Bézier path
- `kahin_mirage_key_text` enters text with cadence
- `kahin_identity_new` → `browser_start(identity=...)` → `kahin_fingerprint_report` shows the pinned UA; rotation produces a different summary
- `kahin_proxy_resolve` returns geo + recommendation (network permitting; documented as network-dependent)
- CI: stealth job green on main

- [ ] **Step 3: Commit any last docs**

```bash
git add AGENTS.md CHANGELOG.md
git commit -m "docs: Faz 3 stealth surface — audit, humanized input, identity lifecycle"
```
