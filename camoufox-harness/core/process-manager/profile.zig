//! Process Manager — profile directory handling.
//!
//! Juggler rule learned in Faz 1: the `-profile` directory MUST exist before
//! the browser spawns (Firefox stalls otherwise). Every instance gets its
//! own isolated profile dir (per-instance counter, 0700).

const std = @import("std");
const linux = std.os.linux;
const Allocator = std.mem.Allocator;

/// Monotonic per-process counter for default profile naming.
var next_profile_id: u64 = 0;

/// Create `path` (0700). Tolerates an existing DIRECTORY (idempotent);
/// an existing regular file at `path` is an error — a stale file must
/// never be silently reused as a profile (Faz 7 Minor c: pid-reuse state
/// reuse guard). Verified with openat(O_DIRECTORY), not just EEXIST.
pub fn ensureDir(path: []const u8) !void {
    const z = try std.heap.page_allocator.dupeZ(u8, path);
    defer std.heap.page_allocator.free(z);
    const rc = linux.mkdir(z.ptr, 0o700);
    switch (linux.errno(rc)) {
        .SUCCESS => return,
        .EXIST => {},
        else => return error.MkdirFailed,
    }
    // EEXIST: only tolerate a real directory; anything else (a file, a
    // dangling symlink) must not be treated as a usable profile dir.
    const fd = linux.open(z.ptr, .{ .ACCMODE = .RDONLY, .DIRECTORY = true, .CLOEXEC = true }, 0);
    switch (linux.errno(fd)) {
        .SUCCESS => _ = linux.close(@intCast(fd)),
        else => return error.ProfileNotADirectory,
    }
}

/// Unique per-instance profile dir: /tmp/kahin-pm-<pid>-<n> (created).
pub fn defaultProfile(allocator: Allocator) ![]u8 {
    const n = next_profile_id;
    next_profile_id += 1;
    const path = try std.fmt.allocPrint(allocator, "/tmp/kahin-pm-{d}-{d}", .{ linux.getpid(), n });
    try ensureDir(path);
    return path;
}

const testing = std.testing;

fn rmdirPath(path: []const u8) void {
    const z = testing.allocator.dupeZ(u8, path) catch return;
    defer testing.allocator.free(z);
    _ = linux.rmdir(z.ptr);
}

fn unlinkPath(path: []const u8) void {
    const z = testing.allocator.dupeZ(u8, path) catch return;
    defer testing.allocator.free(z);
    _ = linux.unlink(z.ptr);
}

fn touchPath(path: []const u8) !void {
    const z = try testing.allocator.dupeZ(u8, path);
    defer testing.allocator.free(z);
    const fd = linux.open(z.ptr, .{ .ACCMODE = .WRONLY, .CREAT = true, .CLOEXEC = true }, 0o600);
    switch (linux.errno(fd)) {
        .SUCCESS => _ = linux.close(@intCast(fd)),
        else => return error.TouchFailed,
    }
}

test "profile: ensureDir creates and tolerates existing" {
    const a = testing.allocator;
    const path = try std.fmt.allocPrint(a, "/tmp/kahin-prof-test-{d}", .{linux.getpid()});
    defer a.free(path);
    defer rmdirPath(path);
    try ensureDir(path);
    try ensureDir(path); // idempotent
    // It is a directory.
    const z = try a.dupeZ(u8, path);
    defer a.free(z);
    const fd = linux.open(z.ptr, .{ .ACCMODE = .RDONLY, .DIRECTORY = true, .CLOEXEC = true }, 0);
    switch (linux.errno(fd)) {
        .SUCCESS => _ = linux.close(@intCast(fd)),
        else => return error.TestDirMissing,
    }
}

test "profile: defaultProfile is unique per call" {
    const a = testing.allocator;
    const p1 = try defaultProfile(a);
    defer a.free(p1);
    const p2 = try defaultProfile(a);
    defer a.free(p2);
    defer {
        rmdirPath(p1);
        rmdirPath(p2);
    }
    try testing.expect(!std.mem.eql(u8, p1, p2));
    // Both exist (created inside defaultProfile).
    try ensureDir(p1);
    try ensureDir(p2);
}

test "profile: ensureDir rejects a regular file in the way" {
    const a = testing.allocator;
    const path = try std.fmt.allocPrint(a, "/tmp/kahin-prof-file-{d}", .{linux.getpid()});
    defer a.free(path);
    defer unlinkPath(path);
    try touchPath(path);
    // A stale FILE at the profile path must error, never be tolerated as a
    // directory (Faz 7 Minor c).
    try testing.expectError(error.ProfileNotADirectory, ensureDir(path));
}
