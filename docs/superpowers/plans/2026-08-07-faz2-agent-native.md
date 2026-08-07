# Faz 2 — Agent-Native (Playwright MCP'nin Ötesi) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Kahin'i AI agent'lar için birinci sınıf olay haline getirmek — ref'li (nodeId) snapshot formatı, token disiplini, snapshot-after-action, form doldurma ve oturum/identity kalıcılığı. Playwright MCP'nin yapamadığı: canlı DOM nodeId'leri üzerinden aksiyon (stale selector yok), MutationObserver delta akışı ve fingerprint'li kalıcı oturumlar.

**Architecture:** Yeni saf modül `kahin/agent_snapshot.py` (DOM-stream snapshot'ı → agent formatı + token budget); yeni tool'lar `kahin/tools/agent_mirage.py`; mevcut action tool'larına opsiyonel `return_snapshot`; state/identity kalıcılığı `kahin/tools/agent_mirage.py` + `mirage.py` start yükseltmesi. Juggler protokolüne dokunulmaz.

**Tech Stack:** Python ≥3.12, orjson, asyncio, pytest-asyncio, real Camoufox (e2e), mevcut dom_stream (nodeId + cursor) ve accessibility altyapısı.

## Global Constraints

- Faz 1 kısıtları aynen geçer (JSON-string dönüş, `_text_arg`/`_bounded_float`/`_strict_int` doğrulama, `_healer_ref.safe`, `_capture_page_session` session pinning, `_real_available()` e2e pattern, port rezervasyonu)
- Ref'ler = `kahin_mirage_dom_snapshot` nodeId'leri (`n1`, `n5`...). Ref ömrü: document/frame yaşam döngüsüyle sınırlı — `reset`/`dropped` durumlarında taze snapshot zorunlu (dom_stream kuralı)
- Snapshot çıktısı HER ZAMAN `{tokens_estimate, max_tokens, truncated}` taşır — truncation asla sessiz olmaz
- `return_snapshot` parametresi tüm yeni action tool'larında `bool = False` varsayılan (geriye uyumlu)
- State/identity dosyaları: `~/.config/kahin/identities/*.json` ve kullanıcı belirtilen path; asla güvenli olmayan global dizinlere yazılmaz; `path` argümanı `~` genişletmesi yapar, göreli yol reddedilir (mutlak path zorunlu — upload_files kuralıyla aynı)
- AGENTS.md güncellemesi her task'ta yapılmaz; T6'da toplu

---

### Task 1: `kahin/agent_snapshot.py` — ref'li agent snapshot + token budget

**Files:**
- Create: `kahin/agent_snapshot.py`
- Test: `tests/test_agent_snapshot.py`

**Interfaces:**
- Consumes: dom_stream snapshot dict şekli (dom_stream.py:184-224: `{root, nodeCount, truncated, cursor, streamId, ...}`; node'lar `{nodeId, tag, role, name, text, visible, rect, attributes, actions, value, disabled, checked, selected}`)
- Produces: `format_snapshot(tree, *, max_tokens=1500) -> dict` — çıktı: `{"lines": [...], "tokens_estimate": int, "max_tokens": int, "truncated": bool, "nodeCount": int, "shownCount": int}`
- Line formatı (Playwright MCP tarzı, derinlik girintili): `- button "Add to cart" [ref=n9] [click]`; gizli/boş node'lar atlanır; text > 60 char kırpılır

- [ ] **Step 1: Write the failing test** — `tests/test_agent_snapshot.py`

```python
"""Agent snapshot formatting tests (pure Python)."""

from __future__ import annotations

import pytest

from kahin.agent_snapshot import format_snapshot


def _node(node_id: str, **overrides: object) -> dict:
    base = {
        "nodeId": node_id, "tag": "button", "role": "button", "name": "",
        "text": "", "visible": True, "rect": {"x": 0, "y": 0, "width": 10, "height": 10},
        "attributes": {}, "actions": [], "children": [],
    }
    base.update(overrides)
    return base


def test_format_basic_tree() -> None:
    tree = _node("n1", tag="main", role=None, children=[
        _node("n2", tag="h1", role="heading", name="Kahin", text="Kahin MCP"),
        _node("n3", tag="input", role="textbox", name="Search", actions=["focus", "type"]),
        _node("n4", tag="button", role="button", name="Go", actions=["click"]),
        _node("n5", tag="a", role="link", name="Docs", actions=["click"]),
    ])
    result = format_snapshot(tree, max_tokens=5000)
    lines = result["lines"]
    assert any("heading" in line and "Kahin" in line and "[ref=n2]" in line for line in lines)
    assert any('textbox "Search" [ref=n3] [focus, type]' in line for line in lines)
    assert any('button "Go" [ref=n4] [click]' in line for line in lines)
    assert result["truncated"] is False
    assert result["nodeCount"] == 5 and result["shownCount"] == 4


def test_hidden_nodes_skipped() -> None:
    tree = _node("n1", tag="div", role=None, children=[
        _node("n2", tag="button", role="button", name="Shown", actions=["click"]),
        _node("n3", tag="button", role="button", name="Hidden", visible=False, actions=["click"]),
    ])
    result = format_snapshot(tree)
    assert "Shown" in "\n".join(result["lines"])
    assert "Hidden" not in "\n".join(result["lines"])


def test_token_budget_truncates() -> None:
    children = [_node(f"n{i}", tag="button", role="button", name=f"Item {i}", actions=["click"]) for i in range(50)]
    tree = _node("n1", tag="div", role=None, children=children)
    result = format_snapshot(tree, max_tokens=30)
    assert result["truncated"] is True
    assert result["shownCount"] < 50
    assert result["tokens_estimate"] <= result["max_tokens"]


def test_text_is_capped_and_indented() -> None:
    tree = _node("n1", tag="div", role=None, children=[
        _node("n2", tag="button", role="button", name="X" * 200, actions=["click"]),
    ])
    result = format_snapshot(tree)
    lines = result["lines"]
    assert len(lines[0]) < 120
    assert lines[0].startswith("- ") or lines[0].startswith("  ")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_snapshot.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kahin.agent_snapshot'`

- [ ] **Step 3: Write the implementation** — `kahin/agent_snapshot.py`

```python
"""agent_snapshot.py — DOM-stream snapshot -> AI-agent observation format.

Turns the live DOM snapshot (nodeId-carrying tree from the page) into the
compact line format agents consume, with a hard token budget. The budget
is a character/4 estimate, always reported, and truncation is always
explicit — an agent is never silently shown a partial page.

Line format (Playwright-MCP-like):

    - button "Add to cart" [ref=n9] [click]
    - textbox "Search" [ref=n3] [focus, type]

refs are the LIVE nodeIds of the DOM stream, so every line can be acted on
with kahin_mirage_dom_action without re-resolving a selector.
"""

from __future__ import annotations

from typing import Any

_DEFAULT_MAX_TOKENS = 1500
_MAX_LINE_TEXT = 60
_TOKEN_PER_CHAR = 4.0


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / _TOKEN_PER_CHAR))


def _line_for(node: dict[str, Any], depth: int) -> str:
    indent = "  " * depth
    role = node.get("role") or node.get("tag") or "element"
    name = str(node.get("name") or node.get("text") or "").strip().replace("\n", " ")
    if len(name) > _MAX_LINE_TEXT:
        name = name[:_MAX_LINE_TEXT] + "…"
    ref = str(node.get("nodeId") or "")
    actions = node.get("actions") or []
    flags = [f"ref={ref}"] if ref else []
    if actions:
        flags.append("[" + ", ".join(str(a) for a in actions) + "]")
    suffix = " ".join(flags)
    label = f'"{name}"' if name else ""
    return f"{indent}- {role} {label} {suffix}".rstrip()


def _walk(tree: dict[str, Any], depth: int, budget: int) -> list[str]:
    """Collect lines depth-first until the budget is spent."""
    lines: list[str] = []
    shown = 0
    total_chars = 0
    stack: list[tuple[dict[str, Any], int]] = [(tree, depth)]
    while stack and total_chars < budget:
        node, level = stack.pop()
        if node.get("visible") is False:
            continue
        line = _line_for(node, level)
        lines.append(line)
        shown += 1
        total_chars += len(line)
        children = [c for c in (node.get("children") or []) if isinstance(c, dict)]
        for child in reversed(children):
            stack.append((child, level + 1))
    return lines, shown, total_chars


def format_snapshot(tree: dict[str, Any], *, max_tokens: int = _DEFAULT_MAX_TOKENS) -> dict[str, Any]:
    """Format a DOM-stream snapshot into agent lines with token accounting."""
    budget = max(1, min(int(max_tokens) * _TOKEN_PER_CHAR, 10_000_000))
    node_count = 0
    counter_stack: list[dict[str, Any]] = [tree]
    while counter_stack:
        node = counter_stack.pop()
        node_count += 1
        counter_stack.extend(c for c in (node.get("children") or []) if isinstance(c, dict))
    lines, shown, total_chars = _walk(tree, 0, budget)
    truncated = shown < node_count
    return {
        "lines": lines,
        "tokens_estimate": _estimate_tokens("\n".join(lines)) if lines else 0,
        "max_tokens": int(max_tokens),
        "truncated": truncated,
        "nodeCount": node_count,
        "shownCount": shown,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_snapshot.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
uv run ruff check kahin/agent_snapshot.py tests/test_agent_snapshot.py
git add kahin/agent_snapshot.py tests/test_agent_snapshot.py
git commit -m "feat(agent): ref-based snapshot formatter with token budget"
```

---

### Task 2: `kahin_mirage_snapshot` tool

**Files:**
- Create: `kahin/tools/agent_mirage.py`
- Modify: `kahin/tools/__init__.py` (register)
- Test: `tests/test_e2e_reliability.py` (extend — snapshot e2e)

**Interfaces:**
- Consumes: `format_snapshot` (Task 1), `dom_stream_mirage.mirage_dom_snapshot` (mevcut tool, dom_stream_mirage.py:166), `_capture_page_session`, `_text_arg`, `_bounded_int`, `_json_error`
- Produces: `kahin_mirage_snapshot(selector=None, max_tokens=1500, include_hidden=False, frame_id=None)` → `{"lines": [...], "tokens_estimate", "max_tokens", "truncated", "nodeCount", "shownCount", "url", "title", "readyState", "cursor", "streamId", "hint"}` — hint: `"reset"`/`"dropped"` durumunda `"take a fresh snapshot before acting on refs"`

- [ ] **Step 1: Write the failing test** — extend `tests/test_e2e_reliability.py`

```python
from kahin.tools import agent_mirage


@pytest.mark.asyncio
async def test_snapshot_refs_and_token_budget(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><h1>Todo</h1><input id='t' placeholder='What next?'><button id='b'>Add</button></body></html>"
    ))
    result = _loads(await agent_mirage.mirage_snapshot(max_tokens=800))
    assert result.get("lines"), result
    assert result.get("tokens_estimate", 0) > 0
    assert result.get("max_tokens") == 800
    assert result.get("truncated") is not None
    joined = "\n".join(result["lines"])
    assert "textbox" in joined and "Add" in joined
    # refs must be action-ready: find the Add button's ref and click it via dom_action
    add_line = next(line for line in result["lines"] if "Add" in line)
    ref = add_line.split("ref=")[1].split("]")[0]
    assert ref.startswith("n")
    action = _loads(await dom_stream_mirage.mirage_dom_action(node_id=ref, action="click"))
    assert action.get("ready") is True, action
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_e2e_reliability.py::test_snapshot_refs_and_token_budget -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kahin.tools.agent_mirage'`

- [ ] **Step 3: Write the implementation** — `kahin/tools/agent_mirage.py`

```python
"""agent_mirage.py — Agent-native observation tools (Faz 2)."""

from __future__ import annotations

from typing import Any

import orjson

from kahin._mcp import mcp
from kahin.agent_snapshot import format_snapshot
from kahin.tools._common import _RO, _healer_ref
from kahin.tools.dom_stream_mirage import dom_snapshot
from kahin.tools.pilot_mirage import (
    _MAX_SELECTOR_LENGTH,
    _bounded_float,
    _bounded_int,
    _capture_page_session,
    _json_error,
    _text_arg,
)

_MAX_TOKEN_BUDGET = 100_000


@mcp.tool(name="kahin_mirage_snapshot", annotations=_RO)
async def mirage_snapshot(
    selector: str | None = None,
    max_tokens: int = 1500,
    include_hidden: bool = False,
    frame_id: str | None = None,
) -> str:
    """Mirage: agent-ready page observation. Returns compact lines with
    live DOM refs (nodeId) — every ref is actable via kahin_mirage_dom_action.
    max_tokens caps the output; tokens_estimate/truncated are always
    reported. On reset/dropped the hint demands a fresh snapshot."""
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= _MAX_TOKEN_BUDGET:
        return _json_error("kahin_mirage_snapshot", f"max_tokens must be an integer in 1..{_MAX_TOKEN_BUDGET}", "invalid_argument", field="max_tokens")
    selector_value, error = None, None
    if selector is not None:
        selector_value, error = _text_arg(selector, tool="kahin_mirage_snapshot", field="selector", maximum=_MAX_SELECTOR_LENGTH)
        if error:
            return error
    async with _healer_ref.safe(
        "kahin_mirage_snapshot", selector=selector_value[:80] if selector_value else None,
        max_tokens=max_tokens, include_hidden=include_hidden, frame_id=frame_id,
    ):
        raw = _loads(await dom_snapshot(
            selector=selector_value, max_nodes=_bounded_int(2000, minimum=1, maximum=5000, default=2000),
            max_depth=_bounded_int(24, minimum=1, maximum=32, default=24),
            include_hidden=include_hidden, frame_id=frame_id,
        ))
        if isinstance(raw, dict) and raw.get("error"):
            return orjson.dumps(raw, option=orjson.OPT_INDENT_2).decode()
        tree = raw.get("root") or {}
        formatted = format_snapshot(tree, max_tokens=max_tokens)
        payload: dict[str, Any] = dict(formatted)
        for key in ("streamId", "cursor", "revision", "url", "title", "readyState", "focused"):
            if key in raw:
                payload[key] = raw[key]
        if raw.get("reset") or raw.get("dropped"):
            payload["hint"] = "take a fresh snapshot before acting on refs"
        payload["include_hidden"] = bool(include_hidden)
        return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()
```

Needed helpers: `_loads` (orjson.loads) — define at module top:

```python
def _loads(text: str) -> dict[str, Any]:
    return orjson.loads(text)
```

Also note: `dom_snapshot` is the MCP tool function in `dom_stream_mirage.py` — verify its exact name and signature (dom_stream_mirage.py:166; it may be `mirage_dom_snapshot`). Use the real name; the wrapping here calls it directly so the result is a JSON string.

Register in `kahin/tools/__init__.py`:

```python
from kahin.tools import agent_mirage  # noqa: F401
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_e2e_reliability.py::test_snapshot_refs_and_token_budget -v
```

Expected: PASS — ref'siz hiçbir satır yok; action ref üzerinden çalışıyor.

- [ ] **Step 5: Commit**

```bash
uv run ruff check kahin/tools/agent_mirage.py kahin/tools/__init__.py
git add kahin/tools/agent_mirage.py kahin/tools/__init__.py tests/test_e2e_reliability.py
git commit -m "feat(agent): kahin_mirage_snapshot — refs + token budget"
```

---

### Task 3: `return_snapshot` on action tools + `kahin_mirage_fill_form`

**Files:**
- Modify: `kahin/tools/agent_mirage.py` (fill_form)
- Modify: `kahin/tools/reliability_mirage.py` (return_snapshot on click/type/check/select/dblclick/drag) — opsiyonel param
- Test: `tests/test_e2e_reliability.py` (extend)

**Interfaces:**
- Produces: `kahin_mirage_fill_form(fields=[{"ref": "n5", "text": "..."}], timeout=10.0, frame_id=None)` — her alan için `mirage_dom_action(node_id=ref, action="type", text=...)`; `{"filled": N, "results": [...]}`; ref yoksa hata `stale_node` + `requiresSnapshot: true`. `return_snapshot` param: action tool'ları başarıdan sonra `kahin_mirage_snapshot(max_tokens=1500)` çağırır ve sonuca `"snapshot": {...}` ekler.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_fill_form_by_refs(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><input id='a' placeholder='Name'><input id='b' placeholder='Email'>"
        "<button id='s'>Save</button></body></html>"
    ))
    snap = _loads(await agent_mirage.mirage_snapshot(max_tokens=800))
    refs: dict[str, str] = {}
    for line in snap["lines"]:
        if "Name" in line:
            refs["name"] = line.split("ref=")[1].split("]")[0]
        if "Email" in line:
            refs["email"] = line.split("ref=")[1].split("]")[0]
    assert "name" in refs and "email" in refs
    result = _loads(await agent_mirage.mirage_fill_form(fields=[
        {"ref": refs["name"], "text": "Ada"},
        {"ref": refs["email"], "text": "ada@example.com"},
    ]))
    assert result.get("filled") == 2, result
    v1 = _loads(await pilot_mirage.mirage_get_value("#a"))
    v2 = _loads(await pilot_mirage.mirage_get_value("#b"))
    assert v1.get("value") == "Ada" and v2.get("value") == "ada@example.com"


