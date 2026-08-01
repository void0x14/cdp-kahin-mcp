//! Faz 7 perf benchmark: Juggler + Camoufox round-trip latency per domain,
//! resident memory (idle/loaded), cold start, N-context degradation.
//!
//! Usage: perf <camoufox-bin> [profile-dir]
//!
//! Workload (methodology: .superpowers/sdd/task-7-report.md):
//!   - cold start: spawn -> Browser.enable response (fresh instance per
//!     sample; the first sample doubles as the workload driver)
//!   - Runtime.evaluate("1+1") x200 (10 warmup)
//!   - Page.navigate (data URL, load-gated) x30
//!   - Page.dispatchKeyEvent (CDP Input.dispatchKeyEvent equivalent) x100
//!   - Network interception DECISIONS: resume x100 / fulfill x20 / abort
//!     x20 — the decision round-trip only; request arrival is async and is
//!     waited for, not timed
//!   - RSS (VmRSS from /proc/<pid>/status): idle (browser up, page
//!     about:blank) + loaded (DOM-heavy data page)
//!   - N-context degradation: 1/2/4/8 contexts, 20 evaluates each,
//!     sequential round-robin, per level
//!
//! All latencies in ms (measured in microseconds). Exit 0 only when every
//! section ran; any failure prints PERF-FAIL and exits 1.

const std = @import("std");
const linux = std.os.linux;

const driver = @import("driver");

const eval_samples: usize = 200;
const eval_warmup: usize = 10;
const nav_samples: usize = 30;
const key_samples: usize = 100;
const resume_samples: usize = 100;
const fulfill_samples: usize = 20;
const abort_samples: usize = 20;
const cold_start_samples: usize = 3;
const ctx_levels = [_]usize{ 1, 2, 4, 8 };
const degrade_eval_per_ctx: usize = 20;
const cmd_timeout_ms: i32 = 30_000;
const eval_timeout_ms: i32 = 15_000;
/// Interception workload target. Requests are intercepted BEFORE hitting
/// the network, but Juggler does not emit isIntercepted for targets that
/// fail connection setup immediately (observed: port 1 never registered).
/// startInjectServer() owns a live listener on this port, so every request
/// reaches the interception point, then gets resumed/fulfilled/aborted.
const inject_port: u16 = 8333;

pub fn main(args: std.process.Init.Minimal) u8 {
    run(args) catch |err| {
        std.debug.print("PERF-FAIL: {s}\n", .{@errorName(err)});
        return 1;
    };
    return 0;
}

const Stats = struct {
    n: usize,
    min_ms: f64,
    max_ms: f64,
    mean_ms: f64,
    p50_ms: f64,
    p95_ms: f64,
    p99_ms: f64,

    fn print(self: Stats) void {
        std.debug.print("  n={d} min={d:.2} max={d:.2} mean={d:.2} p50={d:.2} p95={d:.2} p99={d:.2} (ms)\n", .{
            self.n, self.min_ms, self.max_ms, self.mean_ms,
            self.p50_ms, self.p95_ms, self.p99_ms,
        });
    }
};

