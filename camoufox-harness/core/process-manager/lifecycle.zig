//! Process Manager — instance lifecycle: spawn / stop / reap, health-check,
//! kill fallback, and the resource limit (max instances, default 1).
//!
//! Spawn contract (Juggler): the browser is exec'd with `--juggler-pipe`
//! (fd 3 read / fd 4 write, `\x00`-framed JSON) and `-profile <dir>` — the
//! profile dir MUST exist before spawn (Faz 1 rule). Closing both pipe ends
//! makes the browser exit cleanly with code 0; a wedged child gets SIGKILL
//! after a grace period. Health = main process alive (not zombie) AND the
//! pipe write end still open.

const std = @import("std");
const linux = std.os.linux;
const Allocator = std.mem.Allocator;

const pipe = @import("../src/transport/pipe.zig");
const binary = @import("binary.zig");
const profile_mod = @import("profile.zig");

/// Resource limit default: one browser instance (configurable per manager).
pub const default_max_instances: usize = 1;
/// Grace period between pipe-close and SIGKILL during stop().
pub const stop_timeout_ms: i32 = 15_000;
/// How often stop() re-checks waitpid while waiting for the child to exit.
const reap_poll_ms: i32 = 5;

pub const Health = enum { healthy, dead };

/// One managed Camoufox process.
pub const Instance = struct {
    allocator: Allocator,
    child: pipe.Spawned,
    /// Owned profile dir path (created before spawn).
    profile_path: []u8,
    /// Owned resolved binary path; null when the caller supplied the exe.
    owned_bin: ?[]u8 = null,
    /// Pin-consistency warning from binary resolution (owned).
    warning: ?[]u8 = null,
    verbose: bool,
    state: State = .running,

    pub const State = enum { running, dead };

    /// Spawn Camoufox with Juggler args. `exe` null or "" resolves via
    /// binary.resolve (env / cache); `profile` null creates an isolated
    /// default profile dir; an explicit profile is created if missing.
    pub fn spawn(allocator: Allocator, exe: ?[]const u8, profile: ?[]const u8, verbose: bool) !*Instance {
        var owned_bin: ?[]u8 = null;
        var warning: ?[]u8 = null;
        errdefer {
            if (owned_bin) |b| allocator.free(b);
            if (warning) |w| allocator.free(w);
        }
        var bin: []const u8 = undefined;
        if (exe) |e| {
            if (e.len > 0) {
                bin = e;
            } else {
                const res = try binary.resolve(allocator);
                owned_bin = res.path;
                warning = res.warning;
                bin = owned_bin.?;
            }
        } else {
            const res = try binary.resolve(allocator);
            owned_bin = res.path;
            warning = res.warning;
            bin = owned_bin.?;
        }

        const profile_path = if (profile) |p| blk: {
            if (p.len == 0) break :blk try profile_mod.defaultProfile(allocator);
            try profile_mod.ensureDir(p);
            break :blk try allocator.dupe(u8, p);
        } else try profile_mod.defaultProfile(allocator);
        errdefer allocator.free(profile_path);

        const inst = try allocator.create(Instance);
        errdefer allocator.destroy(inst);
        inst.* = .{
            .allocator = allocator,
            .child = .{ .pid = -1, .read_fd = -1, .write_fd = -1 },
            .profile_path = profile_path,
            .owned_bin = owned_bin,
            .warning = warning,
            .verbose = verbose,
        };

        const argv = [_][]const u8{
            bin,
            "-juggler-pipe",
            "-profile",
            profile_path,
            "-no-remote",
            "-headless",
        };
        inst.child = try pipe.spawn(allocator, &argv);
        errdefer {
            // Spawn-error path: closing the fds makes the browser exit, then
            // reap it (pipe EOF exit rule).
            pipe.closeFds(&inst.child);
            if (inst.child.pid > 0) _ = pipe.wait(&inst.child) catch {};
        }

        if (verbose) {
            std.debug.print("process-manager: spawned pid={d} exe={s} profile={s}\n", .{ inst.child.pid, bin, profile_path });
            if (inst.warning) |w| std.debug.print("process-manager: warning: {s}\n", .{w});
        }
        return inst;
    }

    /// Raw spawn for tests / non-Camoufox children: argv as-is, no binary
    /// resolution, no juggler args. `profile_path` null -> isolated default.
    pub fn spawnArgv(allocator: Allocator, argv: []const []const u8, profile_path: ?[]const u8, verbose: bool) !*Instance {
        const profile_owned = if (profile_path) |p| blk: {
            try profile_mod.ensureDir(p);
            break :blk try allocator.dupe(u8, p);
        } else try profile_mod.defaultProfile(allocator);
        errdefer allocator.free(profile_owned);

        const inst = try allocator.create(Instance);
        errdefer allocator.destroy(inst);
        inst.* = .{
            .allocator = allocator,
            .child = .{ .pid = -1, .read_fd = -1, .write_fd = -1 },
            .profile_path = profile_owned,
            .verbose = verbose,
        };
        inst.child = try pipe.spawn(allocator, argv);
        errdefer {
            pipe.closeFds(&inst.child);
            if (inst.child.pid > 0) _ = pipe.wait(&inst.child) catch {};
        }
        if (verbose) {
            std.debug.print("process-manager: spawned pid={d} argv[0]={s} profile={s}\n", .{ inst.child.pid, argv[0], profile_owned });
        }
        return inst;
    }

    /// Free owned memory only. Does NOT close fds / reap — stop() owns the
    /// child (contract: call stop() first, or leak the child).
    pub fn deinit(self: *Instance, allocator: Allocator) void {
        allocator.free(self.profile_path);
        if (self.owned_bin) |b| allocator.free(b);
        if (self.warning) |w| allocator.free(w);
        allocator.destroy(self);
    }

    /// Close the pipes (browser exits 0 on EOF), reap with a deadline, and
    /// SIGKILL a wedged child. Returns the exit code; error.ChildSignaled
    /// when the child died on a signal; error.AlreadyStopped when not
    /// running. Idempotent-safe: a second call errors instead of double
    /// reaping.
    pub fn stop(self: *Instance, timeout_ms: i32) !u8 {
        if (self.state != .running) return error.AlreadyStopped;
        pipe.closeFds(&self.child);
        var status: u32 = 0;
        const deadline = nowMs() + timeout_ms;
        var signaled = false;
        while (true) {
            const rc = linux.waitpid(self.child.pid, &status, linux.W.NOHANG);
            switch (linux.errno(rc)) {
                .SUCCESS => {},
                .INTR => continue,
                else => return error.WaitFailed,
            }
            if (rc == 0) {
                if (nowMs() >= deadline) {
                    // Kill fallback: pipe EOF was ignored (wedged child).
                    _ = linux.kill(self.child.pid, .KILL);
                    const rc2 = linux.waitpid(self.child.pid, &status, 0);
                    if (linux.errno(rc2) != .SUCCESS) return error.WaitFailed;
                    signaled = true;
                } else {
                    sleepMs(reap_poll_ms);
                    continue;
                }
            }
            break;
        }
        self.state = .dead;
        if (!signaled and linux.W.IFEXITED(status)) return linux.W.EXITSTATUS(status);
        return error.ChildSignaled;
    }

    /// Health = pipe write end still open (no HUP on our read end) AND the
    /// main process alive (not gone, not a zombie).
    pub fn health(self: *Instance) Health {
        if (self.state != .running) return .dead;
        // Pipe first: HUP means every child-side write end is closed — the
        // browser is gone (kill -9 closes fds on process death).
        if (self.child.read_fd >= 0) {
            var pfd = [_]linux.pollfd{.{ .fd = self.child.read_fd, .events = linux.POLL.IN, .revents = 0 }};
            const rc = linux.poll(&pfd, pfd.len, 0);
            if (linux.errno(rc) == .SUCCESS and (pfd[0].revents & (linux.POLL.HUP | linux.POLL.ERR | linux.POLL.NVAL)) != 0) {
                return .dead;
            }
        }
        // Process: /proc/<pid>/stat missing (exited) or state 'Z' (zombie,
        // not yet reaped — kill(pid,0) would still succeed) -> dead.
        const st = procState(self.child.pid) orelse return .dead;
        if (st == 'Z') return .dead;
        return .healthy;
    }
};