@pytest.mark.asyncio
async def test_click_returns_snapshot_when_requested(mirage_tools: None) -> None:
    await _navigate(_doc(
        "<html><body><button id='b'>Toggle</button><div id='o'>off</div>"
        "<script>document.getElementById('b').addEventListener('click', () => { "
        "document.getElementById('o').textContent = 'on'; });</script></body></html>"
    ))
    result = _loads(await reliability_mirage.mirage_click("#b", timeout=5.0, return_snapshot=True))
    assert not result.get("error"), result
    assert result.get("snapshot", {}).get("lines"), result
    joined = "\n".join(result["snapshot"]["lines"])
    assert "on" in joined or "Toggle" in joined
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_e2e_reliability.py::test_fill_form_by_refs tests/test_e2e_reliability.py::test_click_returns_snapshot_when_requested -v`
Expected: FAIL — attribute missing

- [ ] **Step 3: Implement `fill_form`** — append to `agent_mirage.py`

```python
_FILL_FIELDS_MAX = 100


@mcp.tool(name="kahin_mirage_fill_form", annotations=_RW)
async def mirage_fill_form(
    fields: list[dict[str, Any]],
    timeout: float = 10.0,
    frame_id: str | None = None,
) -> str:
    """Mirage: fill several fields in one call. fields: [{ref, text}],
    ref is a nodeId from kahin_mirage_snapshot/dom_snapshot. Returns
    {filled, results[]} — each result is the dom_action output."""
    if not isinstance(fields, list) or not fields or len(fields) > _FILL_FIELDS_MAX:
        return _json_error("kahin_mirage_fill_form", f"fields must be a list of 1..{_FILL_FIELDS_MAX} items", "invalid_argument", field="fields")
    from kahin.tools.dom_stream_mirage import dom_action  # noqa: PLC0415

    timeout_value = _bounded_float(timeout, minimum=0.0, maximum=_MAX_WAIT_TIMEOUT, default=10.0)
    async with _healer_ref.safe("kahin_mirage_fill_form", count=len(fields), timeout=timeout_value, frame_id=frame_id):
        results: list[dict[str, Any]] = []
        for field in fields:
            if not isinstance(field, dict):
                return _json_error("kahin_mirage_fill_form", "each field must be an object {ref, text}", "invalid_argument")
            ref = field.get("ref")
            text = field.get("text")
            if not isinstance(ref, str) or not isinstance(text, str):
                return _json_error("kahin_mirage_fill_form", "each field needs a string ref and a string text", "invalid_argument")
            outcome = _loads(await dom_action(node_id=ref, action="type", text=text, frame_id=frame_id))
            results.append(outcome)
            if outcome.get("requiresSnapshot"):
                return orjson.dumps({
                    "error": "a field went stale mid-form",
                    "code": "stale_node",
                    "requiresSnapshot": True,
                    "filled": len(results) - 1,
                    "results": results,
                }, option=orjson.OPT_INDENT_2).decode()
        return orjson.dumps({"filled": len(results), "results": results}, option=orjson.OPT_INDENT_2).decode()
