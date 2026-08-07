# Faz 4 Task 1 baseline — Runtime.evaluate concurrency (N=20)

Measured 2026-08-08 on the developer machine, real Camoufox, pinned
Zig 0.16.0, `ReleaseSafe` perf binary. This is the gate reference for
Faz 4 Task 2 (non-blocking sidecar request handling): Task 2 must rerun
the **same** probe and show the parallel wall shrinking further.

## Environment

| Item | Value |
|---|---|
| kernel | 6.12.69-2-cachyos-lts-lto |
| cpu / mem | 16 cores / 15651 MB (harness `printMachine`) |
| zig | `~/.local/opt/zig-x86_64-linux-0.16.0/zig` (0.16.0, repo pin) |
| camoufox | `~/.cache/camoufox/browsers/official/152.0.4-beta.28-924f3109/camoufox-bin` |
| sidecar | built from `camoufox-harness/core` with the pinned zig (byte-identical to `camoufox-harness/vendor/bin/kahin-sidecar`) |

## Commands

```bash
# build perf + core (ReleaseSafe), run unit tests
~/.local/opt/zig-x86_64-linux-0.16.0/zig build -Doptimize=ReleaseSafe
~/.local/opt/zig-x86_64-linux-0.16.0/zig build test

# build the sidecar (temporary copy; vendored binary untouched)
~/.local/opt/zig-x86_64-linux-0.16.0/zig build-exe \
  --dep driver -Mroot=ipc_main.zig -Mdriver=driver.zig \
  -O ReleaseSafe -femit-bin=/tmp/kahin-sidecar-perf

# full benchmark incl. the concurrency section (exit 0 = all sections ran)
./zig-out/bin/perf ~/.cache/camoufox/browsers/official/152.0.4-beta.28-924f3109/camoufox-bin \
  --sidecar /tmp/kahin-sidecar-perf
```

## Probe

`camoufox-harness/core/tests/perf/concurrent.zig` drives the **real
sidecar** (`kahin-sidecar <camoufox-bin>`) over its newline-delimited JSON
stdio protocol:

- boot `Browser.health`, open one data page (`data:text/html,<h1>bench</h1>`)
- **serial**: 20 × `Runtime.evaluate("1+1")`, one request → one reply wait;
  wall time + per-request latency stats; every reply verified (`value: 2`)
- **parallel**: all 20 requests pipelined into a single pipe write, then the
  20 replies drained and verified; wall time from write to 20th reply
- ratio = serial wall / parallel wall

Threads are deliberately not used: the driver's shared reader/router are
single-threaded by design, so thread-level "parallel" calls race the frame
buffer and the pending map. The pipelined write is the only honest
in-flight probe, and it exercises the exact artifact Task 2 refactors.

**Pipelining constraint (observed, not refactored):** the sidecar's main
loop only drains its stdin line buffer on a poll wakeup, so lines that
arrived in the same read as the request being processed are stranded until
the pipe HUP's. The parallel phase therefore closes its stdin after the
pipelined write (batch-then-EOF); the sidecar drains the buffered lines on
EOF and exits 0. The same probe binary reruns unchanged after Task 2.

## Results

### Full harness run (deliverable, `PERF PASS`)

```
=== Runtime.evaluate concurrency (N=20, real sidecar JSONL path) ===
  data page: data:text/html,<h1>bench</h1>
  serial wall:   74.78 ms  (n=20 min=2.68 mean=3.74 p50=2.87 p95=9.15 max=12.25)
  parallel wall: 56.89 ms  (20 requests pipelined in one write, stdin closed after)
  ratio serial/parallel: 1.314
```

### Repeat runs (standalone probe, same binary/browser)

| run | serial wall | parallel wall | ratio |
|---|---|---|---|
| 1 | 113.33 ms | 61.66 ms | 1.838 |
| 2 | 97.61 ms | 57.44 ms | 1.699 |
| 3 | 89.67 ms | 73.44 ms | 1.221 |
| 4 | 83.13 ms | 56.49 ms | 1.472 |
| 5 (full harness) | 74.78 ms | 56.89 ms | 1.314 |

Baseline range: ratio **1.22 – 1.84** (mean ≈ 1.5). Per-request serial
latency is dominated by the request/reply round trip through the sidecar
(mean ≈ 3.7 – 5.7 ms; the browser-side evaluate itself is ≈ 0.3 ms per the
direct-driver section of the same harness).

## Task 2 results (non-blocking sidecar, `perf(sidecar)` commit)

Same probe binary, same machine/browser, same pinned Zig 0.16.0. The
sidecar main loop now owns polling on both fds; `send()` is replaced by
`sendAsync` wire calls registered in a task registry, so the browser has
all 20 evaluates in flight at once and stdin processing never blocks on a
browser reply.

### Full harness run (deliverable, `PERF PASS`)

```
=== Runtime.evaluate concurrency (N=20, real sidecar JSONL path) ===
  data page: data:text/html,<h1>bench</h1>
  serial wall:   28.56 ms  (n=20 min=0.67 mean=1.43 p50=0.81 p95=3.96 max=8.14)
  parallel wall: 8.71 ms  (20 requests pipelined in one write, stdin closed after)
  ratio serial/parallel: 3.281
```

vs. the Task 1 baseline: serial 74.78 ms -> 28.56 ms, parallel 56.89 ms ->
8.71 ms (**6.5x drop in the parallel wall — the true in-flight signal**),
ratio 1.314 -> **3.281** (plan gate: ratio > 1.5; parallel < 2/3 of serial).

### Repeat runs (standalone probe, Task 2 binary)

| run | serial wall | parallel wall | ratio |
|---|---|---|---|
| 1 | 28.82 ms | 10.99 ms | 2.622 |
| 2 | 31.07 ms | 6.79 ms | 4.575 |
| 3 | 23.54 ms | 5.75 ms | 4.092 |
| 4 (full harness) | 28.56 ms | 8.71 ms | 3.281 |

Task 2 range: ratio **2.62 – 4.58** — no overlap with the Task 1 baseline
range (1.22 – 1.84). Per-request serial latency dropped with it (mean
≈ 1.2 – 1.6 ms vs ≈ 3.7 – 5.7 ms): the per-request loop overhead is gone
because the sidecar no longer round-trips through a blocking send per
request.

## Interpretation (Task 2)

The batch is now genuinely concurrent server-side: the loop drains the
pipelined stdin lines into 20 pending wire calls before any reply arrives,
the browser processes them back-to-back, and the replies resolve their
owners by id as they stream in. The batch-then-EOF probe constraint no
longer reflects a sidecar limitation (the loop also drains pipelined input
without EOF); the probe binary was deliberately left unchanged so the
baseline comparison is byte-identical.

## Interpretation

Today's sidecar processes requests strictly one at a time (blocking
`send`), yet the ratio is **not** the plan's naive "~1.0": the serial phase
pays the per-request client round trip, poll wakeup and sidecar loop
overhead, while the pipelined batch amortizes those. The wall times are
still dominated by the sidecar's serial processing — the browser never has
more than one request in flight.

Task 2 gate (plan): parallel < 2/3 of serial (ratio > 1.5). Because the
baseline already approaches that threshold through overhead amortization,
Task 2 must compare against **this recorded baseline** (same probe, same
binary) and demonstrate a further drop in the parallel wall — the true
signal of non-blocking in-flight handling — not merely ratio > 1.5.