fn run(args: std.process.Init.Minimal) !void {
    const argv = args.args.vector;
    if (argv.len < 2) {
        std.debug.print("usage: perf <camoufox-bin> [profile-dir]\n", .{});
        return error.MissingArgs;
    }
    const exe = std.mem.sliceTo(argv[1], 0);
    const profile = if (argv.len >= 3) std.mem.sliceTo(argv[2], 0) else null;

    var gpa_state = std.heap.DebugAllocator(.{}).init;
    defer _ = gpa_state.deinit();
    const gpa = gpa_state.allocator();
    var arena_state = std.heap.ArenaAllocator.init(gpa);
    defer arena_state.deinit();
    const a = arena_state.allocator();

    printMachine(a);

    // ---- cold start (spawn -> Browser.enable response) ------------------
    var cold_lats: std.array_list.Aligned(i64, null) = .empty;
    defer cold_lats.deinit(a);
    var d: ?driver.Driver = null;
    var i: usize = 0;
    while (i < cold_start_samples) : (i += 1) {
        const t0 = nowUs();
        var drv = try driver.Driver.start(a, exe, profile, false);
        const t1 = nowUs();
        try cold_lats.append(a, t1 - t0);
        if (i == 0) {
            d = drv; // keep as the workload driver
        } else {
            const code = try drv.stop();
            if (code != 0) return error.ColdStartBadExit;
        }
    }
    defer d.?.deinit();
    const drv: *driver.Driver = &d.?;
    std.debug.print("=== cold start (spawn -> Browser.enable) ===\n", .{});
    for (cold_lats.items, 0..) |us, s| std.debug.print("  sample {d}: {d:.2} ms\n", .{ s + 1, usToMs(us) });
    (try summarize(a, cold_lats.items)).print();

    // ---- RSS idle (browser up, no page) ---------------------------------
    const pid = drv.instance.?.child.pid;
    const rss_idle_kb = try vmRssKb(pid, a);

    // ---- page + loaded state --------------------------------------------
    const ctx = try drv.newContext(cmd_timeout_ms);
    defer a.free(ctx);
    const tgt = try drv.newPage(ctx, null, cmd_timeout_ms);
    defer a.free(tgt);
    const nav0 = try drv.navigate(tgt, loadedPageUrl, cmd_timeout_ms);
    a.free(nav0);
    const rss_loaded_kb = try vmRssKb(pid, a);

    std.debug.print("=== resident memory (VmRSS) ===\n", .{});
    std.debug.print("  idle   (browser up, no page):  {d} MB\n", .{rss_idle_kb / 1024});
    std.debug.print("  loaded (DOM-heavy data page):  {d} MB\n", .{rss_loaded_kb / 1024});
    std.debug.print("  delta:                         {d} MB\n", .{rss_loaded_kb / 1024 - rss_idle_kb / 1024});

    // ---- Runtime.evaluate round trip -------------------------------------
    for (0..eval_warmup) |_| {
        var ev = try drv.evaluate(tgt, "1+1", eval_timeout_ms);
        ev.deinit(a);
    }
    var eval_lats: std.array_list.Aligned(i64, null) = .empty;
    defer eval_lats.deinit(a);
    for (0..eval_samples) |_| {
        const t0 = nowUs();
        var ev = try drv.evaluate(tgt, "1+1", eval_timeout_ms);
        const t1 = nowUs();
        defer ev.deinit(a);
        if (ev.exception_text != null or !std.mem.eql(u8, ev.value_json, "2")) return error.EvaluateMismatch;
        try eval_lats.append(a, t1 - t0);
    }
    std.debug.print("=== Runtime.evaluate round-trip ===\n", .{});
    (try summarize(a, eval_lats.items)).print();

    // ---- Page.navigate round trip (load-gated) ---------------------------
    var nav_lats: std.array_list.Aligned(i64, null) = .empty;
    defer nav_lats.deinit(a);
    for (0..nav_samples) |s| {
        const url = try std.fmt.allocPrint(a, "data:text/html,<h1>bench{d}</h1>", .{s});
        defer a.free(url);
        const t0 = nowUs();
        const nav = try drv.navigate(tgt, url, cmd_timeout_ms);
        const t1 = nowUs();
        a.free(nav);
        try nav_lats.append(a, t1 - t0);
    }
    std.debug.print("=== Page.navigate round-trip (load-gated) ===\n", .{});
    (try summarize(a, nav_lats.items)).print();

    // ---- Page.dispatchKeyEvent (Input equivalent) round trip --------------
    var key_lats: std.array_list.Aligned(i64, null) = .empty;
    defer key_lats.deinit(a);
    for (0..key_samples) |_| {
        const t0 = nowUs();
        try drv.dispatchKeyEvent(tgt, "keyDown", "Enter", 13, 0, "Enter", false, null, cmd_timeout_ms);
        const t1 = nowUs();
        try key_lats.append(a, t1 - t0);
    }
    std.debug.print("=== Page.dispatchKeyEvent round-trip ===\n", .{});
    (try summarize(a, key_lats.items)).print();

    // ---- Network interception decisions -----------------------------------
    // The benchmark's own listener supplies live request targets (§ above).
    _ = std.Thread.spawn(.{}, startInjectServer, .{}) catch return error.InjectServerSpawn;
    try drv.setInterception(tgt, true, cmd_timeout_ms);

    var resume_lats: std.array_list.Aligned(i64, null) = .empty;
    defer resume_lats.deinit(a);
    for (0..resume_samples) |s| {
        try injectPending(a, drv, tgt, s);
        const rid = try firstPendingRid(a, drv);
        defer a.free(rid);
        const t0 = nowUs();
        try drv.continueInterceptedRequest(rid, null, null, null, null, cmd_timeout_ms);
        const t1 = nowUs();
        try resume_lats.append(a, t1 - t0);
    }
    std.debug.print("=== Network.resumeInterceptedRequest decision ===\n", .{});
    (try summarize(a, resume_lats.items)).print();

    var fulfill_lats: std.array_list.Aligned(i64, null) = .empty;
    defer fulfill_lats.deinit(a);
    for (0..fulfill_samples) |s| {
        try injectPending(a, drv, tgt, resume_samples + s);
        const rid = try firstPendingRid(a, drv);
        defer a.free(rid);
        const t0 = nowUs();
        try drv.fulfillInterceptedRequest(rid, 200, "OK", &[_]driver.browser.Header{}, null, cmd_timeout_ms);
        const t1 = nowUs();
        try fulfill_lats.append(a, t1 - t0);
    }
    std.debug.print("=== Network.fulfillInterceptedRequest decision ===\n", .{});
    (try summarize(a, fulfill_lats.items)).print();

    var abort_lats: std.array_list.Aligned(i64, null) = .empty;
    defer abort_lats.deinit(a);
    for (0..abort_samples) |s| {
        try injectPending(a, drv, tgt, resume_samples + fulfill_samples + s);
        const rid = try firstPendingRid(a, drv);
        defer a.free(rid);
        const t0 = nowUs();
        try drv.abortInterceptedRequest(rid, "NS_ERROR_ABORT", cmd_timeout_ms);
        const t1 = nowUs();
        try abort_lats.append(a, t1 - t0);
    }
    std.debug.print("=== Network.abortInterceptedRequest decision ===\n", .{});
    (try summarize(a, abort_lats.items)).print();

    try drv.setInterception(tgt, false, cmd_timeout_ms);

    // ---- N-context degradation --------------------------------------------
    std.debug.print("=== N-context degradation (evaluate round trip) ===\n", .{});
    for (ctx_levels) |n| {
        var ctxs: [8][]u8 = undefined;
        var tgts: [8][]u8 = undefined;
        var live: usize = 0;
        for (0..n) |_| {
            ctxs[live] = try drv.newContext(cmd_timeout_ms);
            tgts[live] = try drv.newPage(ctxs[live], null, cmd_timeout_ms);
            const url = try std.fmt.allocPrint(a, "data:text/html,<h1>ctx{d}</h1>", .{n});
            defer a.free(url);
            const nav = try drv.navigate(tgts[live], url, cmd_timeout_ms);
            a.free(nav);
            live += 1;
        }
        var lats: std.array_list.Aligned(i64, null) = .empty;
        defer lats.deinit(a);
        for (0..degrade_eval_per_ctx) |_| {
            for (0..live) |c| {
                const t0 = nowUs();
                var ev = try drv.evaluate(tgts[c], "1+1", eval_timeout_ms);
                const t1 = nowUs();
                defer ev.deinit(a);
                if (ev.exception_text != null or !std.mem.eql(u8, ev.value_json, "2")) return error.EvaluateMismatch;
                try lats.append(a, t1 - t0);
            }
        }
        std.debug.print("  contexts={d}\n", .{n});
        (try summarize(a, lats.items)).print();
        for (0..live) |c| {
            try drv.removeBrowserContext(ctxs[c], cmd_timeout_ms);
            a.free(ctxs[c]);
            a.free(tgts[c]);
        }
    }

    // ---- clean shutdown ----------------------------------------------------
    const code = try drv.stop();
    std.debug.print("clean stop: browser exit code {d}\n", .{code});
    if (code != 0) return error.BadBrowserExit;
    std.debug.print("PERF PASS\n", .{});
}