```

Needed import: `_MAX_WAIT_TIMEOUT` from pilot_mirage; `dom_action` from `dom_stream_mirage` (verify the real function name at dom_stream_mirage.py:275 — it may be `mirage_dom_action`; use the real name).

- [ ] **Step 4: Add `return_snapshot` to click** — `reliability_mirage.py`

In `mirage_click` (Faz 1 Task 5), add the parameter and post-action hook:

```python
async def mirage_click(selector: str, timeout: float = 10.0, return_snapshot: bool = False, frame_id: str | None = None) -> str:
    ...
    payload = {"clicked": selector_value, "x": x, "y": y, "waited": True}
    if return_snapshot:
        from kahin.tools.agent_mirage import mirage_snapshot
        payload["snapshot"] = _loads(await mirage_snapshot(max_tokens=1500, frame_id=frame_id))
    return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()
```

Apply the same `return_snapshot` hook to `mirage_type`, `mirage_check`, `mirage_uncheck`, `mirage_select_option`, `mirage_dblclick`, `mirage_drag` (identical pattern; each tool gets the bool param default False and appends `payload["snapshot"]` when requested).

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_e2e_reliability.py::test_fill_form_by_refs tests/test_e2e_reliability.py::test_click_returns_snapshot_when_requested -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
uv run ruff check kahin/tools/agent_mirage.py kahin/tools/reliability_mirage.py
git add kahin/tools/agent_mirage.py kahin/tools/reliability_mirage.py tests/test_e2e_reliability.py
git commit -m "feat(agent): fill_form by refs + return_snapshot on actions"
```

