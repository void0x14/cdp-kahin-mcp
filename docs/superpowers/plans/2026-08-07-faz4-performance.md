# Faz 4 — Performans (Opsiyonel) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Zig sidecar'ın tek-in-flight darboğazını kaldırmak, start latency'sini düşürmek ve tool bazlı performans metriklerini görünür yapmak.

**Architecture:** Zig tarafında `send()`'in blocking pump'ını (driver.zig:259-286) ana poll loop'una entegre non-blocking state machine'e çevirmek (ipc_main.zig:168-216); Python tarafında `kahin_engine_stats` metrik tool'u (healer tracker verisi). Öncelik sıralaması: ölç → düzelt → tekrar öl.

## Global Constraints

- Faz 1-3 kısıtları aynen geçer
- Zig değişiklikleri mevcut IPC sözleşmesini (JSONL, id-matching, event forwarding) BOZAMAZ — `tests/test_phantom.py` + `tests/test_mirage_ipc.py` bu sözleşmenin regresyon kapısıdır
- Zig 0.16.x zorunlu; `scripts/build-sidecar.sh` ile build; `camoufox-harness/vendor/bin/kahin-sidecar` güncellenir
- Benchmark: mevcut `tests/perf/main.zig` (build.zig `perf` target) kullanılır

---

### Task 1: Baseline benchmark — concurrency darboğazını ölç

**Files:** none (measurement; add `tests/perf/concurrency.md` notes)

- [ ] **Step 1: Write a concurrency probe**

Extend `tests/perf/main.zig` (or add `tests/perf/concurrent.zig`) to fire N=20 `Runtime.evaluate` calls in parallel against a `data:` page and record wall time; run the same N serially. Baseline ratio = serial/parallel (expect ~1 today, i.e. no parallelism).

- [ ] **Step 2: Measure**

```bash
zig build perf -Doptimize=ReleaseSafe && ./zig-out/bin/perf
```

Record the numbers in `camoufox-harness/tests/perf/concurrency.md`. This is the gate reference for Task 2.

- [ ] **Step 3: Commit**

```bash
git add camoufox-harness/tests/perf/ tests/perf/
git commit -m "perf: baseline concurrency benchmark"
```

---

### Task 2: Non-blocking sidecar request handling

**Files:**
- Modify: `camoufox-harness/core/driver.zig` (send → send_async + pending map consumption in the main loop)
- Modify: `camoufox-harness/core/ipc_main.zig` (main loop: poll stdin + browser fd; process incoming replies into pending futures; drain events as today)
- Test: `tests/test_phantom.py`, `tests/test_mirage_ipc.py` (must stay green), `tests/perf` (ratio must improve)

**Interfaces:**
- Invariant: request ids, sessions, event forwarding, `Kahin.eventDropped`, timeouts, death detection — ALL unchanged
- New: multiple pending requests can be in flight; replies resolve their owner by id; stdin processing never blocks on a browser reply

- [ ] **Step 1: Write the failing perf test (expect ratio > 1.5)**

`tests/perf/concurrency.md` gate: parallel wall time must be < 2/3 of serial for N=20 evaluates after this task.

- [ ] **Step 2: Refactor `send()`**

Move the pump loop out of `send()`: `send()` registers `{id, caller_ctx, done}` in `pending` and returns immediately; the main loop (ipc_main.zig) owns `poll()` on both fds; when a browser reply for a pending id arrives, the loop resolves that caller's future (`done` flag + raw) and drains/forwards events as today. `send()` keeps its deadline — on timeout it pops the pending entry and signals the caller error (same `-32603`-style behavior; verify against `test_phantom.py` expectations). Keep the single-threaded model: NO threads, NO locks — the state machine replaces blocking.

- [ ] **Step 3: Rebuild + run protocol tests**

```bash
scripts/build-sidecar.sh && cp camoufox-harness/vendor/bin/kahin-sidecar kahin/_vendor/kahin-sidecar
uv run pytest tests/test_phantom.py tests/test_mirage_ipc.py -v
```

Expected: all PASS (contract preserved).

- [ ] **Step 4: Re-run benchmark + e2e**

```bash
zig build perf -Doptimize=ReleaseSafe && ./zig-out/bin/perf
KAHIN_REQUIRE_REAL_E2E=1 uv run pytest tests/test_e2e_*.py -v
```

Expected: ratio > 1.5; all e2e green.

- [ ] **Step 5: Commit**

```bash
git add camoufox-harness/core/driver.zig camoufox-harness/core/ipc_main.zig kahin/_vendor/kahin-sidecar camoufox-harness/vendor/bin/kahin-sidecar tests/perf/
git commit -m "perf(sidecar): non-blocking concurrent request handling"
```

---

### Task 3: Startup pre-warm + `kahin_engine_stats`

**Files:**
- Modify: `kahin/the_twins/mirage.py` (profile pre-warm: keep a cached profile dir per identity; reuse on start)
- Modify: `kahin/tools/engine.py` (`kahin_engine_stats` tool)
- Test: `tests/test_e2e_stealth.py` or new `tests/test_e2e_perf.py` (start time delta + stats shape)

**Interfaces:**
- Produces: `kahin_engine_stats()` → `{"engine", "uptime_s", "tool_calls": N, "tool_errors": N, "top_slow": [{"tool", "count", "avg_ms", "max_ms"}], "last_error": ...}` (healer ErrorTracker verisinden; `_healer.py` tracker'a tool-duration kaydı eklenir — `safe()` içinde `time.monotonic()` ölçümü)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_e2e_perf.py — starts engine, calls a couple tools, asserts stats shape
```

- [ ] **Step 2: Implement**

Healer `safe()` context manager'ına duration kaydı ekle (başlangıç/bitiş monotonic, tracker dict: `{tool: {"calls", "errors", "total_ms", "max_ms"}}`); `kahin_engine_stats` tool'u bunu döker; profile pre-warm: identity cache dizini `~/.cache/kahin/profiles/<hash(identity)>` — start'ta mevcut profile varsa kopyala, yoksa üret.

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/test_e2e_perf.py -v && uv run ruff check .
git add kahin/_healer.py kahin/the_twins/mirage.py kahin/tools/engine.py tests/test_e2e_perf.py
git commit -m "perf: engine stats tool + profile pre-warm"
```

---

### Task 4: Faz 4 acceptance

- [ ] **Step 1: Full suite**

```bash
KAHIN_REQUIRE_REAL_E2E=1 uv run pytest tests/ -v && uv run ruff check . && uv run pyright
```

- [ ] **Step 2: Document**

`AGENTS.md` + `CHANGELOG.md`: engine_stats tool, concurrency notes, pre-warm.

```bash
git add AGENTS.md CHANGELOG.md
git commit -m "docs: Faz 4 performance surface"
```