/// Benchmark-owned HTTP listener on 127.0.0.1:inject_port. Interception
/// needs a live target; the benchmark supplies it, no external server
/// required. Runs on a spawned thread; process exit reaps it.
fn startInjectServer() void {
    const fd = linux.socket(linux.AF.INET, linux.SOCK.STREAM, 0);
    if (linux.errno(fd) != .SUCCESS) return;
    defer _ = linux.close(@intCast(fd));
    const one: u32 = 1;
    _ = linux.setsockopt(@intCast(fd), linux.SOL.SOCKET, linux.SO.REUSEADDR, @ptrCast(&one), @sizeOf(u32));
    var addr: linux.sockaddr.in = .{
        .family = linux.AF.INET,
        .port = std.mem.nativeToBig(u16, inject_port),
        .addr = std.mem.nativeToBig(u32, 0x7F000001),
    };
    if (linux.errno(linux.bind(@intCast(fd), @ptrCast(&addr), @sizeOf(linux.sockaddr.in))) != .SUCCESS) {
        std.debug.print("PERF: inject server bind failed on 127.0.0.1:{d}\n", .{inject_port});
        return;
    }
    if (linux.errno(linux.listen(@intCast(fd), 16)) != .SUCCESS) return;
    const resp = "HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
    var client: linux.sockaddr.in = undefined;
    var clen: linux.socklen_t = @sizeOf(linux.sockaddr.in);
    while (true) {
        const c = linux.accept(@intCast(fd), @ptrCast(&client), &clen);
        if (linux.errno(c) == .SUCCESS) {
            _ = linux.write(@intCast(c), resp.ptr, resp.len);
            _ = linux.close(@intCast(c));
        }
    }
}

