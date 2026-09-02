# Kahin Persistent Observable Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add default persistent Mirage sessions, cursor-based live crawl progress, deterministic SVG visualization, and real WebExtension staging without removing current Kahin capabilities.

**Architecture:** Keep the existing one-engine Mirage/Juggler architecture. Persist the default Firefox profile at a bounded XDG data path, add a bounded event ledger beside the existing crawl result ledger, generate SVG in a pure standard-library module, and stage compatible addon manifests before passing real addon directories to Camoufox.

**Tech Stack:** Python 3.12+, FastMCP, asyncio, orjson, Camoufox launch options, standard-library SVG/zip/json/path handling, pytest/pytest-asyncio.

## Global Constraints

- Preserve every existing public tool and existing response success shape.
- Never use Playwright or an external browser fallback inside Kahin.
- Never bypass CAPTCHA/access-denied or evade rate limits with identity rotation.
- Never store or return raw cookie values, proxy credentials, or fingerprint payloads in diagnostic/event responses.
- Keep one active Mirage engine slot and one crawler tab.
- Enforce bounded queue, event, row, string, and response sizes.

---

### Task 1: Make the default Mirage profile persistent

**Files:**
- Modify: `kahin/the_twins/mirage.py:376-430,472-640,2050-2070`
- Modify: `kahin/tools/pilot.py:312-620`
- Test: `tests/test_profile_persistence.py`

**Interfaces:**
- Produces `_profile_directory(persistent: bool, configured: str | None) -> tuple[Path, bool]` in `kahin/the_twins/mirage.py`.
- Extends `kahin_browser_start` with `persistent_profile: bool = True` and `profile_dir: str | None = None`.
- The start response adds `profile: {persistent: bool, path: str}` without exposing cookies or fingerprint data.

- [ ] **Step 1: Write failing profile policy tests**

```python
def test_default_profile_is_stable_under_xdg_data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    path, persistent = _profile_directory(True, None)
    assert persistent is True
    assert path == tmp_path / "kahin" / "profile"

def test_ephemeral_profile_is_not_stable(tmp_path):
    path, persistent = _profile_directory(False, None)
    assert persistent is False
    assert path is None

def test_configured_profile_must_be_absolute(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        _profile_directory(True, "relative-profile")
```

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: `uv run pytest -q tests/test_profile_persistence.py`

Expected: FAIL because `_profile_directory` does not exist.

- [ ] **Step 3: Implement bounded profile selection and lifecycle ownership**

Use `KAHIN_PROFILE_DIR` when present, otherwise `XDG_DATA_HOME/kahin/profile`, otherwise `~/.local/share/kahin/profile`. Reject relative paths, files, and paths outside the selected explicit value only when the explicit value is malformed. Keep temporary `mkdtemp(prefix="kahin-fp-")` for `persistent_profile=False`; set `self._persistent_profile` and do not remove persistent directories from `_remove_profile`.

Pass the two new arguments from `pilot.browser_start` through `candidate.start`, validate them before taking the lifecycle lock, and include only the boolean/path metadata in the response. Preserve identity, proxy, launch policy, and engine reuse conflict behavior.

- [ ] **Step 4: Run focused tests and existing lifecycle unit tests**

Run: `uv run pytest -q tests/test_profile_persistence.py tests/test_e2e_capability_routing.py -k 'not real'`

Expected: PASS; existing start/reuse response assertions remain valid.

- [ ] **Step 5: Commit the independently useful persistence slice**

```bash
git add kahin/the_twins/mirage.py kahin/tools/pilot.py tests/test_profile_persistence.py
git commit -m "feat(mirage): persist the default browser profile"
```

### Task 2: Add a real-time crawl event stream

**Files:**
- Modify: `kahin/tools/crawler_mirage.py:250-465,800-1030,1429-1525`
- Modify: `kahin/_mcp.py:30-65`
- Test: `tests/test_crawl_events.py`

**Interfaces:**
- `_CrawlJob._record_event(kind: str, **payload: Any) -> None` records bounded event entries.
- `kahin_crawl_events(jobId: str | None = None, cursor: int = 0, limit: int = 100, waitMs: int = 0) -> str` returns `{jobId,cursor,oldestCursor,nextCursor,hasMore,cursorReset,events}`.

- [ ] **Step 1: Write failing event ledger/cursor tests**

```python
def test_event_cursor_reports_eviction_and_monotonic_indexes():
    job = _CrawlJob("crawl-test", _minimal_config(), ["https://example.com/"])
    for index in range(_MAX_EVENTS_PER_JOB + 2):
        job._record_event("progress", value=index)
    payload = json.loads(_event_payload(job, cursor=0, limit=10))
    assert payload["cursorReset"] is True
    assert payload["oldestCursor"] > 0
    assert payload["events"][0]["index"] == payload["oldestCursor"]

@pytest.mark.asyncio
async def test_crawl_events_waits_until_a_new_event():
    job = _CrawlJob("crawl-test", _minimal_config(), ["https://example.com/"])
    task = asyncio.create_task(_wait_for_events(job, cursor=0, wait_ms=1000))
    await asyncio.sleep(0)
    job._record_event("progress", currentUrl="https://example.com/")
    result = await task
    assert result["events"][0]["kind"] == "progress"
```

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: `uv run pytest -q tests/test_crawl_events.py`

Expected: FAIL because the event ledger and public tool do not exist.

- [ ] **Step 3: Implement bounded event recording and wake-up**