---

### Task 4: Session state persistence — `kahin_mirage_state_save` / `kahin_mirage_state_load`

**Files:**
- Modify: `kahin/tools/agent_mirage.py`
- Test: `tests/test_e2e_reliability.py` (extend)

**Interfaces:**
- Produces: `kahin_mirage_state_save(path)` → `{"saved": true, "path", "cookies", "localStorage", "sessionStorage", "url"}`; `kahin_mirage_state_load(path)` → restores cookies + local/session storage + navigates to saved url → `{"loaded": true, "path", "url"}`
- Storage: single JSON file; `path` MUST be absolute (or `~`-prefixed); refuses relative paths with `invalid_argument` (güvenlik: upload_files kuralı)
- Consumes: `mirage_cookie_get/set`, `mirage_storage_local_get/set`, `mirage_storage_session_get` (storage_mirage.py), `pilot.navigate`

- [ ] **Step 1: Write the failing test** — extend `tests/test_e2e_reliability.py` (needs a temp dir)

```python
import tempfile
from pathlib import Path


@pytest.mark.asyncio
async def test_state_save_load_roundtrip(mirage_tools: None, tmp_path: Path) -> None:
    from kahin.tools import agent_mirage, storage_mirage

    await _navigate(_doc(
        "<html><body><script>"
        "localStorage.setItem('k1', 'v1'); sessionStorage.setItem('s1', 'sv');"
        "document.cookie = 'c1=vv; path=/';"
        "</script></body></html>"
    ))
    await agent_mirage.mirage_wait_for_timeout(ms=200)
    save_path = str(tmp_path / "state.json")
    saved = _loads(await agent_mirage.mirage_state_save(save_path))
    assert saved.get("saved") is True, saved
    assert saved.get("localStorage", {}).get("k1") == "v1"
    assert saved.get("sessionStorage", {}).get("s1") == "sv"

    # new tab, wipe storage, reload
    await pilot_mirage.mirage_clear_...  # no wipe tool; instead load into fresh state
    loaded = _loads(await agent_mirage.mirage_state_load(save_path))
    assert loaded.get("loaded") is True, loaded
    await agent_mirage.mirage_wait_for_timeout(ms=300)
    ls = _loads(await storage_mirage.mirage_storage_local_get())
    assert ls.get("k1") == "v1", ls


@pytest.mark.asyncio
async def test_state_save_rejects_relative_path(mirage_tools: None) -> None:
    from kahin.tools import agent_mirage

    result = _loads(await agent_mirage.mirage_state_save("relative/state.json"))
    assert result.get("code") == "invalid_argument", result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_e2e_reliability.py::test_state_save_load_roundtrip tests/test_e2e_reliability.py::test_state_save_rejects_relative_path -v`