/// data URL used for the "loaded" RSS measurement (moderate DOM).
const loadedPageUrl = "data:text/html,<h1>loaded</h1>" ++
    "<table><script>for(var r=0;r<200;r++){var tr=document.createElement('tr');" ++
    "for(var c=0;c<8;c++){var td=document.createElement('td');td.textContent=r+'-'+c;tr.appendChild(td);}" ++
    "document.querySelector('table').appendChild(tr);}</script></table>";

/// Inject a script element whose request lands in the interception registry.
fn injectPending(a: std.mem.Allocator, d: *driver.Driver, tgt: []const u8, n: usize) !void {
    const expr = try std.fmt.allocPrint(
        a,
        "var s=document.createElement('script');s.src='http://127.0.0.1:{d}/b{d}.js';document.body.appendChild(s)",
        .{ inject_port, n },
    );
    defer a.free(expr);
    var ev = try d.evaluate(tgt, expr, eval_timeout_ms);
    ev.deinit(a);
    try d.waitForIntercepted(15_000);
}

/// First requestId of the pending registry JSON.
fn firstPendingRid(a: std.mem.Allocator, d: *driver.Driver) ![]u8 {
    const pend = try d.listInterceptedRequests();
    defer a.free(pend);
    const parsed = try std.json.parseFromSlice(std.json.Value, a, pend, .{});
    defer parsed.deinit();
    const v = parsed.value;
    if (v != .array or v.array.items.len == 0) return error.NoPendingRequest;
    const first = v.array.items[0];
    if (first != .object) return error.NoPendingRequest;
    const rid = first.object.get("requestId") orelse return error.NoPendingRequest;
    if (rid != .string) return error.NoPendingRequest;
    return a.dupe(u8, rid.string);
}

/// p50/p95/p99 (nearest-rank) + min/max/mean over an owned sorted copy.
/// OOM propagates: a silent zeroed Stats would masquerade as a real run.
fn summarize(a: std.mem.Allocator, lats: []const i64) !Stats {
    const sorted = try a.dupe(i64, lats);
    defer a.free(sorted);
    std.mem.sort(i64, sorted, {}, std.sort.asc(i64));

    var sum: i64 = 0;
    for (sorted) |us| sum += us;
    return .{
        .n = sorted.len,
        .min_ms = usToMs(sorted[0]),
        .max_ms = usToMs(sorted[sorted.len - 1]),
        .mean_ms = usToMs(@divTrunc(sum, @as(i64, @intCast(sorted.len)))),
        .p50_ms = usToMs(percentile(sorted, 50)),
        .p95_ms = usToMs(percentile(sorted, 95)),
        .p99_ms = usToMs(percentile(sorted, 99)),
    };
}

