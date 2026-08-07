//! Faz 4 Task 1 baseline: N=20 Runtime.evaluate concurrency probe against
//! the REAL sidecar path — the `kahin-sidecar` binary over its
//! newline-delimited JSON stdio protocol (the exact artifact Faz 4 Task 2
//! refactors; the driver module the perf harness links is the same code).
//!
//! Serial phase:  20 evaluate round trips, one request -> one reply wait,
//!                wall time + per-request latency stats.
//! Parallel phase: all 20 requests pipelined into a single pipe write
//!                (atomic), then the 20 replies are drained and verified.
//!
//! Today the sidecar processes one request at a time (blocking send), so
//! both walls are ~equal (ratio ~1). Task 2 makes stdin processing
//! non-blocking, so the parallel wall must drop; the Task 2 gate is
//! parallel < 2/3 of serial (ratio > 1.5). Threads are deliberately NOT
//! used: the driver's shared reader/router are single-threaded by design,
//! so thread-level "parallel" calls would race the frame buffer and the
//! pending map — the pipelined write is the only honest in-flight probe.
//!
//! Pipelining constraint (observed, not refactored): the sidecar's main
//! loop only drains its stdin line buffer on a poll wakeup, so lines that
//! arrived in the same read as a request already being processed are
//! stranded until the pipe HUP's. The parallel phase therefore closes its
//! stdin after the pipelined write (batch-then-EOF); the sidecar drains
//! the remaining buffered lines on EOF and exits 0. The same probe binary
//! reruns unchanged after Task 2.

const std = @import("std");
const linux = std.os.linux;
const Allocator = std.mem.Allocator;

pub const eval_n: usize = 20;
const request_timeout_ms: i32 = 30_000;
const parallel_deadline_ms: i64 = 60_000;
const line_max: usize = 16 * 1024 * 1024;
const chunk_size: usize = 64 * 1024;

const data_page = "data:text/html,<h1>bench</h1>";
const evaluate_params = "{\"expression\":\"1+1\"}";

const id_health: u32 = 1;
const id_new_page: u32 = 2;
const id_serial_base: u32 = 101;
const id_parallel_base: u32 = 201;

/// Spawned sidecar: our write end of its stdin + our read end of its
/// stdout (stderr inherited so boot failures are visible).
const Spawned = struct {
    pid: linux.pid_t,
    in_fd: i32,
    out_fd: i32,
};

const Probe = struct {
    a: Allocator,
    pid: linux.pid_t,
    in_fd: i32,
    reader: LineReader,

    fn init(a: Allocator, sidecar: []const u8, exe: []const u8) !Probe {
        const spawned = try spawnSidecar(a, sidecar, exe);
        return .{ .a = a, .pid = spawned.pid, .in_fd = spawned.in_fd, .reader = LineReader.init(spawned.out_fd) };
    }

    fn deinit(self: *Probe) void {
        if (self.in_fd >= 0) _ = linux.close(self.in_fd);
        self.reader.deinit(self.a);
    }

    /// Write one newline-terminated request line.
    fn writeRequest(self: *Probe, id: u32, method: []const u8, params: []const u8) !void {
        const line = try std.fmt.allocPrint(self.a, "{{\"id\":{d},\"method\":\"{s}\",\"params\":{s}}}\n", .{ id, method, params });
        defer self.a.free(line);
        try writeAll(self.in_fd, line);
    }

    /// Close our end of the sidecar's stdin (batch-then-EOF drain; see the
    /// module comment). Idempotent.
    fn closeStdin(self: *Probe) void {
        if (self.in_fd >= 0) {
            _ = linux.close(self.in_fd);
            self.in_fd = -1;
        }
    }

    /// Read lines until the reply with `expected` id arrives (events and
    /// other replies are skipped). The parsed Value is arena-owned.
    fn waitReply(self: *Probe, arena: Allocator, expected: u32, deadline_ms: i64) !std.json.Value {
        while (true) {
            const now = nowMs();
            if (now >= deadline_ms) return error.WaitTimeout;
            const rem: i32 = @intCast(@min(deadline_ms - now, request_timeout_ms));
            const line = (try self.reader.readLine(self.a, rem)) orelse return error.SidecarClosed;
            defer self.a.free(line);
            const owned = try arena.dupe(u8, line);
            const v = std.json.parseFromSliceLeaky(std.json.Value, arena, owned, .{}) catch return error.BadReplyJson;
            if (v != .object) return error.BadReplyJson;
            if (v.object.get("id")) |idv| {
                if (idv != .integer) continue;
                const id: u32 = std.math.cast(u32, idv.integer) orelse continue;
                if (id == expected) return v;
            }
        }
    }
};