/// Registry with a resource limit and a double-instance guard. Owns nothing
/// beyond the registry: instances must be stop()'d by the caller before
/// deinit (deinit frees the structs; running children would leak).
pub const Manager = struct {
    allocator: Allocator,
    max_instances: usize,
    instances: std.array_list.Aligned(*Instance, null) = .empty,

    /// `max_instances` defaults to 1 when 0 is passed (safety).
    pub fn init(allocator: Allocator, max_instances: usize) Manager {
        return .{ .allocator = allocator, .max_instances = @max(1, max_instances) };
    }

    /// Spawn under the limit; error.LimitReached when at cap (double-spawn
    /// guard for limit 1).
    pub fn spawn(self: *Manager, exe: ?[]const u8, profile: ?[]const u8, verbose: bool) !*Instance {
        if (self.instances.items.len >= self.max_instances) return error.LimitReached;
        const inst = try Instance.spawn(self.allocator, exe, profile, verbose);
        errdefer inst.deinit(self.allocator);
        try self.instances.append(self.allocator, inst);
        return inst;
    }

    /// Drop a (stopped) instance from the registry and free it.
    pub fn remove(self: *Manager, inst: *Instance) void {
        for (self.instances.items, 0..) |it, i| {
            if (it == inst) {
                _ = self.instances.orderedRemove(i);
                inst.deinit(self.allocator);
                return;
            }
        }
    }

    /// Stop every registered instance (best effort; first error after all
    /// attempts). Instances stay registered until remove()/deinit().
    pub fn stopAll(self: *Manager, timeout_ms: i32) !void {
        var first_err: ?anyerror = null;
        for (self.instances.items) |inst| {
            inst.stop(timeout_ms) catch |e| {
                if (first_err == null) first_err = e;
            };
        }
        if (first_err) |e| return e;
    }

    /// Free the registry and any still-registered instance structs. Does NOT
    /// stop children (caller owns stop()).
    pub fn deinit(self: *Manager) void {
        for (self.instances.items) |inst| inst.deinit(self.allocator);
        self.instances.deinit(self.allocator);
        // Manager is a value type; instances own their heap memory.
    }
};