Expected: FAIL — attribute missing

- [ ] **Step 3: Implement** — append to `agent_mirage.py`

```python
from pathlib import Path


def _absolute_path(value: str, tool: str, field: str) -> tuple[Path, str | None]:
    expanded = Path(value).expanduser()
    if not expanded.is_absolute():
        return Path(), _json_error(tool, f"{field} must be an absolute path", "invalid_argument", field=field)
    return expanded, None


@mcp.tool(name="kahin_mirage_state_save", annotations=_RO)
async def mirage_state_save(path: str) -> str:
    """Mirage: persist the current session (cookies, localStorage,
    sessionStorage, current url) to an absolute-path JSON file. Restore
    with kahin_mirage_state_load."""
    path_value, error = _text_arg(path, tool="kahin_mirage_state_save", field="path", maximum=4_096)
    if error:
        return error
    target, error = _absolute_path(path_value, "kahin_mirage_state_save", "path")
    if error:
        return error
    async with _healer_ref.safe("kahin_mirage_state_save", path=str(target)):
        from kahin.tools import storage_mirage  # noqa: PLC0415

        cookies_raw = _loads(await storage_mirage.mirage_cookie_get())
        cookies = cookies_raw.get("cookies") if isinstance(cookies_raw, dict) else cookies_raw
        local_raw = _loads(await storage_mirage.mirage_storage_local_get())
        session_raw = _loads(await storage_mirage.mirage_storage_session_get())
        local = local_raw if isinstance(local_raw, dict) else {}
        session = session_raw if isinstance(session_raw, dict) else {}
        if isinstance(local_raw, dict) and isinstance(local_raw.get("error"), str):
            return orjson.dumps(local_raw, option=orjson.OPT_INDENT_2).decode()
        if isinstance(session_raw, dict) and isinstance(session_raw.get("error"), str):
            return orjson.dumps(session_raw, option=orjson.OPT_INDENT_2).decode()
        url = ""
        try:
            from kahin.the_twins import mirage as mirage_mod  # noqa: PLC0415
            from kahin import _state  # noqa: PLC0415
            engine = _state._current_engine
            if isinstance(engine, mirage_mod.Mirage):
                pages = await engine.list_pages()
                current = next((p for p in pages if p.get("targetId") == engine._current_target), None)
                url = str((current or {}).get("url") or "")
        except Exception:  # noqa: BLE001
            url = ""
        payload = {
            "version": 1,
            "url": url,
            "cookies": cookies if isinstance(cookies, list) else [],
            "localStorage": local,
            "sessionStorage": session,
        }
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode(), encoding="utf-8")
        except OSError as exc:
            return _json_error("kahin_mirage_state_save", f"cannot write state file: {exc}", "tool_failed", path=str(target))
        return orjson.dumps({
            "saved": True, "path": str(target),
            "cookies": len(payload["cookies"]), "localStorage": len(payload["localStorage"]),
            "sessionStorage": len(payload["sessionStorage"]), "url": url,
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_mirage_state_load", annotations=_RW)
async def mirage_state_load(path: str) -> str:
    """Mirage: restore a session saved by kahin_mirage_state_save."""
    path_value, error = _text_arg(path, tool="kahin_mirage_state_load", field="path", maximum=4_096)
    if error:
        return error
    target, error = _absolute_path(path_value, "kahin_mirage_state_load", "path")
    if error:
        return error
    async with _healer_ref.safe("kahin_mirage_state_load", path=str(target)):
        try:
            payload = orjson.loads(target.read_text(encoding="utf-8"))
        except OSError as exc:
            return _json_error("kahin_mirage_state_load", f"cannot read state file: {exc}", "tool_failed", path=str(target))
        except orjson.JSONDecodeError as exc:
            return _json_error("kahin_mirage_state_load", f"state file is not valid JSON: {exc}", "invalid_argument", path=str(target))
        from kahin.tools import storage_mirage  # noqa: PLC0415

        cookies = payload.get("cookies") or []
        if cookies:
            outcome = _loads(await storage_mirage.mirage_cookie_set(cookies=cookies))
            if isinstance(outcome, dict) and outcome.get("error"):
                return orjson.dumps(outcome, option=orjson.OPT_INDENT_2).decode()
        local = payload.get("localStorage") or {}
        for key, value in local.items():
            if isinstance(key, str) and isinstance(value, str):
                await storage_mirage.mirage_storage_local_set(key=key, value=value)
        url = payload.get("url")
        if isinstance(url, str) and url:
            navigate = _loads(await pilot.navigate(url=url))
            if isinstance(navigate, dict) and navigate.get("error"):
                return orjson.dumps(navigate, option=orjson.OPT_INDENT_2).decode()
        return orjson.dumps({
            "loaded": True, "path": str(target), "url": url,
            "cookies": len(cookies), "localStorage": len(local),
        }, option=orjson.OPT_INDENT_2).decode()
```

