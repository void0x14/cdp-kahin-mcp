//! Raw pipe transport for the Juggler protocol.
//!
//! The browser child sees its command pipe as fd 3 (it reads from us) and its
//! response pipe as fd 4 (it writes to us). We spawn the browser ourselves
//! (fork + execve) so those fds land exactly where Firefox expects them, with
//! stdin/stdout/stderr pointed at /dev/null. Closing our ends of the pipes
//! terminates the browser.
//!
//! All I/O here is blocking, raw-syscall, allocator-free hot path except the
//! per-message copy.

const std = @import("std");
const linux = std.os.linux;
const Allocator = std.mem.Allocator;

/// Child-side fd numbers the browser uses.
pub const child_read_fd: i32 = 3; // parent -> browser (commands)
pub const child_write_fd: i32 = 4; // browser -> parent (responses/events)

/// Guard against unbounded growth on garbage input.
pub const max_message_size: usize = 64 * 1024 * 1024;

const chunk_size: usize = 64 * 1024;

pub const Spawned = struct {
    pid: linux.pid_t,
    /// Parent's read end (browser -> parent). -1 once closed.
    read_fd: i32,
    /// Parent's write end (parent -> browser). -1 once closed.
    write_fd: i32,
};

/// Spawn the browser with `argv` (argv[0] = executable path) on the Juggler
/// pipe. The child inherits the parent environment.
pub fn spawn(allocator: Allocator, argv: []const []const u8) !Spawned {
    if (argv.len == 0 or argv[0].len == 0) return error.InvalidArgs;

    // cmd: parent writes commands (cmd[1]), browser reads them at fd 3 (cmd[0]).
    // resp: browser writes responses at fd 4 (resp[1]), parent reads (resp[0]).
    var cmd: [2]i32 = undefined;
    var resp: [2]i32 = undefined;
    var err_pipe: [2]i32 = undefined; // child -> parent exec-error report, CLOEXEC'd away on success

    if (linux.errno(linux.pipe2(&cmd, .{})) != .SUCCESS) return error.PipeFailed;
    errdefer {
        _ = linux.close(cmd[0]);
        _ = linux.close(cmd[1]);
    }
    if (linux.errno(linux.pipe2(&resp, .{})) != .SUCCESS) return error.PipeFailed;
    errdefer {
        _ = linux.close(resp[0]);
        _ = linux.close(resp[1]);
    }
    if (linux.errno(linux.pipe2(&err_pipe, .{ .CLOEXEC = true })) != .SUCCESS) return error.PipeFailed;
    errdefer {
        _ = linux.close(err_pipe[0]);
        _ = linux.close(err_pipe[1]);
    }

    const devnull = openDevNull() catch return error.NoDevNull;
    errdefer _ = linux.close(devnull);

    // All allocations for execve happen before fork() (fork-safety).
    var arena_state = std.heap.ArenaAllocator.init(allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();

    const argv_z = try a.allocSentinel(?[*:0]const u8, argv.len, null);
    for (argv, 0..) |arg, i| argv_z[i] = (try a.dupeZ(u8, arg)).ptr;
    const envp = try buildEnvp(a);

    const pid: linux.pid_t = fork: {
        const rc = linux.fork();
        switch (linux.errno(rc)) {
            .SUCCESS => break :fork @intCast(rc),
            .AGAIN, .NOMEM => return error.SystemResources,
            else => return error.ForkFailed,
        }
    };

    if (pid == 0) {
        // ---- child ----
        const err_w = err_pipe[1];
        dup2OrExit(cmd[0], child_read_fd, err_w);
        dup2OrExit(resp[1], child_write_fd, err_w);
        dup2OrExit(devnull, 0, err_w);
        dup2OrExit(devnull, 1, err_w);
        dup2OrExit(devnull, 2, err_w);
        // Close everything >= 5 except err_w (kept for exec-error reporting).
        if (err_w >= 5) {
            _ = linux.close_range(5, err_w - 1, .{ .UNSHARE = false, .CLOEXEC = false });
            _ = linux.close_range(err_w + 1, ~@as(i32, 0), .{ .UNSHARE = false, .CLOEXEC = false });
        } else {
            _ = linux.close_range(5, ~@as(i32, 0), .{ .UNSHARE = false, .CLOEXEC = false });
        }
        const rc = linux.execve(argv_z[0].?, argv_z.ptr, envp);
        const e = linux.errno(rc);
        const v: u32 = @intFromEnum(e);
        _ = linux.write(err_w, @ptrCast(&v), @sizeOf(u32));
        linux.exit_group(126);
    }

    // ---- parent ----
    _ = linux.close(cmd[0]);
    _ = linux.close(resp[1]);
    _ = linux.close(err_pipe[1]);
    _ = linux.close(devnull);

    // Wait for the exec outcome: 4 bytes = errno, EOF = exec succeeded.
    var errbuf: [4]u8 = undefined;
    const n = linux.read(err_pipe[0], &errbuf, errbuf.len);
    _ = linux.close(err_pipe[0]);
    if (linux.errno(n) == .SUCCESS and n == 4) {
        // Child failed before/during exec; reap it and report the cause.
        var status: u32 = 0;
        _ = linux.waitpid(pid, &status, 0);
        _ = linux.close(cmd[1]);
        _ = linux.close(resp[0]);
        return mapExecErrno(@enumFromInt(std.mem.readInt(u32, &errbuf, .little)));
    }
    if (linux.errno(n) != .SUCCESS) {
        _ = linux.close(cmd[1]);
        _ = linux.close(resp[0]);
        return error.ExecStatusPipeFailed;
    }

    return .{ .pid = pid, .read_fd = resp[0], .write_fd = cmd[1] };
}

fn mapExecErrno(e: linux.E) error{
    FileNotFound,
    AccessDenied,
    ExecFormatError,
    IsDir,
    FileBusy,
    ExecFailed,
} {
    return switch (e) {
        .NOENT => error.FileNotFound,
        .ACCES => error.AccessDenied,
        .NOEXEC => error.ExecFormatError,
        .ISDIR => error.IsDir,
        .TXTBSY => error.FileBusy,
        else => error.ExecFailed,
    };
}

/// dup2 in the child; on failure report errno to the parent and exit.
fn dup2OrExit(old: i32, new: i32, err_w: i32) void {
    const rc = linux.dup2(old, new);
    if (linux.errno(rc) == .SUCCESS) return;
    const v: u32 = @intFromEnum(linux.errno(rc));
    _ = linux.write(err_w, @ptrCast(&v), @sizeOf(u32));
    linux.exit_group(126);
}

fn openDevNull() !i32 {
    const rc = linux.open("/dev/null", .{ .ACCMODE = .RDWR }, 0);
    switch (linux.errno(rc)) {
        .SUCCESS => return @intCast(rc),
        else => return error.NoDevNull,
    }
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

/// Incremental reader for `\x00`-framed messages on a pipe. Retains the
/// partial tail across calls (buffer-drain pattern).
pub const Reader = struct {
    fd: i32,
    buf: std.array_list.Aligned(u8, null) = .empty,

    pub fn init(fd: i32) Reader {
        return .{ .fd = fd };
    }

    pub fn deinit(self: *Reader, allocator: Allocator) void {
        self.buf.deinit(allocator);
    }

    /// Read one complete message (terminator stripped, owned copy).
    ///   null              -> clean EOF (write end closed by the browser)
    ///   error.Timeout     -> nothing arrived within timeout_ms (negative = block forever)
    ///   error.UnexpectedEndOfStream -> EOF in the middle of a message
    ///   error.MessageTooLarge -> no terminator within max_message_size
    pub fn readMessage(self: *Reader, allocator: Allocator, timeout_ms: i32) !?[]u8 {
        while (true) {
            if (std.mem.findScalar(u8, self.buf.items, 0)) |sep| {
                const msg = try allocator.dupe(u8, self.buf.items[0..sep]);
                const rest_len = self.buf.items.len - sep - 1;
                std.mem.copyForwards(u8, self.buf.items[0..rest_len], self.buf.items[sep + 1 ..]);
                self.buf.shrinkRetainingCapacity(rest_len);
                return msg;
            }
            if (self.buf.items.len > max_message_size) return error.MessageTooLarge;

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
                        if (self.buf.items.len == 0) return null; // clean EOF
                        return error.UnexpectedEndOfStream;
                    }
                    try self.buf.appendSlice(allocator, chunk[0..n]);
                },
                .INTR => continue,
                .AGAIN => continue, // blocking fd; defensive
                else => return error.ReadFailed,
            }
        }
    }
};