/// Incremental newline reader on a blocking fd (poll + read, tail retained).
const LineReader = struct {
    fd: i32,
    buf: std.array_list.Aligned(u8, null) = .empty,

    fn init(fd: i32) LineReader {
        return .{ .fd = fd };
    }

    fn deinit(self: *LineReader, a: Allocator) void {
        if (self.fd >= 0) _ = linux.close(self.fd);
        self.buf.deinit(a);
    }

    /// One newline-terminated line (terminator stripped, owned copy).
    /// null -> clean EOF; error.Timeout -> nothing within timeout_ms.
    fn readLine(self: *LineReader, a: Allocator, timeout_ms: i32) !?[]u8 {
        while (true) {
            if (std.mem.indexOfScalar(u8, self.buf.items, '\n')) |idx| {
                const line = try a.dupe(u8, self.buf.items[0..idx]);
                const rest = self.buf.items[idx + 1 ..];
                std.mem.copyForwards(u8, self.buf.items[0..rest.len], rest);
                self.buf.shrinkRetainingCapacity(rest.len);
                return line;
            }
            if (self.buf.items.len > line_max) return error.LineTooLong;

            var pfd = [_]linux.pollfd{.{ .fd = self.fd, .events = linux.POLL.IN, .revents = 0 }};
            const rc = linux.poll(&pfd, pfd.len, timeout_ms);
            switch (linux.errno(rc)) {
                .SUCCESS => {},
                .INTR => continue,
                else => return error.PollFailed,
            }
            if (pfd[0].revents == 0) return error.Timeout;

            var chunk: [chunk_size]u8 = undefined;
            const n = linux.read(self.fd, &chunk, chunk.len);
            switch (linux.errno(n)) {
                .SUCCESS => {
                    if (n == 0) {
                        if (self.buf.items.len == 0) return null;
                        return error.UnexpectedEndOfStream;
                    }
                    try self.buf.appendSlice(a, chunk[0..n]);
                },
                .INTR => continue,
                else => return error.ReadFailed,
            }
        }
    }
};