Note: the test's wipe step is replaced by "load into fresh state" — the load test navigates (fresh origin context) and then asserts localStorage restored. If `mirage_cookie_set` expects a specific shape, adapt `payload["cookies"]` to it (check storage_mirage.py:232's cookie dict shape during implementation).

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_e2e_reliability.py::test_state_save_load_roundtrip tests/test_e2e_reliability.py::test_state_save_rejects_relative_path -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff check kahin/tools/agent_mirage.py
git add kahin/tools/agent_mirage.py tests/test_e2e_reliability.py
git commit -m "feat(agent): session state save/load (cookies + web storage)"
```

---

### Task 5: Identity persistence — `browser_start(identity=...)` + `kahin_identity_*`

**Files:**
- Modify: `kahin/tools/pilot.py` (`browser_start` 139-280)
- Modify: `kahin/the_twins/mirage.py` (`start` 345-419 — identity config injection)
- Create: `kahin/tools/agent_mirage.py` additions (identity tools)
- Test: `tests/test_e2e_reliability.py` (extend)

**Interfaces:**
- Produces: `kahin_browser_start(engine="mirage", headless=True, port=0, identity=None)` — `identity` is a NAME of a saved identity (see below) or an inline dict. `kahin_identity_new(name, os="random", screen=None)` → generates a fresh identity (BrowserForge-style config) and saves it; `kahin_identity_save(name, config)` → saves a raw config dict; `kahin_identity_list()` → names + fingerprint summary; `kahin_identity_delete(name)`; `kahin_identity_report()` → the ACTIVE engine's identity summary (UA, screen, seeds, webgl, tz, locale) or `{"code": "engine_unavailable"}`.
- Storage: `~/.config/kahin/identities/<name>.json` (absolute path rule; mkdir -p; refuses path traversal: name must match `^[a-zA-Z0-9_-]+$`)

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_identity_roundtrip_pins_user_agent(mirage_tools: None) -> None:
    from kahin.tools import agent_mirage, emulation_mirage

    created = _loads(await agent_mirage.kahin_identity_new("testid"))
    assert created.get("saved") is True, created
    assert created.get("name") == "testid"
    ua = created.get("summary", {}).get("navigator.userAgent", "")
    assert ua, created
    await pilot.browser_stop()
    started = _loads(await pilot.browser_start(engine="mirage", identity="testid"))
    assert started.get("status") == "started", started
    tab = _loads(await trainman_mirage.mirage_tab_new())
    assert tab.get("targetId"), tab
    await asyncio.sleep(0.5)
    real_ua = _loads(await emulation_mirage.mirage_set_user_agent(user_agent=""))
    assert "user_agent" in real_ua, real_ua
    listed = _loads(await agent_mirage.kahin_identity_list())
    names = [i.get("name") for i in listed.get("identities", [])]
    assert "testid" in names, listed
    deleted = _loads(await agent_mirage.kahin_identity_delete("testid"))
    assert deleted.get("deleted") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_e2e_reliability.py::test_identity_roundtrip_pins_user_agent -v`
Expected: FAIL — `browser_start() got an unexpected keyword argument 'identity'`

- [ ] **Step 3: Implement engine-side identity injection** — `mirage.py` start

In `Mirage.start(self, headless=True, port=0, **kwargs)` (mirage.py:345), after `launch_options()` merge (mirage.py:364-375), add:

```python
identity = kwargs.get("identity")
if isinstance(identity, dict) and identity:
    try:
        from camoufox.utils import launch_options  # noqa: PLC0415

        merged = launch_options(config=identity, **{})
        env = merged.get("env") or {}
        for key, value in env.items():
            os.environ.setdefault(key, str(value)) if False else None
        # env merges into the child env below; prefs already come from config
    except Exception:  # noqa: BLE001 - identity injection must not crash boot
        logger.warning("identity config injection failed: %s", exc_info=True)
```

Check how the current launch flow consumes `launch_options()` output — the child env merge already happens (mirage.py:364-375). The cleanest injection: call `launch_options(config=identity)` and merge its `env`/`args`/`prefs` the same way the existing code merges the default options, with the identity taking precedence. Adapt the diff to the actual merge code — the invariant to test: with a pinned identity containing `"navigator.userAgent": "<fixed>"`, the engine's UA equals that fixed value.

