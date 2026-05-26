# cdp-kahin-mcp — Optimization Audit

## 1) Optimization Summary

**Current health:** Fair. The codebase is small and clean overall, but contains several Python-specific bugs (mutable default args) and significant code duplication between the two engine implementations. Hot paths are cold (CDP schema queries are infrequent), so many issues are latent rather than active. Three issues are **critical** because they are correctness bugs, not performance issues.

**Top 3 highest-impact improvements:**
1. Fix mutable default arguments (`dict = {}`) in tool handlers — a Python bug that causes state leakage across calls
2. Eliminate ~100 lines of duplicated engine code between `shadow.py` and `mirage.py` — cuts maintenance surface in half
3. Add bounded event buffers to prevent unbounded memory growth in long sessions

**Biggest risk if no changes:**
- `kahin_execute_cdp(dict={})` silently mutates shared dict across calls, causing CDP commands to receive stale parameters from previous invocations
- Engine bug fixes require touching two identical code paths, doubling the chance of missed fixes
- Long-running sessions can OOM from unbounded `_network_requests` / `_console_messages` growth

---

## 2) Findings (Prioritized)

### F1: Mutable Default Arguments in Tool Functions

- **Category:** Reliability / Correctness
- **Severity:** Critical
- **Impact:** Bug correctness, silent data corruption
- **Evidence:**
  - `kahin/oracle.py:28` — `async def _auto_learn(domain: str, command: str, params: dict = {})`
  - `kahin/oracle.py:283` — `async def kahin_execute_cdp(domain: str, command: str, parameters: dict = {})`
- **Why it's inefficient/buggy:** Python evaluates default args once at function definition time. The same dict object is shared across all calls. If the dict is mutated inside the function, subsequent calls see the mutated dict. While the current code only reads from `params` (doesn't mutate), this is a latent bug waiting for any future edit that writes to the dict.
- **Recommended fix:** Replace `dict = {}` with `dict | None = None` and use `params or {}` inside.
- **Tradeoffs / Risks:** None. Safe refactor, preserves API.
- **Expected impact:** Eliminates a correctness bug. No measurable perf impact.
- **Removal Safety:** Safe
- **Reuse Scope:** Module-wide pattern

### F2: Duplicated Engine Implementation (shadow.py / mirage.py)

- **Category:** Maintainability, Code Reuse
- **Severity:** High
- **Impact:** Bug surface area doubled, maintenance overhead
- **Evidence:**
  - `kahin/the_twins/shadow.py:72-121` — `stop()`, `send_cdp()`, `screenshot()`, `on_event()`
  - `kahin/the_twins/mirage.py:86-134` — Identical methods, byte-for-byte copy
  - Only `start()` and `_wait_for_page_ws()` differ between the two engines
- **Why it's inefficient:** 84 out of 134 lines in mirage.py (63%) are exact duplicates of shadow.py. A bug fix or enhancement must be applied twice. This has already caused issues — `mirage.py` has a double `find_chrome` import that shadow.py doesn't.
- **Recommended fix:** Extract a `BaseCdpEngine` class that implements `stop()`, `send_cdp()`, `screenshot()`, `on_event()`. Have `Obscura` and `Mirage` inherit from it and only override `start()` and `_wait_for_page_ws()`.
- **Tradeoffs / Risks:** Needs careful verification that the shared methods truly don't diverge. Currently they are identical.
- **Expected impact:** ~50% reduction in engine code. Bug fixes apply once.
- **Removal Safety:** Safe — no external consumers of the engine classes exist.
- **Reuse Scope:** Module (the_twins/)

### F3: Duplicate Import in mirage.py

- **Category:** Code Reuse / Dead Code
- **Severity:** Low (cosmetic)
- **Evidence:** `kahin/the_twins/mirage.py:13-14` — `from kahin._chrome import find_chrome` appears twice
- **Why it's inefficient:** Confuses type checkers, suggests previous edit left a ghost line. No runtime impact (Python deduplicates imports).
- **Recommended fix:** Remove one of the import lines.
- **Tradeoffs / Risks:** None.
- **Expected impact:** Clean LSP output.
- **Removal Safety:** Safe
- **Reuse Scope:** Local file

### F4: Unbounded Event Buffer Growth