/// Monotonic ms (raw syscall; std.time.milliTimestamp is gone in 0.16).
fn nowMs() i64 {
    var ts: linux.timespec = undefined;
    _ = linux.clock_gettime(linux.CLOCK.MONOTONIC, &ts);
    return @as(i64, @intCast(ts.sec)) * std.time.ms_per_s + @divTrunc(@as(i64, @intCast(ts.nsec)), std.time.ns_per_ms);
}

fn sleepMs(ms: i32) void {
    var ts = linux.timespec{
        .sec = @intCast(@divTrunc(ms, 1000)),
        .nsec = @intCast(@rem(ms, 1000) * std.time.ns_per_ms),
    };
    _ = linux.nanosleep(&ts, null);
}

/// First char of the process state from /proc/<pid>/stat, or null when the
/// process is gone (ENOENT). Handles comm names containing ')'.
fn procState(pid: linux.pid_t) ?u8 {
    var buf: [64]u8 = undefined;
    const path = std.fmt.bufPrintZ(&buf, "/proc/{d}/stat", .{pid}) catch return null;
    const fd = linux.open(path.ptr, .{ .ACCMODE = .RDONLY, .CLOEXEC = true }, 0);
    switch (linux.errno(fd)) {
        .SUCCESS => {},
        else => return null,
    }
    defer _ = linux.close(@intCast(fd));

    var data: [512]u8 = undefined;
    var len: usize = 0;
    while (len < data.len) {
        const n = linux.read(@intCast(fd), data[len..].ptr, data.len - len);
        switch (linux.errno(n)) {
            .SUCCESS => {
                if (n == 0) break;
                len += n;
            },
            .INTR => continue,
            else => break,
        }
    }
    const last = std.mem.lastIndexOfScalar(u8, data[0..len], ')') orelse return null;
    if (last + 2 >= len) return null;
    return data[last + 2];
}

const testing = std.testing;