- [ ] **Step 4: Implement the tool surface** — append to `agent_mirage.py`

```python
import re

_IDENTITY_DIR = Path.home() / ".config" / "kahin" / "identities"
_IDENTITY_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _identity_path(name: str) -> Path | None:
    if not _IDENTITY_NAME_RE.match(name):
        return None
    return _IDENTITY_DIR / f"{name}.json"


@mcp.tool(name="kahin_identity_new", annotations=_RO)
async def kahin_identity_new(name: str, os_target: str = "random") -> str:
    """Create and persist a fresh identity (BrowserForge-style config).
    os_target: windows|macos|linux|random. Returns {saved, name, summary}."""
    name_value, error = _text_arg(name, tool="kahin_identity_new", field="name", maximum=64)
    if error:
        return error
    path = _identity_path(name_value)
    if path is None:
        return _json_error("kahin_identity_new", "name must match ^[a-zA-Z0-9_-]{1,64}$", "invalid_argument", field="name")
    if os_target not in ("windows", "macos", "linux", "random"):
        return _json_error("kahin_identity_new", "os_target must be windows|macos|linux|random", "invalid_argument", field="os_target")
    async with _healer_ref.safe("kahin_identity_new", name=name_value, os_target=os_target):
        try:
            from camoufox.fingerprints import generate_context_fingerprint  # noqa: PLC0415
        except ImportError:
            return _json_error("kahin_identity_new", "camoufox.fingerprints unavailable", "capability_requires_mirage")
        os_filter = None if os_target == "random" else os_target
        try:
            generated = generate_context_fingerprint(os=os_filter)
        except Exception as exc:  # noqa: BLE001
            return _json_error("kahin_identity_new", f"fingerprint generation failed: {exc}", "tool_failed")
        config = generated.get("config") or {}
        try:
            _IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(orjson.dumps({"version": 1, "config": config}, option=orjson.OPT_INDENT_2).decode(), encoding="utf-8")
        except OSError as exc:
            return _json_error("kahin_identity_new", f"cannot write identity: {exc}", "tool_failed", path=str(path))
        return orjson.dumps({
            "saved": True, "name": name_value, "path": str(path),
            "summary": {
                "navigator.userAgent": config.get("navigator.userAgent", ""),
                "screen.width": config.get("screen.width", ""),
                "screen.height": config.get("screen.height", ""),
                "timezone": config.get("timezone", ""),
                "webGl:renderer": config.get("webGl:renderer", ""),
            },
        }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_identity_save", annotations=_RO)
async def kahin_identity_save(name: str, config: dict[str, Any]) -> str:
    """Persist a raw fingerprint config dict under a name."""
    name_value, error = _text_arg(name, tool="kahin_identity_save", field="name", maximum=64)
    if error:
        return error
    path = _identity_path(name_value)
    if path is None:
        return _json_error("kahin_identity_save", "name must match ^[a-zA-Z0-9_-]{1,64}$", "invalid_argument", field="name")
    if not isinstance(config, dict) or not config:
        return _json_error("kahin_identity_save", "config must be a non-empty object", "invalid_argument", field="config")
    async with _healer_ref.safe("kahin_identity_save", name=name_value):
        try:
            _IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(orjson.dumps({"version": 1, "config": config}, option=orjson.OPT_INDENT_2).decode(), encoding="utf-8")
        except OSError as exc:
            return _json_error("kahin_identity_save", f"cannot write identity: {exc}", "tool_failed", path=str(path))
        return orjson.dumps({"saved": True, "name": name_value, "path": str(path)}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_identity_list", annotations=_RO)
async def kahin_identity_list() -> str:
    """List saved identities with fingerprint summaries."""
    async with _healer_ref.safe("kahin_identity_list"):
        identities: list[dict[str, Any]] = []
        if _IDENTITY_DIR.is_dir():
            for file in sorted(_IDENTITY_DIR.glob("*.json")):
                try:
                    payload = orjson.loads(file.read_text(encoding="utf-8"))
                except (OSError, orjson.JSONDecodeError):
                    continue
                config = payload.get("config") or {}
                identities.append({
                    "name": file.stem,
                    "summary": {
                        "navigator.userAgent": config.get("navigator.userAgent", ""),
                        "screen.width": config.get("screen.width", ""),
                        "screen.height": config.get("screen.height", ""),
                        "timezone": config.get("timezone", ""),
                    },
                })
        return orjson.dumps({"identities": identities}, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_identity_delete", annotations=_RO)
async def kahin_identity_delete(name: str) -> str:
    """Delete a saved identity."""
    name_value, error = _text_arg(name, tool="kahin_identity_delete", field="name", maximum=64)
    if error:
        return error
    path = _identity_path(name_value)
    if path is None:
        return _json_error("kahin_identity_delete", "name must match ^[a-zA-Z0-9_-]{1,64}$", "invalid_argument", field="name")
    async with _healer_ref.safe("kahin_identity_delete", name=name_value):
        try:
            if path.is_file():
                path.unlink()
        except OSError as exc:
            return _json_error("kahin_identity_delete", f"cannot delete identity: {exc}", "tool_failed", path=str(path))
        return orjson.dumps({"deleted": True, "name": name_value}, option=orjson.OPT_INDENT_2).decode()
```

- [ ] **Step 5: Wire `identity` into `browser_start`** — `pilot.py`

In `browser_start` (pilot.py:139), add `identity: str | dict | None = None` to the signature. Before the engine start call, resolve it:

```python
identity_config = None
if identity is not None:
    if isinstance(identity, dict):
        identity_config = identity
    elif isinstance(identity, str):
        from kahin.tools.agent_mirage import _identity_path  # noqa: PLC0415
        path = _identity_path(identity)
        if path is None or not path.is_file():
            return _json_error("kahin_browser_start", f"unknown identity: {identity!r}", "invalid_argument", field="identity")
        try:
            payload = orjson.loads(path.read_text(encoding="utf-8"))
        except (OSError, orjson.JSONDecodeError) as exc:
            return _json_error("kahin_browser_start", f"identity file unreadable: {exc}", "invalid_argument", field="identity")
        identity_config = payload.get("config") or {}
```

then pass `identity=identity_config` into the engine start call. The engine start (`Mirage.start`) already receives `**kwargs` — it will flow through after Step 3.

- [ ] **Step 6: Run tests**

```bash
uv run pytest tests/test_e2e_reliability.py::test_identity_roundtrip_pins_user_agent -v
```

Expected: PASS — identity config reaches the engine and the UA is pinned.

- [ ] **Step 7: Commit**

```bash
uv run ruff check kahin/the_twins/mirage.py kahin/tools/pilot.py kahin/tools/agent_mirage.py
git add kahin/the_twins/mirage.py kahin/tools/pilot.py kahin/tools/agent_mirage.py tests/test_e2e_reliability.py
git commit -m "feat(agent): identity persistence — new/save/list/delete + browser_start(identity=)"
```

---

### Task 6: `kahin_agent_status` + tool surface sync

**Files:**
- Modify: `kahin/tools/agent_mirage.py`
- Modify: `tests/test_oracle.py` (tool count)
- Modify: `AGENTS.md`, `CHANGELOG.md`

**Interfaces:**
- Produces: `kahin_agent_status()` → `{"engine", "alive", "url", "title", "readyState", "tabCount", "currentTab", "refsLive": bool (dom stream active?), "pendingDialogs": N, "networkEvents": N, "consoleMessages": N, "domCursor": N|None, "identity": {name, summary} | None}` — the agent-loop overview; never raises.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_agent_status_summary(mirage_tools: None) -> None:
    await _navigate(_doc("<html><head><title>T</title></head><body>x</body></html>"))
    status = _loads(await agent_mirage.kahin_agent_status())
    assert status.get("alive") is True, status
    assert status.get("url", "").startswith("data:text/html")
    assert status.get("tabCount", 0) >= 1
    assert isinstance(status.get("consoleMessages"), int)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_e2e_reliability.py::test_agent_status_summary -v`
Expected: FAIL — attribute missing

- [ ] **Step 3: Implement** — append to `agent_mirage.py`

```python
@mcp.tool(name="kahin_agent_status", annotations=_RO)
async def kahin_agent_status() -> str:
    """Agent loop overview: engine liveness, current page state, tab count,
    pending dialogs, buffered network/console, DOM stream cursor."""
    async with _healer_ref.safe("kahin_agent_status"):
        from kahin import _state  # noqa: PLC0415
        from kahin.the_twins import mirage as mirage_mod  # noqa: PLC0415

        engine = _state._current_engine
        if engine is None:
            return orjson.dumps({
                "engine": None, "alive": False, "hint": "use kahin_browser_start",
            }, option=orjson.OPT_INDENT_2).decode()
        payload: dict[str, Any] = {"engine": getattr(engine, "engine_name", "unknown")}
        if isinstance(engine, mirage_mod.Mirage):
            try:
                health = await engine.health()
                payload["alive"] = bool(health.get("alive"))
            except Exception:  # noqa: BLE001
                payload["alive"] = False
            try:
                pages = await engine.list_pages()
                payload["tabCount"] = len(pages)
                current = next((p for p in pages if p.get("targetId") == engine._current_target), None)
                payload["currentTab"] = engine._current_target
                payload["url"] = (current or {}).get("url", "")
                payload["title"] = (current or {}).get("title", "")
            except Exception:  # noqa: BLE001
                pass
            payload["domCursor"] = getattr(engine, "_dom_cursor", None)
        else:
            payload["alive"] = bool(engine.is_alive()) if hasattr(engine, "is_alive") else True
        payload["networkEvents"] = len(_state._network_requests or [])
        payload["consoleMessages"] = len(_state._console_messages or [])
        try:
            from kahin.tools import dialog_mirage  # noqa: PLC0415
            dialogs = _loads(await dialog_mirage.mirage_dialog_list())
            payload["pendingDialogs"] = len(dialogs.get("dialogs", [])) if isinstance(dialogs, dict) else 0
        except Exception:  # noqa: BLE001
            payload["pendingDialogs"] = 0
        return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()
```

(Adjust `_state` attribute names to the real module — `_state._network_requests`, `_state._console_messages` per the Faz 0 inventory; if the DOM stream cursor lives elsewhere, drop `domCursor`.)

- [ ] **Step 4: Tool count + docs sync**

```bash
uv run pytest tests/test_oracle.py -v
```

If the tool-count assertion fails, update the number in `tests/test_oracle.py` (Faz 1: +9 → 119; Faz 2 adds snapshot, fill_form, state_save, state_load, identity_new, identity_save, identity_list, identity_delete, agent_status → 128). Then update `AGENTS.md` (add the new tools to the MIRAGE list) and `CHANGELOG.md` (Faz 2 entry).

- [ ] **Step 5: Full verification**

```bash
KAHIN_REQUIRE_REAL_E2E=1 uv run pytest tests/ -v
uv run ruff check .
uv run pyright
```

Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add tests/test_oracle.py tests/test_e2e_reliability.py AGENTS.md CHANGELOG.md
git commit -m "feat(agent): agent_status overview + Faz 2 tool surface docs"
```