- **Category:** Memory / Reliability
- **Severity:** Medium
- **Impact:** Potential OOM in long sessions
- **Evidence:**
  - `kahin/_state.py:8-10` — Three lists with no cap: `_current_event_log`, `_network_requests`, `_console_messages`
  - `kahin/oracle.py:197` — Every CDP event appended without limit
  - `kahin/oracle.py:202` — Every network event appended without limit
  - `kahin/oracle.py:210` — Every console message appended without limit
- **Why it's inefficient:** In a session that processes 100,000 network requests, `_network_requests` grows to 100,000 entries. The `kahin_list_network_requests(limit=20)` tool only returns the last 20 but the list keeps all of them. Over hours of browsing, this grows unbounded.
- **Recommended fix:** Cap each buffer at a reasonable max (e.g., 10,000 entries) using `collections.deque(maxlen=10000)` or trim on append.
- **Tradeoffs / Risks:** Loses historical events beyond the cap. Acceptable since the tools only show the tail anyway.
- **Expected impact:** Bounded memory usage. High-confidence fix.
- **Removal Safety:** Safe
- **Reuse Scope:** Module (the_keymaker)

### F5: In-Function Imports (Import Inside Hot/Cold Paths)

- **Category:** Maintainability, Latency (minor)
- **Severity:** Low
- **Impact:** Cold-start latency (negligible on second call due to sys.modules cache)
- **Evidence:**
  - `kahin/oracle.py:31` — `from urllib.parse import urlparse` inside `_auto_learn()`
  - `kahin/the_twins/shadow.py:57` — `import httpx` inside `_wait_for_page_ws()`
  - `kahin/the_twins/shadow.py:117` — `import base64` inside `screenshot()`
  - `kahin/the_twins/mirage.py:71` — `import httpx` inside `_wait_for_page_ws()`
  - `kahin/the_twins/mirage.py:130` — `import base64` inside `screenshot()`
- **Why it's inefficient:** Imports are cached by `sys.modules` after first load, so repeated calls are fast. However, it adds latency to the first call of each function, is non-standard, and makes static analysis harder.
- **Recommended fix:** Move all imports to module top level.
- **Tradeoffs / Risks:** None.
- **Expected impact:** ~1-5ms faster first call per function. Mostly a code quality improvement.
- **Removal Safety:** Safe
- **Reuse Scope:** Module-wide

### F6: Dead Code in SchemaEngine — Unused _command_list / _event_list

- **Category:** Dead Code
- **Severity:** Low
- **Evidence:**
  - `kahin/the_source/architect.py:89-90` — `self._command_list` and `self._event_list` are initialized
  - `kahin/the_source/architect.py:107-108` — They are populated with `sorted(...)` during `load()`
  - No method in SchemaEngine references these attributes for reading
