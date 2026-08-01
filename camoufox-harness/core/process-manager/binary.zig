//! Process Manager — binary resolution + version pin awareness.
//!
//! Resolution order (mirrors kahin/the_twins/mirage.py `_camoufox_bin()`):
//!   1. `KAHIN_CAMOUFOX_BIN` env override (explicit; pin check skipped)
//!   2. `$HOME/.cache/camoufox/browsers/official/*/camoufox-bin` — newest
//!      version dir (lexicographically last, same rule as mirage's
//!      `sorted(...)[-1]`)
//!
//! Version pin: `camoufox-harness/vendor/camoufox-fork-pin.lock` pins the
//! protocol schema to a ref (e.g. `ref: v152.0.4-beta.28`). A binary whose
//! path does not contain the pinned ref yields a warning (never a hard
//! error — an explicit env override or a newer fetched version wins).

const std = @import("std");
const linux = std.os.linux;
const Allocator = std.mem.Allocator;

pub const env_var = "KAHIN_CAMOUFOX_BIN";
/// Relative candidates for the pin lock file, probed from the cwd.
const pin_lock_candidates = [_][]const u8{
    "camoufox-harness/vendor/camoufox-fork-pin.lock",
    "vendor/camoufox-fork-pin.lock",
    "../vendor/camoufox-fork-pin.lock",
    "../../vendor/camoufox-fork-pin.lock",
};

/// Resolved binary path + optional pin-consistency warning. Both slices are
/// owned (free with deinit).
pub const Resolution = struct {
    path: []u8,
    warning: ?[]u8 = null,

    pub fn deinit(self: *Resolution, allocator: Allocator) void {
        allocator.free(self.path);
        if (self.warning) |w| allocator.free(w);
        self.* = undefined;
    }
};

/// Resolve the Camoufox binary (env override, then $HOME cache scan).
pub fn resolve(allocator: Allocator) !Resolution {
    if (getEnv(allocator, env_var)) |env_path| {
        if (fileExists(env_path)) {
            return .{ .path = env_path };
        }
        allocator.free(env_path);
        return error.CamoufoxBinEnvMissing;
    }

    const home = getEnv(allocator, "HOME") orelse return error.NoHome;
    defer allocator.free(home);
    const root = try std.fmt.allocPrint(allocator, "{s}/.cache/camoufox/browsers/official", .{home});
    defer allocator.free(root);

    var res = try resolveIn(allocator, root);
    errdefer res.deinit(allocator);
    // Pin consistency: warn (never block) when the binary path does not
    // contain the pinned ref.
    if (res.warning == null) res.warning = try checkPinAuto(allocator, res.path);
    return res;
}

/// Scan `official_root` for `<version>/camoufox-bin` directories; return the
/// lexicographically-last version (mirage.py rule). Testable without env.
pub fn resolveIn(allocator: Allocator, official_root: []const u8) !Resolution {
    const root_z = try allocator.dupeZ(u8, official_root);
    defer allocator.free(root_z);
    const fd = linux.open(root_z.ptr, .{ .ACCMODE = .RDONLY, .DIRECTORY = true, .CLOEXEC = true }, 0);
    switch (linux.errno(fd)) {
        .SUCCESS => {},
        .NOENT => return error.CamoufoxNotFound,
        else => return error.CamoufoxScanFailed,
    }
    defer _ = linux.close(@intCast(fd));

    var best: ?[]u8 = null; // owned copy of the best version dir name
    defer if (best) |b| allocator.free(b);

    var buf: [4096]u8 align(@alignOf(linux.dirent64)) = undefined;
    while (true) {
        const n = linux.getdents64(@intCast(fd), &buf, buf.len);
        switch (linux.errno(n)) {
            .SUCCESS => {},
            .INTR => continue,
            else => return error.CamoufoxScanFailed,
        }
        if (n == 0) break;
        var off: usize = 0;
        while (off < @as(usize, @intCast(n))) {
            const d: *align(1) const linux.dirent64 = @ptrCast(@alignCast(buf[off..].ptr));
            if (d.type == linux.DT.DIR) {
                const name = std.mem.sliceTo(@as([*:0]const u8, @ptrCast(&d.name)), 0);
                if (name.len > 0 and !std.mem.eql(u8, name, ".") and !std.mem.eql(u8, name, "..")) {
                    if (dirHasBin(@intCast(fd), name)) {
                        if (best == null or std.mem.order(u8, name, best.?) == .gt) {
                            if (best) |b| allocator.free(b);
                            best = try allocator.dupe(u8, name);
                        }
                    }
                }
            }
            off += d.reclen;
        }
    }

    const chosen = best orelse return error.CamoufoxNotFound;
    return .{ .path = try std.fmt.allocPrint(allocator, "{s}/{s}/camoufox-bin", .{ official_root, chosen }) };
}