/// Write `msg` followed by the terminator.
pub fn writeMessage(fd: i32, msg: []const u8) !void {
    try writeAll(fd, msg);
    try writeAll(fd, &.{0});
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

/// Close both pipe ends. Idempotent. Closing them makes the browser exit.
pub fn closeFds(s: *Spawned) void {
    if (s.read_fd >= 0) {
        _ = linux.close(s.read_fd);
        s.read_fd = -1;
    }
    if (s.write_fd >= 0) {
        _ = linux.close(s.write_fd);
        s.write_fd = -1;
    }
}

/// Reap the child. Returns the exit code, or error.ChildSignaled if it died
/// on a signal.
pub fn wait(s: *const Spawned) !u8 {
    var status: u32 = 0;
    while (true) {
        const rc = linux.waitpid(s.pid, &status, 0);
        switch (linux.errno(rc)) {
            .SUCCESS => break,
            .INTR => continue,
            else => return error.WaitFailed,
        }
    }
    if (linux.W.IFEXITED(status)) return linux.W.EXITSTATUS(status);
    return error.ChildSignaled;
}

const testing = std.testing;

/// Self-pipe pair for tests (no fork needed).
fn testPipe() ![2]i32 {
    var fds: [2]i32 = undefined;
    if (linux.errno(linux.pipe2(&fds, .{})) != .SUCCESS) return error.PipeFailed;
    return fds;
}

test "writeMessage/readMessage roundtrip" {
    const fds = try testPipe();
    defer {
        _ = linux.close(fds[0]);
        _ = linux.close(fds[1]);
    }
    try writeMessage(fds[1], "{\"id\":1}");

    var reader = Reader.init(fds[0]);
    defer reader.deinit(testing.allocator);
    const msg = (try reader.readMessage(testing.allocator, 1000)).?;
    defer testing.allocator.free(msg);
    try testing.expectEqualSlices(u8, "{\"id\":1}", msg);
}

test "two messages in one write drain one at a time" {
    const fds = try testPipe();
    defer {
        _ = linux.close(fds[0]);
        _ = linux.close(fds[1]);
    }
    try writeAll(fds[1], "{\"id\":1}\x00{\"id\":2}\x00");

    var reader = Reader.init(fds[0]);
    defer reader.deinit(testing.allocator);
    const m1 = (try reader.readMessage(testing.allocator, 1000)).?;
    defer testing.allocator.free(m1);
    const m2 = (try reader.readMessage(testing.allocator, 1000)).?;
    defer testing.allocator.free(m2);
    try testing.expectEqualSlices(u8, "{\"id\":1}", m1);
    try testing.expectEqualSlices(u8, "{\"id\":2}", m2);
}

test "partial write then terminator assembles message" {
    const fds = try testPipe();
    defer {
        _ = linux.close(fds[0]);
        _ = linux.close(fds[1]);
    }
    try writeAll(fds[1], "{\"id\":");
    var reader = Reader.init(fds[0]);
    defer reader.deinit(testing.allocator);
    try testing.expectError(error.Timeout, reader.readMessage(testing.allocator, 50));

    try writeAll(fds[1], "7}\x00");
    const msg = (try reader.readMessage(testing.allocator, 1000)).?;
    defer testing.allocator.free(msg);
    try testing.expectEqualSlices(u8, "{\"id\":7}", msg);
}

test "clean EOF returns null" {
    const fds = try testPipe();
    defer _ = linux.close(fds[0]);
    _ = linux.close(fds[1]); // close write end
    var reader = Reader.init(fds[0]);
    defer reader.deinit(testing.allocator);
    try testing.expectEqual(@as(?[]u8, null), try reader.readMessage(testing.allocator, 1000));
}

test "EOF mid-message is an error" {
    const fds = try testPipe();
    defer _ = linux.close(fds[0]);
    try writeAll(fds[1], "partial");
    _ = linux.close(fds[1]); // EOF with buffered partial message
    var reader = Reader.init(fds[0]);
    defer reader.deinit(testing.allocator);
    try testing.expectError(error.UnexpectedEndOfStream, reader.readMessage(testing.allocator, 1000));
}

test "spawn executes and reaps exit code" {
    var child = try spawn(testing.allocator, &.{ "/bin/true" });
    defer {
        pipeClose(&child);
    }
    try testing.expectEqual(@as(u8, 0), try wait(&child));
}

test "spawn propagates exit code" {
    var child = try spawn(testing.allocator, &.{ "/bin/false" });
    defer pipeClose(&child);
    try testing.expectEqual(@as(u8, 1), try wait(&child));
}

test "spawn missing executable reports FileNotFound" {
    try testing.expectError(error.FileNotFound, spawn(testing.allocator, &.{ "/nonexistent/definitely-not-here" }));
}

fn pipeClose(s: *Spawned) void {
    closeFds(s);
}

test "spawn passes argv and environment" {
    // /bin/sh -c with a side effect we can observe through the exit code.
    // PATH is guaranteed present in the parent's environment.
    var child = try spawn(testing.allocator, &.{ "/bin/sh", "-c", "test -n \"$PATH\" && test \"$0\" = \"/bin/sh\" && exit 3 || exit 4" });
    defer pipeClose(&child);
    try testing.expectEqual(@as(u8, 3), try wait(&child));
}