- **Why it's inefficient:** Wastes ~0.01s on startup (sorting 667 + 237 items), and ~1KB memory. Minimal but no benefit.
- **Recommended fix:** Remove the two lines in `load()` and the two field declarations in `__init__`.
- **Tradeoffs / Risks:** None, provided no external code accesses these (grep confirms they don't).
- **Expected impact:** ~0.01s faster startup, ~1KB less memory.
- **Removal Safety:** Safe
- **Reuse Scope:** Local class

### F7: Keyword Index Memory Overhead

- **Category:** Memory
- **Severity:** Medium
- **Impact:** ~2-4MB additional memory on top of ~1MB schema data
- **Evidence:**
  - `kahin/the_source/architect.py:163-181` — `_build_keyword_index()` creates an inverted index mapping every word from every command/event description to a dict entry
  - ~900 items (667 commands + 237 events) × ~200 unique words each ≈ 70,000-100,000 index entries
  - Each entry is a `dict[str, str]` with `type`, `name`, `description` keys
  - Each Python dict has ~240 bytes overhead

- **Why it's inefficient:** The keyword index is built at startup and duplicates all descriptions. Combined with Levenshtein scan on every `find_concept` call, it's heavy for a rare operation. A lighter-weight TF-IDF or `fuzzywuzzy` process-based approach could be leaner.
- **Recommended fix:** Consider using `sqlite3` FTS5 with in-memory database for full-text search instead of Python dict index. Or accept the memory cost since `find_concept` is cold.
- **Tradeoffs / Risks:** SQLite FTS5 adds a dependency but significantly reduces memory and improves query speed for multi-word queries.
- **Expected impact:** ~50-70% memory reduction for search index.
- **Removal Safety:** Needs Verification (schema queries must be functionally identical)
- **Reuse Scope:** Module (the_source)

### F8: Levenshtein Distance on Every fuzzy_find_command Call

- **Category:** Algorithm / CPU
- **Severity:** Low
- **Impact:** ~1-5ms per call for large domains
- **Evidence:**
  - `kahin/the_source/architect.py:405-413` — `_fuzzy_find_command()` computes Levenshtein distance against every command in a domain
  - `kahin/the_source/architect.py:298-300` — Same for `validate_command()` typo detection
  - A domain like `Network` has ~50-100 commands, so worst-case 100 Levenshtein distance calculations per call
- **Recommended fix:** Use a BK-tree or n-gram index for fuzzy search if this becomes a hot path. For current cold-path usage, this is acceptable.
- **Tradeoffs / Risks:** Premature optimization risk. Current usage is infrequent (only when validation fails).
- **Expected impact:** Not worth changing now, but worth measuring if usage grows.
- **Removal Safety:** Needs Verification
- **Reuse Scope:** Local function

### F9: `kahin_get_session` Bypasses `_safe_cdp`

- **Category:** Reliability / Code Reuse
- **Severity:** Low
- **Impact:** Inconsistent error handling
- **Evidence:**
  - `kahin/oracle.py:303` — Uses `_current_engine.send_cdp()` directly instead of `_safe_cdp()`
  - All other engine tools use `_safe_cdp()` for error wrapping
- **Why it's inefficient:** `kahin_get_session` has its own try/except, but doesn't use the shared `_safe_cdp` wrapper. If `_safe_cdp` gains additional error handling or logging, this tool will miss it.
- **Recommended fix:** Refactor `kahin_get_session` to use `_safe_cdp` for the first call, then parse the result locally. Or move the target-info parsing logic into `_safe_cdp`.
- **Tradeoffs / Risks:** The function parses the CDP result (target list) rather than just returning it, so `_safe_cdp` can't be used as a drop-in replacement without some refactoring.
- **Expected impact:** Consistent error handling.
- **Removal Safety:** Safe with minor refactor
- **Reuse Scope:** Module (oracle.py)

### F10: Mirage Uses Hardcoded Headless Display Env

- **Category:** Reliability
- **Severity:** Low
- **Impact:** Fails on systems without DISPLAY=:99
- **Evidence:**
  - `kahin/the_twins/mirage.py:45` — `"DISPLAY": env.get("DISPLAY", ":99")`
  - Default `:99` assumes Xvfb or similar is running
- **Why it's inefficient:** If `:99` isn't available, the headless browser won't start even though Chromium supports `--headless=new` which doesn't need X11.
- **Recommended fix:** Remove the DISPLAY env override when running headless. `--headless=new` doesn't need X11.
- **Tradeoffs / Risks:** Non-headless (headed) mode still needs a display.
- **Expected impact:** More reliable on different systems.
- **Removal Safety:** Safe if headless — `--headless=new` doesn't require X11.
- **Reuse Scope:** Local file

### F11: Polling Without Exponential Backoff in _wait_for_page_ws

- **Category:** I/O / Network
- **Severity:** Low
- **Impact:** ~50 HTTP requests per startup (15 seconds / 0.3s interval)
- **Evidence:**
  - `kahin/the_twins/shadow.py:59` — `while time.time() < deadline: ... await asyncio.sleep(0.3)`
  - `kahin/the_twins/mirage.py:73` — Same pattern
  - Fixed 0.3s/0.5s interval with no backoff
- **Why it's inefficient:** Early polls (first 0.5-1s) are guaranteed to fail since Chrome hasn't started yet. Backoff would reduce failed requests.
- **Recommended fix:** Use exponential backoff: start at 0.1s, multiply by 1.5, cap at 1s.
- **Tradeoffs / Risks:** Slightly more complex code. Startup time might increase by ~0.1s in worst case.
- **Expected impact:** Reduces HTTP requests from ~50 to ~10-15 for typical startup.
- **Removal Safety:** Safe
- **Reuse Scope:** Both engines (would be shared if F2 is addressed)

### F12: Event History Duplicate Key Check is O(n) Per Event

- **Category:** Algorithm
- **Severity:** Low
- **Impact:** Each event callback is O(1), but `_on_network_event` filters by prefix
- **Evidence:**
  - All event callbacks are sequential and O(1) per event
  - No duplicates issue here — actually the implementation is fine for the volume
- **Recommendation:** No action needed for current volume. If events exceed 1000/s, consider batching.

---

## 3) Quick Wins (Do First)

| # | Fix | Est. Time | Impact | Confidence |
|---|---|---|---|---|
| 1 | Replace `dict = {}` with `None` in `_auto_learn` and `kahin_execute_cdp` | 2 min | Bug fix (critical) | High |
| 2 | Remove duplicate `find_chrome` import in `mirage.py` | 30 sec | Clean lint | High |
| 3 | Move in-function imports (`urlparse`, `httpx`, `base64`) to top level | 5 min | Code quality | High |
| 4 | Remove unused `_command_list` / `_event_list` in `SchemaEngine` | 2 min | Cleanup | High |
| 5 | Use `deque(maxlen=10000)` for event buffers | 5 min | Prevents OOM | High |

## 4) Deeper Optimizations (Do Next)

| # | Fix | Est. Time | Impact | Risk |
|---|---|---|---|---|
| 6 | Extract `BaseCdpEngine` to eliminate duplicated engine code | 2-3 hours | 50% code reduction | Medium (verification needed) |
| 7 | Add exponential backoff to `_wait_for_page_ws` polling | 15 min | Reduces startup HTTP noise | Low |
| 8 | Switch keyword index to SQLite FTS5 or lighter structure | 1-2 hours | 50-70% memory reduction | Medium |
| 9 | Fix `kahin_get_session` to use `_safe_cdp` | 10 min | Consistent error handling | Low |
| 10 | Remove DISPLAY env default from Mirage headless start | 5 min | Cross-system reliability | Low |

## 5) Validation Plan

### For Quick Wins 1-5 (low risk):
- Run `pytest tests/` before and after — all 43 tests must pass
- Manual test: call `kahin_execute_cdp` twice with different params, verify second call doesn't see first call's params
- Verify lint: `pyright kahin/` shows no new errors

### For Deeper Optimizations 6-10 (higher risk):
- **Engine extraction (F2):** Run E2E test suite (`test_e2e.py` — 5 tests with real Chromium) before and after. Verify both Obscura and Mirage start/navigate/screenshot work identically.
- **Keyword index (F7):** Profile memory with `tracemalloc` before and after. Benchmark `find_concept` query time for same queries.
- **Backoff (F7):** Count HTTP requests to `127.0.0.1:port/json` during startup with `tcpdump` or logging.

### Metrics to compare before/after:
| Metric | How to Measure |
|---|---|
| Memory (schema + index) | `tracemalloc` snapshot at startup |
| Cold start latency | `time python3 -c "from kahin.oracle import schema; schema.load()"` |
| Event buffer memory | `sys.getsizeof(_network_requests)` after 10K events |
| Levenshtein query time | `timeit` for `find_concept("navigation")` repeated 1000x |

### Edge case tests to add:
- `kahin_execute_cdp` called twice with different params — verify no param leakage
- Engine stop called twice — verify no crash
- `_network_requests` at cap — verify oldest entries are dropped
- `find_concept` with empty/unicode/malicious input — verify no crash

## 6) Optimized Code / Patch

### F1 Fix: Mutable default arguments

```python
# kahin/oracle.py:28 — Change from:
async def _auto_learn(domain: str, command: str, params: dict = {}) -> None:
# To:
async def _auto_learn(domain: str, command: str, params: dict[str, Any] | None = None) -> None:
    p = params or {}
    fate.learn(domain, command, p, context=ctx)

# kahin/oracle.py:283 — Change from:
async def kahin_execute_cdp(domain: str, command: str, parameters: dict = {}) -> str:
# To:
async def kahin_execute_cdp(domain: str, command: str, parameters: dict[str, Any] | None = None) -> str:
    return await _safe_cdp(domain, command, parameters or {})
```

### F5 Fix: Move imports to top level

```python
# Add to kahin/oracle.py top:
from urllib.parse import urlparse

# Remove from line 31
```

### F4 Fix: Bounded event buffers

```python
# kahin/_state.py — Change from:
from typing import Any
from collections import deque

_current_event_log: deque[dict[str, Any]] = deque(maxlen=5000)
_network_requests: deque[dict[str, Any]] = deque(maxlen=10000)
_console_messages: deque[dict[str, Any]] = deque(maxlen=5000)
```

**Note:** The callers use `.append()` which works identically for `deque`. When the deque reaches maxlen, old items are automatically discarded.

### F7 Fix: Exponential backoff in polling

```python
# Replace fixed sleep with:
delay = 0.1
while time.time() < deadline:
    ...
    await asyncio.sleep(delay)
    delay = min(delay * 1.5, 1.0)  # cap at 1s
```