/// Pin check against the first lock file found among the cwd-relative
/// candidates. `null` when no lock file is reachable or the ref matches.
pub fn checkPinAuto(allocator: Allocator, bin_path: []const u8) Allocator.Error!?[]u8 {
    for (pin_lock_candidates) |candidate| {
        const content = readFile(allocator, candidate) catch continue;
        defer allocator.free(content);
        return checkPin(allocator, content, bin_path);
    }
    return null;
}

/// Parse `ref: v<version>` from lock content; warn when `bin_path` does not
/// contain `<version>` (cache installs carry the version in the path).
pub fn checkPin(allocator: Allocator, lock_content: []const u8, bin_path: []const u8) Allocator.Error!?[]u8 {
    const prefix = "ref:";
    var version: ?[]const u8 = null;
    var lines = std.mem.splitScalar(u8, lock_content, '\n');
    while (lines.next()) |line| {
        const trimmed = std.mem.trim(u8, line, " \t\r");
        if (std.mem.startsWith(u8, trimmed, prefix)) {
            version = std.mem.trim(u8, trimmed[prefix.len..], " \t\r");
            break;
        }
    }
    const v = version orelse return null;
    if (v.len == 0) return null;
    // Strip a leading 'v' (lock says "ref: v152.0.4-beta.28").
    const bare = if (v[0] == 'v' or v[0] == 'V') v[1..] else v;
    if (bare.len == 0) return null;
    if (std.mem.indexOf(u8, bin_path, bare) != null) return null;
    return try std.fmt.allocPrint(
        allocator,
        "binary version mismatch: {s} does not contain pinned ref {s} (vendor/camoufox-fork-pin.lock)",
        .{ bin_path, bare },
    );
}

/// Read the whole file (bounded by allocation failure). error when unreadable.
pub fn readFile(allocator: Allocator, path: []const u8) ![]u8 {
    const z = try allocator.dupeZ(u8, path);
    defer allocator.free(z);
    const fd = linux.open(z.ptr, .{ .ACCMODE = .RDONLY, .CLOEXEC = true }, 0);
    switch (linux.errno(fd)) {
        .SUCCESS => {},
        .NOENT => return error.FileNotFound,
        else => return error.OpenFailed,
    }
    defer _ = linux.close(@intCast(fd));

    var out: std.array_list.Aligned(u8, null) = .empty;
    errdefer out.deinit(allocator);
    var chunk: [4096]u8 = undefined;
    while (true) {
        const n = linux.read(@intCast(fd), &chunk, chunk.len);
        switch (linux.errno(n)) {
            .SUCCESS => {
                if (n == 0) break;
                try out.appendSlice(allocator, chunk[0..n]);
            },
            .INTR => continue,
            else => return error.ReadFailed,
        }
    }
    return out.toOwnedSlice(allocator);
}

/// Value of the first `name=` entry in the process environment, or null.
pub fn getEnv(allocator: Allocator, name: []const u8) ?[]u8 {
    const data = readProcSelfEnviron(allocator) catch return null;
    defer allocator.free(data);
    var it = std.mem.splitScalar(u8, data, 0);
    while (it.next()) |entry| {
        if (std.mem.startsWith(u8, entry, name) and entry.len > name.len and entry[name.len] == '=') {
            return allocator.dupe(u8, entry[name.len + 1 ..]) catch null;
        }
    }
    return null;
}