/// Nearest-rank percentile: ceil(q*n/100)-1, clamped.
fn percentile(sorted: []const i64, q: u8) i64 {
    if (sorted.len == 0) return 0;
    const n: usize = sorted.len;
    const idx = @min((n * @as(usize, q) + 99) / 100, n) - 1;
    return sorted[idx];
}

fn usToMs(us: i64) f64 {
    return @as(f64, @floatFromInt(us)) / 1000.0;
}

/// Monotonic microseconds (raw syscall; std.time removed in 0.16).
fn nowUs() i64 {
    var ts: linux.timespec = undefined;
    _ = linux.clock_gettime(linux.CLOCK.MONOTONIC, &ts);
    return @as(i64, @intCast(ts.sec)) * std.time.us_per_s +
        @divTrunc(@as(i64, @intCast(ts.nsec)), std.time.ns_per_us);
}

/// VmRSS in kB from /proc/<pid>/status.
fn vmRssKb(pid: linux.pid_t, a: std.mem.Allocator) !u64 {
    var buf: [128]u8 = undefined;
    const path = try std.fmt.bufPrintZ(&buf, "/proc/{d}/status", .{pid});
    const content = try readSmallFile(a, path);
    defer a.free(content);
    var lines = std.mem.splitScalar(u8, content, '\n');
    while (lines.next()) |line| {
        if (std.mem.startsWith(u8, line, "VmRSS:")) {
            var it = std.mem.tokenizeAny(u8, line["VmRSS:".len..], " \t");
            const kb = it.next() orelse return error.BadStatus;
            return std.fmt.parseInt(u64, kb, 10) catch error.BadStatus;
        }
    }
    return error.BadStatus;
}

/// Whole small file read (raw syscalls, bounded by allocation).
fn readSmallFile(a: std.mem.Allocator, path_z: [:0]const u8) ![]u8 {
    const fd = linux.open(path_z.ptr, .{ .ACCMODE = .RDONLY, .CLOEXEC = true }, 0);
    switch (linux.errno(fd)) {
        .SUCCESS => {},
        .NOENT => return error.FileNotFound,
        else => return error.OpenFailed,
    }
    defer _ = linux.close(@intCast(fd));
    var out: std.array_list.Aligned(u8, null) = .empty;
    errdefer out.deinit(a);
    var chunk: [4096]u8 = undefined;
    while (true) {
        const n = linux.read(@intCast(fd), &chunk, chunk.len);
        switch (linux.errno(n)) {
            .SUCCESS => {
                if (n == 0) break;
                try out.appendSlice(a, chunk[0..n]);
            },
            .INTR => continue,
            else => return error.ReadFailed,
        }
    }
    return out.toOwnedSlice(a);
}

/// Machine header: kernel, cpu count, total memory.
fn printMachine(a: std.mem.Allocator) void {
    const osrel = readSmallFile(a, "/proc/sys/kernel/osrelease") catch "";
    defer if (osrel.len > 0) a.free(osrel);
    const meminfo = readSmallFile(a, "/proc/meminfo") catch "";
    defer if (meminfo.len > 0) a.free(meminfo);
    var mem_total_mb: u64 = 0;
    var lines = std.mem.splitScalar(u8, meminfo, '\n');
    while (lines.next()) |line| {
        if (std.mem.startsWith(u8, line, "MemTotal:")) {
            var it = std.mem.tokenizeAny(u8, line["MemTotal:".len..], " \t");
            if (it.next()) |kb| mem_total_mb = std.fmt.parseInt(u64, kb, 10) catch 0;
            break;
        }
    }
    const cpus = std.Thread.getCpuCount() catch 0;
    std.debug.print("=== machine ===\n", .{});
    std.debug.print("  kernel={s} cpus={d} mem_total={d} MB\n", .{
        std.mem.trim(u8, osrel, " \t\r\n"), cpus, mem_total_mb / 1024,
    });
}