/// Full probe: spawn the sidecar, boot a data page, measure serial vs
/// pipelined N=20 evaluate walls, shut down cleanly, print the section.
pub fn runProbe(a: Allocator, sidecar_path: []const u8, exe: []const u8) !void {
    ignoreSigpipe();

    var arena_state = std.heap.ArenaAllocator.init(a);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    var probe = try Probe.init(a, sidecar_path, exe);
    defer probe.deinit();
    std.debug.print("probe: sidecar spawned (pid {d})\n", .{probe.pid});

    // Boot handshake: Browser.health proves the sidecar answers on stdio.
    try probe.writeRequest(id_health, "Browser.health", "{}");
    try verifyHealth(try probe.waitReply(arena, id_health, nowMs() + request_timeout_ms));
    std.debug.print("probe: Browser.health ok\n", .{});

    // One data page; page-scoped calls route to the current page.
    try probe.writeRequest(id_new_page, "Browser.newPage", "{\"url\":\"" ++ data_page ++ "\"}");
    try requireOk(try probe.waitReply(arena, id_new_page, nowMs() + request_timeout_ms));
    std.debug.print("probe: Browser.newPage ok\n", .{});

    // ---- serial: one request -> one reply, per-request latency -----------
    var lats: std.array_list.Aligned(i64, null) = .empty;
    defer lats.deinit(a);
    const serial_t0 = nowUs();
    for (0..eval_n) |i| {
        const id: u32 = id_serial_base + @as(u32, @intCast(i));
        const t0 = nowUs();
        try probe.writeRequest(id, "Runtime.evaluate", evaluate_params);
        const v = try probe.waitReply(arena, id, nowMs() + request_timeout_ms);
        try verifyEvaluate(v);
        const t1 = nowUs();
        try lats.append(a, t1 - t0);
    }
    const serial_t1 = nowUs();
    std.debug.print("probe: serial {d} evaluates done\n", .{eval_n});

    // ---- parallel: all 20 requests pipelined in one write, then drain ----
    var pipelined: std.array_list.Aligned(u8, null) = .empty;
    defer pipelined.deinit(a);
    for (0..eval_n) |i| {
        const id: u32 = id_parallel_base + @as(u32, @intCast(i));
        const line = try std.fmt.allocPrint(a, "{{\"id\":{d},\"method\":\"Runtime.evaluate\",\"params\":{s}}}\n", .{ id, evaluate_params });
        defer a.free(line);
        try pipelined.appendSlice(a, line);
    }
    const par_t0 = nowUs();
    try writeAll(probe.in_fd, pipelined.items);
    probe.closeStdin();
    const par_deadline = nowMs() + parallel_deadline_ms;
    var received = [_]bool{false} ** eval_n;
    var count: usize = 0;
    while (count < eval_n) {
        const now = nowMs();
        if (now >= par_deadline) return error.ParallelTimeout;
        const rem: i32 = @intCast(@min(par_deadline - now, request_timeout_ms));
        const line = (try probe.reader.readLine(a, rem)) orelse return error.SidecarClosed;
        defer a.free(line);
        const owned = try arena.dupe(u8, line);
        const v = std.json.parseFromSliceLeaky(std.json.Value, arena, owned, .{}) catch return error.BadReplyJson;
        if (v != .object) return error.BadReplyJson;
        if (v.object.get("id")) |idv| {
            if (idv != .integer) continue;
            const id: u32 = std.math.cast(u32, idv.integer) orelse continue;
            if (id >= id_parallel_base and id < id_parallel_base + eval_n) {
                const idx: usize = @intCast(id - id_parallel_base);
                if (!received[idx]) {
                    try verifyEvaluate(v);
                    received[idx] = true;
                    count += 1;
                }
            }
        }
    }
    const par_t1 = nowUs();
    std.debug.print("probe: parallel {d} evaluates done\n", .{eval_n});

    // ---- clean shutdown: the sidecar exits on stdin EOF after draining --
    const exit_code = try waitPid(probe.pid);
    if (exit_code != 0) return error.SidecarBadExit;
    std.debug.print("probe: sidecar drained and exited {d}\n", .{exit_code});

    // ---- report ----------------------------------------------------------
    const serial_wall_us = serial_t1 - serial_t0;
    const par_wall_us = par_t1 - par_t0;
    const stats = try latsStats(a, lats.items);
    std.debug.print("=== Runtime.evaluate concurrency (N={d}, real sidecar JSONL path) ===\n", .{eval_n});
    std.debug.print("  data page: {s}\n", .{data_page});
    std.debug.print("  serial wall:   {d:.2} ms  (n={d} min={d:.2} mean={d:.2} p50={d:.2} p95={d:.2} max={d:.2})\n", .{
        usToMs(serial_wall_us), stats.n, stats.min_ms, stats.mean_ms, stats.p50_ms, stats.p95_ms, stats.max_ms,
    });
    std.debug.print("  parallel wall: {d:.2} ms  ({d} requests pipelined in one write, stdin closed after)\n", .{ usToMs(par_wall_us), eval_n });
    const ratio = usToMs(serial_wall_us) / usToMs(par_wall_us);
    std.debug.print("  ratio serial/parallel: {d:.3}\n", .{ratio});
}

// === reply verification ===

/// null when the reply is well-formed and carries no error.
fn replyError(v: std.json.Value) ?[]const u8 {
    if (v != .object) return "reply is not an object";
    if (v.object.get("error") != null) return "sidecar error";
    if (v.object.get("result") == null) return "missing result";
    return null;
}

fn requireOk(v: std.json.Value) !void {
    if (replyError(v)) |_| return error.SidecarError;
}

