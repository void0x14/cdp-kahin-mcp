//! Faz 3 smoke driver: full end-to-end flow against the real Camoufox binary.
//!
//!   spawn -> Browser.enable -> createBrowserContext -> newPage ->
//!   navigate(data URL) -> evaluate("1+1") == 2 ->
//!   console.log captured as Console.messageAdded ->
//!   screenshot PNG magic -> getFrameTree -> getTargets ->
//!   setViewportSize -> clean exit
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

    // Faz 3: enable* are documented no-ops — the harness never sends them.
    d.enableConsole();
    d.enableNetwork();
    d.enableRuntime();

    // Faz 3: Emulation.setDeviceMetricsOverride equivalent.
    try d.setDefaultViewport(null, 1280, 720, 2.0, driver.default_timeout_ms);
    std.debug.print("setDefaultViewport OK\n", .{});

    // Browser.createBrowserContext.
    const ctx = try d.newContext(driver.default_timeout_ms);
    defer a.free(ctx);
    std.debug.print("createBrowserContext -> browserContextId={s}\n", .{ctx});

    // Browser.newPage (no url — Juggler's newPage takes browserContextId only).
    const target = try d.newPage(ctx, null, driver.default_timeout_ms);
    defer a.free(target);
    std.debug.print("newPage -> targetId={s}\n", .{target});

    // Faz 3: Target.getTargets equivalent after the page exists.
    const targets = try d.getTargets();
    defer a.free(targets);
    std.debug.print("getTargets -> {s}\n", .{targets});

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

    // Faz 3: console.log -> Runtime.console -> normalized messageAdded.
    var ev2 = try d.evaluate(target, "console.log('smoke-msg-42')", 15_000);
    defer ev2.deinit(a);
    if (ev2.exception_text != null) return error.EvaluateFailed;
    try d.waitForConsole(15_000);
    const msgs = try d.getConsoleMessages();
    defer a.free(msgs);
    std.debug.print("console messages -> {s}\n", .{msgs});
    if (std.mem.indexOf(u8, msgs, "smoke-msg-42") == null) return error.ConsoleCaptureFailed;

    // Faz 3: Page.captureScreenshot equivalent; verify PNG magic bytes.
    const png = try d.screenshot(target, false, driver.default_timeout_ms);
    defer a.free(png);
    const magic = "\x89PNG\r\n\x1a\n";
    if (png.len < 8 or !std.mem.eql(u8, png[0..8], magic)) {
        std.debug.print("FAIL: expected PNG magic, got {any}\n", .{png[0..@min(png.len, 8)]});
        return error.BadScreenshot;
    }
    std.debug.print("screenshot -> PNG {d} bytes\n", .{png.len});

    // Faz 3: full-page screenshot path (clip from scroll-size evaluate).
    const png_full = try d.screenshot(target, true, driver.default_timeout_ms);
    defer a.free(png_full);
    if (png_full.len < 8 or !std.mem.eql(u8, png_full[0..8], magic)) return error.BadScreenshot;
    std.debug.print("screenshot(full_page) -> PNG {d} bytes\n", .{png_full.len});

    // Faz 3: Page.getFrameTree equivalent from the frame registry.
    const tree = try d.getFrameTree(target);
    defer a.free(tree);
    std.debug.print("getFrameTree -> {s}\n", .{tree});
    if (std.mem.indexOf(u8, tree, "\"frameTree\"") == null) return error.BadFrameTree;

    // Faz 3: Emulation.setViewportSize equivalent.
    try d.setViewportSize(target, 800, 600, driver.default_timeout_ms);
    std.debug.print("setViewportSize OK\n", .{});

    // Faz 3: Target.closeTarget equivalent.
    const closed = try d.closeTarget(target, driver.default_timeout_ms);
    defer a.free(closed);
    std.debug.print("closeTarget -> {s}\n", .{closed});
    if (std.mem.indexOf(u8, closed, "{}") == null) return error.CloseTargetFailed;

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
