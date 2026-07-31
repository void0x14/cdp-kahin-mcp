//! Faz 1 smoke driver: spawn Camoufox with `--juggler-pipe`, send
//! `Browser.enable`, wait for the matching response, then close the pipe
//! (which terminates the browser).
//!
//! Usage: core <firefox-binary> [profile-dir]

const std = @import("std");
const linux = std.os.linux;

const frame = @import("transport/frame.zig");
const pipe = @import("transport/pipe.zig");
const session = @import("transport/session.zig");

const usage =
    \\usage: core <firefox-binary> [profile-dir]
    \\
;

const handshake_timeout_ms: i32 = 30_000;

pub fn main(args: std.process.Init.Minimal) u8 {
    run(args) catch |err| {
        std.debug.print("error: {s}\n", .{@errorName(err)});
        return 1;
    };
    return 0;
}

fn run(args: std.process.Init.Minimal) !void {
    const argv = args.args.vector;
    if (argv.len < 2) {
        std.debug.print("{s}", .{usage});
        return error.MissingArgs;
    }
    const exe = std.mem.sliceTo(argv[1], 0);

    ignoreSigpipe();

    var gpa_state = std.heap.DebugAllocator(.{}).init;
    defer _ = gpa_state.deinit();
    const gpa = gpa_state.allocator();

    var arena_state = std.heap.ArenaAllocator.init(gpa);
    defer arena_state.deinit();
    const a = arena_state.allocator();

    const profile = if (argv.len >= 3) std.mem.sliceTo(argv[2], 0) else blk: {
        const p = try std.fmt.allocPrint(a, "/tmp/kahin-core-{d}-smoke", .{linux.getpid()});
        // Reuse the dir if it already exists; Firefox only needs it present.
        _ = linux.mkdirat(linux.AT.FDCWD, try a.dupeZ(u8, p), 0o700);
        break :blk p;
    };

    const browser_argv = [_][]const u8{
        exe,
        "-juggler-pipe",
        "-profile",
        profile,
        "-no-remote",
        "-headless",
    };

    var child = pipe.spawn(a, &browser_argv) catch |err| {
        std.debug.print("spawn failed: {s}\n", .{@errorName(err)});
        return err;
    };
    defer pipe.closeFds(&child);

    var reader = pipe.Reader.init(child.read_fd);
    defer reader.deinit(a);

    var router = session.Router.init(a);
    defer router.deinit();
    try router.registerSession(.{ .id = "", .target_type = "browser", .name = "root" });

    const id = router.nextId();
    var ctx: u8 = 1;
    try router.registerPending(id, @ptrCast(&ctx));
    const req = try std.fmt.allocPrint(
        a,
        "{{\"id\":{d},\"method\":\"Browser.enable\",\"params\":{{\"attachToDefaultContext\":true}}}}",
        .{id},
    );
    std.debug.print("SENT: {s}\n", .{req});
    pipe.writeMessage(child.write_fd, req) catch |err| {
        std.debug.print("write failed: {s} (browser died?)\n", .{@errorName(err)});
        return err;
    };

    var matched = false;
    while (!matched) {
        const raw = reader.readMessage(a, handshake_timeout_ms) catch |err| {
            std.debug.print("read failed: {s}\n", .{@errorName(err)});
            return err;
        } orelse {
            std.debug.print("EOF: browser closed the pipe without answering\n", .{});
            return error.HandshakeFailed;
        };
        std.debug.print("RECV: {s}\n", .{raw});
        switch (try router.dispatch(raw)) {
            .response => |resp| {
                if (resp.id != id) {
                    std.debug.print("ignoring stale response id={d}\n", .{resp.id});
                    continue;
                }
                matched = true;
                if (resp.is_error) {
                    std.debug.print("Browser.enable returned an ERROR: {s}\n", .{raw});
                    return error.HandshakeFailed;
                }
                std.debug.print("Browser.enable OK (id={d} matched)\n", .{resp.id});
            },
            .event => |ev| std.debug.print("event: {s} session={?s}\n", .{ ev.method, ev.session_id }),
            .invalid => std.debug.print("ignoring unrecognized message\n", .{}),
        }
    }

    // Stop: closing our pipe ends terminates the browser (Juggler pattern).
    pipe.closeFds(&child);
    const code = pipe.wait(&child) catch |err| blk: {
        std.debug.print("browser exited via signal ({s})\n", .{@errorName(err)});
        break :blk @as(u8, 0); // closing the pipe mid-run may kill it on a signal; not a failure
    };
    std.debug.print("browser exited with code {d}\n", .{code});
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

// Keep a reference so the framing module is exercised by the same binary.
fn unused() void {
    _ = frame;
}