test "lifecycle: instance spawn + reap returns exit code" {
    // /bin/true: exit 0. /bin/false: exit 1.
    const t = try Instance.spawnArgv(testing.allocator, &.{ "/bin/true" }, null, false);
    defer t.deinit(testing.allocator);
    try testing.expectEqual(@as(u8, 0), try t.stop(10_000));

    const f = try Instance.spawnArgv(testing.allocator, &.{ "/bin/false" }, null, false);
    defer f.deinit(testing.allocator);
    try testing.expectEqual(@as(u8, 1), try f.stop(10_000));
}

test "lifecycle: health transitions healthy -> dead after SIGKILL" {
    var inst = try Instance.spawnArgv(testing.allocator, &.{ "/bin/sleep", "30" }, null, false);
    defer inst.deinit(testing.allocator);
    try testing.expectEqual(Health.healthy, inst.health());

    _ = linux.kill(inst.child.pid, .KILL);
    const deadline = nowMs() + 5_000;
    while (inst.health() == .healthy) {
        if (nowMs() >= deadline) return error.TestTimeout;
        sleepMs(10);
    }
    try testing.expectEqual(Health.dead, inst.health());
    // Reap: the SIGKILLed child reports ChildSignaled.
    try testing.expectError(error.ChildSignaled, inst.stop(10_000));
}

test "lifecycle: stop falls back to SIGKILL for a wedged child" {
    // /bin/sleep ignores pipe EOF — the kill fallback must fire.
    var inst = try Instance.spawnArgv(testing.allocator, &.{ "/bin/sleep", "30" }, null, false);
    defer inst.deinit(testing.allocator);
    try testing.expectError(error.ChildSignaled, inst.stop(300));
    try testing.expectEqual(Health.dead, inst.health());
    try testing.expectError(error.AlreadyStopped, inst.stop(10_000));
}

test "lifecycle: manager limit violation (max 2, third spawn rejected)" {
    var m = Manager.init(testing.allocator, 2);
    defer m.deinit();
    const inst_a = try m.spawn("/bin/true", null, false);
    const inst_b = try m.spawn("/bin/true", null, false);
    try testing.expectEqual(@as(usize, 2), m.instances.items.len);
    try testing.expectError(error.LimitReached, m.spawn("/bin/true", null, false));
    try testing.expectEqual(@as(usize, 2), m.instances.items.len);
    _ = try inst_a.stop(10_000);
    _ = try inst_b.stop(10_000);
}

test "lifecycle: double-instance guard (max 1 rejects second spawn)" {
    var m = Manager.init(testing.allocator, 1);
    defer m.deinit();
    const inst_a = try m.spawn("/bin/true", null, false);
    try testing.expectError(error.LimitReached, m.spawn("/bin/true", null, false));
    _ = try inst_a.stop(10_000);
}

test "lifecycle: per-instance profile isolation" {
    var m = Manager.init(testing.allocator, 2);
    defer m.deinit();
    const inst_a = try m.spawn("/bin/true", null, false);
    const inst_b = try m.spawn("/bin/true", null, false);
    defer {
        profile_rmdir(inst_a.profile_path);
        profile_rmdir(inst_b.profile_path);
    }
    try testing.expect(!std.mem.eql(u8, inst_a.profile_path, inst_b.profile_path));
    try testing.expect(dirExists(inst_a.profile_path));
    try testing.expect(dirExists(inst_b.profile_path));
    _ = try inst_a.stop(10_000);
    _ = try inst_b.stop(10_000);
}

fn dirExists(path: []const u8) bool {
    const z = testing.allocator.dupeZ(u8, path) catch return false;
    defer testing.allocator.free(z);
    const fd = linux.open(z.ptr, .{ .ACCMODE = .RDONLY, .DIRECTORY = true, .CLOEXEC = true }, 0);
    switch (linux.errno(fd)) {
        .SUCCESS => {
            _ = linux.close(@intCast(fd));
            return true;
        },
        else => return false,
    }
}

fn profile_rmdir(path: []const u8) void {
    const z = testing.allocator.dupeZ(u8, path) catch return;
    defer testing.allocator.free(z);
    _ = linux.rmdir(z.ptr);
}