fn verifyHealth(v: std.json.Value) !void {
    if (replyError(v)) |_| return error.HealthFailed;
    const res = v.object.get("result").?;
    if (res != .object) return error.HealthFailed;
    const alive = res.object.get("alive") orelse return error.HealthFailed;
    if (alive != .bool or !alive.bool) return error.HealthFailed;
}

/// Runtime.evaluate reply shape: {"id":N,"result":{"result":{"type":"number","value":2}}}.
fn verifyEvaluate(v: std.json.Value) !void {
    if (replyError(v)) |_| return error.SidecarError;
    const res = v.object.get("result").?;
    if (res != .object) return error.BadEvaluateReply;
    const inner = res.object.get("result") orelse return error.BadEvaluateReply;
    if (inner != .object) return error.BadEvaluateReply;
    const val = inner.object.get("value") orelse return error.BadEvaluateReply;
    if (val == .integer and val.integer == 2) return;
    return error.EvaluateMismatch;
}

// === stats ===

const Lats = struct {
    n: usize,
    min_ms: f64,
    max_ms: f64,
    mean_ms: f64,
    p50_ms: f64,
    p95_ms: f64,
};

fn latsStats(a: Allocator, lats: []const i64) !Lats {
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

fn nowMs() i64 {
    return @divTrunc(nowUs(), std.time.us_per_ms);
}

fn writeAll(fd: i32, bytes: []const u8) !void {
    var off: usize = 0;
    while (off < bytes.len) {
        const n = linux.write(fd, bytes[off..].ptr, bytes.len - off);
        switch (linux.errno(n)) {
            .SUCCESS => {
                if (n == 0) return error.WriteZero;
                off += n;
            },
            .INTR => continue,
            .PIPE => return error.BrokenPipe,
            else => return error.WriteFailed,
        }
    }
}

fn waitPid(pid: linux.pid_t) !u8 {
    var status: u32 = 0;
    while (true) {
        const rc = linux.waitpid(pid, @ptrCast(&status), 0);
        switch (linux.errno(rc)) {
            .SUCCESS => break,
            .INTR => continue,
            else => return error.WaitFailed,
        }
    }
    if (linux.W.IFEXITED(status)) return linux.W.EXITSTATUS(status);
    return error.ChildSignaled;
}

fn ignoreSigpipe() void {
    const set: linux.sigset_t = @splat(0);
    var act = linux.Sigaction{
        .handler = .{ .handler = linux.SIG.IGN },
        .mask = set,
        .flags = 0,
    };
    _ = linux.sigaction(linux.SIG.PIPE, &act, null);
}

// === sidecar spawn (fork + exec; stdin/stdout pipes) ===

fn spawnSidecar(a: Allocator, sidecar_path: []const u8, exe: []const u8) !Spawned {
    var in_p: [2]i32 = undefined; // child stdin: we write in_p[1], child reads in_p[0]
    var out_p: [2]i32 = undefined; // child stdout: child writes out_p[1], we read out_p[0]
    var err_p: [2]i32 = undefined; // exec-error report, CLOEXEC'd away on success
    if (linux.errno(linux.pipe2(&in_p, .{})) != .SUCCESS) return error.PipeFailed;
    errdefer {
        _ = linux.close(in_p[0]);
        _ = linux.close(in_p[1]);
    }
    if (linux.errno(linux.pipe2(&out_p, .{})) != .SUCCESS) return error.PipeFailed;
    errdefer {
        _ = linux.close(out_p[0]);
        _ = linux.close(out_p[1]);
    }
    if (linux.errno(linux.pipe2(&err_p, .{ .CLOEXEC = true })) != .SUCCESS) return error.PipeFailed;
    errdefer {
        _ = linux.close(err_p[0]);
        _ = linux.close(err_p[1]);
    }

    // argv: <sidecar> <firefox-binary> (profile omitted -> isolated default)
    const argv = [_][]const u8{ sidecar_path, exe };

    var arena_state = std.heap.ArenaAllocator.init(a);
    defer arena_state.deinit();
    const aa = arena_state.allocator();
    const argv_z = try aa.allocSentinel(?[*:0]const u8, argv.len, null);
    for (argv, 0..) |arg, i| argv_z[i] = (try aa.dupeZ(u8, arg)).ptr;
    const envp = try buildEnvp(aa);

    const pid: linux.pid_t = fork: {
        const rc = linux.fork();
        switch (linux.errno(rc)) {
            .SUCCESS => break :fork @intCast(rc),
            .AGAIN, .NOMEM => return error.SystemResources,
            else => return error.ForkFailed,
        }
    };

    if (pid == 0) {
        const err_w = err_p[1];
        // stderr is inherited so sidecar boot failures stay visible.
        dup2OrExit(in_p[0], 0, err_w);
        dup2OrExit(out_p[1], 1, err_w);
        if (err_w >= 3) {
            _ = linux.close_range(3, err_w - 1, .{ .UNSHARE = false, .CLOEXEC = false });
            _ = linux.close_range(err_w + 1, ~@as(i32, 0), .{ .UNSHARE = false, .CLOEXEC = false });
        } else {
            _ = linux.close_range(3, ~@as(i32, 0), .{ .UNSHARE = false, .CLOEXEC = false });
        }
        const rc = linux.execve(argv_z[0].?, argv_z.ptr, envp);
        const e = linux.errno(rc);
        const v: u32 = @intFromEnum(e);
        _ = linux.write(err_w, @ptrCast(&v), @sizeOf(u32));
        linux.exit_group(126);
    }

    _ = linux.close(in_p[0]);
    _ = linux.close(out_p[1]);
    _ = linux.close(err_p[1]);

    var errbuf: [4]u8 = undefined;
    const n = linux.read(err_p[0], &errbuf, errbuf.len);
    _ = linux.close(err_p[0]);
    if (linux.errno(n) == .SUCCESS and n == 4) {
        var status: u32 = 0;
        _ = linux.waitpid(pid, @ptrCast(&status), 0);
        _ = linux.close(in_p[1]);
        _ = linux.close(out_p[0]);
        return error.SidecarExecFailed;
    }
    if (linux.errno(n) != .SUCCESS) {
        _ = linux.close(in_p[1]);
        _ = linux.close(out_p[0]);
        return error.ExecStatusPipeFailed;
    }
    return .{ .pid = pid, .in_fd = in_p[1], .out_fd = out_p[0] };
}

fn dup2OrExit(old: i32, new: i32, err_w: i32) void {
    const rc = linux.dup2(old, new);
    if (linux.errno(rc) == .SUCCESS) return;
    const v: u32 = @intFromEnum(linux.errno(rc));
    _ = linux.write(err_w, @ptrCast(&v), @sizeOf(u32));
    linux.exit_group(126);
}

/// Environment block for execve, copied from /proc/self/environ (the Zig
/// runtime without libc does not expose the parent environment).
fn buildEnvp(a: Allocator) ![*:null]const ?[*:0]const u8 {
    const data = try readProcSelfEnviron(a);
    var count: usize = 0;
    for (data) |b| {
        if (b == 0) count += 1;
    }
    var envp = try a.allocSentinel(?[*:0]const u8, count, null);
    var i: usize = 0;
    var start: usize = 0;
    for (data, 0..) |b, idx| {
        if (b == 0) {
            envp[i] = @ptrCast(data[start..].ptr);
            i += 1;
            start = idx + 1;
        }
    }
    return envp;
}

fn readProcSelfEnviron(a: Allocator) ![]u8 {
    const rc = linux.open("/proc/self/environ", .{ .ACCMODE = .RDONLY }, 0);
    switch (linux.errno(rc)) {
        .SUCCESS => {},
        else => return error.NoEnviron,
    }
    const fd: i32 = @intCast(rc);
    defer _ = linux.close(fd);

    const max: usize = 1 << 20;
    const buf = try a.alloc(u8, max);
    var len: usize = 0;
    while (len < max) {
        const n = linux.read(fd, buf[len..].ptr, max - len);
        switch (linux.errno(n)) {
            .SUCCESS => {
                if (n == 0) break;
                len += n;
            },
            .INTR => continue,
            else => return error.NoEnviron,
        }
    }
    return buf[0..len];
}