fn readProcSelfEnviron(allocator: Allocator) ![]u8 {
    const fd = linux.open("/proc/self/environ", .{ .ACCMODE = .RDONLY, .CLOEXEC = true }, 0);
    switch (linux.errno(fd)) {
        .SUCCESS => {},
        else => return error.NoEnviron,
    }
    defer _ = linux.close(@intCast(fd));
    const max: usize = 1 << 20;
    const buf = try allocator.alloc(u8, max);
    var len: usize = 0;
    while (len < max) {
        const n = linux.read(@intCast(fd), buf[len..].ptr, max - len);
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

fn fileExists(path: []const u8) bool {
    const z = std.heap.page_allocator.dupeZ(u8, path) catch return false;
    defer std.heap.page_allocator.free(z);
    const fd = linux.open(z.ptr, .{ .ACCMODE = .RDONLY, .CLOEXEC = true }, 0);
    switch (linux.errno(fd)) {
        .SUCCESS => {
            _ = linux.close(@intCast(fd));
            return true;
        },
        else => return false,
    }
}

fn dirHasBin(dir_fd: i32, name: []const u8) bool {
    const z = std.heap.page_allocator.dupeZ(u8, name) catch return false;
    defer std.heap.page_allocator.free(z);
    const fd = linux.openat(dir_fd, z.ptr, .{ .ACCMODE = .RDONLY, .DIRECTORY = true, .CLOEXEC = true }, 0);
    switch (linux.errno(fd)) {
        .SUCCESS => {},
        else => return false,
    }
    defer _ = linux.close(@intCast(fd));

    var found = false;
    var buf: [4096]u8 align(@alignOf(linux.dirent64)) = undefined;
    while (true) {
        const n = linux.getdents64(@intCast(fd), &buf, buf.len);
        switch (linux.errno(n)) {
            .SUCCESS => {},
            .INTR => continue,
            else => return false,
        }
        if (n == 0) break;
        var off: usize = 0;
        while (off < @as(usize, @intCast(n))) {
            const d: *align(1) const linux.dirent64 = @ptrCast(@alignCast(buf[off..].ptr));
            const entry = std.mem.sliceTo(@as([*:0]const u8, @ptrCast(&d.name)), 0);
            if (d.type == linux.DT.REG and std.mem.eql(u8, entry, "camoufox-bin")) {
                found = true;
                break;
            }
            off += d.reclen;
        }
        if (found) break;
    }
    return found;
}

const testing = std.testing;

fn mkdirPath(path: []const u8) !void {
    const z = try testing.allocator.dupeZ(u8, path);
    defer testing.allocator.free(z);
    const rc = linux.mkdir(z.ptr, 0o700);
    switch (linux.errno(rc)) {
        .SUCCESS, .EXIST => {},
        else => return error.MkdirFailed,
    }
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

fn unlinkPath(path: []const u8) void {
    const z = testing.allocator.dupeZ(u8, path) catch return;
    defer testing.allocator.free(z);
    _ = linux.unlink(z.ptr);
}

fn rmdirPath(path: []const u8) void {
    const z = testing.allocator.dupeZ(u8, path) catch return;
    defer testing.allocator.free(z);
    _ = linux.rmdir(z.ptr);
}

test "binary: resolveIn picks the lexicographically-last version dir" {
    const a = testing.allocator;
    const root = try std.fmt.allocPrint(a, "/tmp/kahin-bin-test-{d}", .{linux.getpid()});
    defer a.free(root);
    const v27 = try std.fmt.allocPrint(a, "{s}/152.0.4-beta.27", .{root});
    defer a.free(v27);
    const v28 = try std.fmt.allocPrint(a, "{s}/152.0.4-beta.28", .{root});
    defer a.free(v28);
    const empty = try std.fmt.allocPrint(a, "{s}/empty", .{root});
    defer a.free(empty);
    const b27 = try std.fmt.allocPrint(a, "{s}/camoufox-bin", .{v27});
    defer a.free(b27);
    const b28 = try std.fmt.allocPrint(a, "{s}/camoufox-bin", .{v28});
    defer a.free(b28);
    defer {
        unlinkPath(b27);
        unlinkPath(b28);
        rmdirPath(v27);
        rmdirPath(v28);
        rmdirPath(empty);
        rmdirPath(root);
    }

    // The 'empty' version dir has no binary and must be skipped.
    try mkdirPath(root);
    try mkdirPath(v27);
    try mkdirPath(v28);
    try mkdirPath(empty);
    try touchPath(b27);
    try touchPath(b28);

    var res = try resolveIn(a, root);
    defer res.deinit(a);
    const want = try std.fmt.allocPrint(a, "{s}/152.0.4-beta.28/camoufox-bin", .{root});
    defer a.free(want);
    try testing.expectEqualStrings(want, res.path);
}

test "binary: resolveIn with no versions reports CamoufoxNotFound" {
    const a = testing.allocator;
    const root = try std.fmt.allocPrint(a, "/tmp/kahin-bin-none-{d}", .{linux.getpid()});
    defer a.free(root);
    defer rmdirPath(root);
    try mkdirPath(root);
    try testing.expectError(error.CamoufoxNotFound, resolveIn(a, root));
}

test "binary: checkPin flags version mismatch, silent on match" {
    const lock = "repo: daijro/camoufox\nref: v152.0.4-beta.28\ndate: 2026-07-19\n";
    const ok = try checkPin(testing.allocator, lock, "/home/u/.cache/camoufox/browsers/official/152.0.4-beta.28/camoufox-bin");
    defer if (ok) |w| testing.allocator.free(w);
    try testing.expect(ok == null);

    const bad = (try checkPin(testing.allocator, lock, "/home/u/.cache/camoufox/browsers/official/153.0.0/camoufox-bin")).?;
    defer testing.allocator.free(bad);
    try testing.expect(std.mem.indexOf(u8, bad, "152.0.4-beta.28") != null);
}

test "binary: checkPin tolerates missing ref line" {
    const lock = "repo: daijro/camoufox\ndate: 2026-07-19\n";
    const w = try checkPin(testing.allocator, lock, "/x/camoufox-bin");
    defer if (w) |s| testing.allocator.free(s);
    try testing.expect(w == null);
}
