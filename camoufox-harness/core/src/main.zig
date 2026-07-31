//! Faz 2 smoke driver: full end-to-end flow against the real Camoufox binary.
//!
//!   spawn -> Browser.enable -> createBrowserContext -> newPage ->
//!   navigate(data URL) -> evaluate("1+1") == 2 -> clean exit
//!
//! Usage: core <firefox-binary> [profile-dir]

const std = @import("std");
const linux = std.os.linux;

const driver = @import("driver");

const usage =
    \\usage: core <firefox-binary> [profile-dir]
    \\
;

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
    const profile = if (argv.len >= 3) std.mem.sliceTo(argv[2], 0) else null;

    ignoreSigpipe();

    var gpa_state = std.heap.DebugAllocator(.{}).init;
    defer _ = gpa_state.deinit();
    const gpa = gpa_state.allocator();

    var arena_state = std.heap.ArenaAllocator.init(gpa);
    defer arena_state.deinit();
    const a = arena_state.allocator();

    // Start + Browser.enable handshake.
    var d = try driver.Driver.start(a, exe, profile, true);
    defer d.deinit();
    std.debug.print("Browser.enable OK\n", .{});

    // Browser.createBrowserContext.
    const ctx = try d.newContext(driver.default_timeout_ms);
    defer a.free(ctx);
    std.debug.print("createBrowserContext -> browserContextId={s}\n", .{ctx});

    // Browser.newPage (no url — Juggler's newPage takes browserContextId only).
    const target = try d.newPage(ctx, null, driver.default_timeout_ms);
    defer a.free(target);
    std.debug.print("newPage -> targetId={s}\n", .{target});

    // Page.navigate to a data URL (no network dependency), wait for load.
    const nav = try d.navigate(target, "data:text/html,<h1>hi</h1>", driver.default_timeout_ms);
    defer a.free(nav);
    std.debug.print("navigate -> navigationId={s}\n", .{nav});

    // Runtime.evaluate.
    var ev = try d.evaluate(target, "1+1", 15_000);
    defer ev.deinit(a);
    if (ev.exception_text) |t| {
        std.debug.print("evaluate threw: {s}\n", .{t});
        return error.EvaluateFailed;
    }
    std.debug.print("evaluate(\"1+1\") -> {s}\n", .{ev.value_json});
    if (!std.mem.eql(u8, ev.value_json, "2")) {
        std.debug.print("FAIL: expected 2, got {s}\n", .{ev.value_json});
        return error.EvaluateMismatch;
    }
    std.debug.print("SMOKE PASS: evaluate(\"1+1\") == 2\n", .{});

    // Clean shutdown: closing the pipes makes the browser exit 0.
    const code = try d.stop();
    std.debug.print("browser exited with code {d}\n", .{code});
    if (code != 0) return error.BadBrowserExit;
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