Add a max-10,000 event deque, monotonic `_event_seq`, `asyncio.Event`, and `_record_event`. Record state transitions, queue dequeue, result completion/failure, backoff, challenge, rotation, and terminal states. Copy only bounded status/counter fields into events; do not include cookies, proxy URLs, or raw identity configuration.

- [ ] **Step 4: Implement the public cursor/long-poll tool**

Add a strict parser for `cursor`, `limit` (1..100), and `waitMs` (0..30,000). Return the current window immediately; if empty and `waitMs > 0`, wait once for the event signal, clear it only after reading, and return the next window. Keep `cursorReset` semantics explicit when the deque evicts old entries.

- [ ] **Step 5: Verify crawl compatibility and documentation**

Run: `uv run pytest -q tests/test_crawl_events.py tests/test_oracle.py tests/test_e2e_reliability.py -k 'not real'`

Expected: PASS; `kahin_crawl_status` and `kahin_crawl_results` still return their existing fields and ORBIT documentation lists the new observer.

### Task 3: Add deterministic data visualization

**Files:**
- Create: `kahin/visualization.py`
- Create: `kahin/tools/visualization.py`
- Modify: `kahin/tools/__init__.py`
- Test: `tests/test_visualization.py`

**Interfaces:**
- `render_chart(rows: list[dict[str, Any]], chart_type: str, x_field: str, y_field: str, color_field: str | None, title: str) -> dict[str, Any]` returns normalized fields, summary, and escaped SVG.
- `kahin_visualize_data(rows, chartType="line", xField="", yField="", colorField=None, title="")` returns the bounded chart payload.

- [ ] **Step 1: Write failing renderer tests**

```python
def test_line_chart_contains_scaled_path_and_escaped_title():
    result = render_chart(
        [{"day": "Mon", "count": 2}, {"day": "Tue", "count": 5}],
        "line", "day", "count", None, "<Traffic>",
    )
    assert result["summary"]["rowCount"] == 2
    assert "<Traffic>" not in result["svg"]
    assert "&lt;Traffic&gt;" in result["svg"]
    assert "polyline" in result["svg"]

def test_unknown_chart_type_is_rejected():
    with pytest.raises(ValueError, match="chart_type"):
        render_chart([], "radar", "x", "y", None, "")
```

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: `uv run pytest -q tests/test_visualization.py`

Expected: FAIL because the renderer module does not exist.

- [ ] **Step 3: Implement bounded normalization and SVG rendering**

Support `line`, `bar`, `scatter`, and `pie`. Validate at most 5,000 rows, field names, finite numeric y values, and bounded title/text lengths. Scale into a fixed 960x540 viewBox, use XML escaping for every user value, group only by an optional bounded color field, and return row count/min/max/series summary plus the SVG.

- [ ] **Step 4: Register the MCP tool and verify output contracts**

Register the tool through the existing side-effect import pattern. Return structured `invalid_argument` JSON for bad input and preserve all existing tool imports. Run: `uv run pytest -q tests/test_visualization.py tests/test_oracle.py`.

### Task 4: Stage compatible WebExtensions and pass real addons to Camoufox

**Files:**
- Create: `kahin/extensions.py`
- Create: `kahin/tools/extensions_mirage.py`
- Modify: `kahin/the_twins/mirage.py:500-640`
- Modify: `kahin/tools/pilot.py:312-620`
- Modify: `kahin/tools/__init__.py`
- Test: `tests/test_extensions.py`

**Interfaces:**
- `stage_extension(source: str, destination_root: Path) -> dict[str, Any]` returns a staged path, manifest summary, and unsupported feature list.
- `kahin_extension_prepare(source, name=None)` stages a real addon and reports compatibility.
- `kahin_browser_start(addons: list[str] | None = None)` passes only validated staged addon paths to `launch_options(addons=...)`.

- [ ] **Step 1: Write failing manifest compatibility tests**

```python
def test_prepare_manifest_v3_content_scripts(tmp_path):
    source = write_extension(tmp_path, {"manifest_version": 3, "name": "Reader", "version": "1", "content_scripts": [{"matches": ["<all_urls>"], "js": ["content.js"]}]})
    result = stage_extension(str(source), tmp_path / "staged")
    assert result["compatible"] is True
    assert (Path(result["path"]) / "manifest.json").is_file()

def test_service_worker_is_explicitly_unsupported(tmp_path):
    source = write_extension(tmp_path, {"manifest_version": 3, "name": "Worker", "version": "1", "background": {"service_worker": "background.js"}})
    result = stage_extension(str(source), tmp_path / "staged")
    assert result["compatible"] is False
    assert "background.service_worker" in result["unsupported"]
```

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: `uv run pytest -q tests/test_extensions.py`

Expected: FAIL because there is no staged addon implementation.

- [ ] **Step 3: Implement safe directory/zip staging and manifest validation**

Accept an existing directory or zip archive, reject symlink/path traversal entries, require a valid manifest/name/version, copy only bounded files, normalize supported content scripts and permissions, and report unsupported Chrome-only features instead of removing behavior silently.

- [ ] **Step 4: Wire addons into real Mirage launch options**

Validate addon paths before starting, pass them through `launch_options(addons=...)`, retain the list only as bounded engine metadata, and include compatibility/path summaries without contents. A non-compatible addon must fail with a structured error before a browser process is spawned.

- [ ] **Step 5: Verify no-regression startup and package docs**

Run: `uv run pytest -q tests/test_extensions.py tests/test_e2e_capability_routing.py tests/test_oracle.py -k 'not real'`.

Expected: PASS; existing starts with no addons use the same launch path and no fallback automation library is introduced.
