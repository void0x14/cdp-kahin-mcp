//! Faz 4 smoke driver: full end-to-end flow against the real Camoufox binary.
//!
//!   spawn -> Browser.enable -> createBrowserContext -> newPage ->
//!   navigate(data URL) -> evaluate("1+1") == 2 ->
//!   console.log captured as Console.messageAdded ->
//!   screenshot PNG magic -> getFrameTree -> getTargets ->
//!   setViewportSize ->
//!   NETWORK INTERCEPTION (Faz 4): fulfill a script request (injected
//!     <script src> answered with base64 JS -> window.__fulfilled == 42),
//!     continue an image request (injected <img> through to the local HTTP
//!     server -> naturalWidth > 0), abort an image request (injected <img>
//!     blocked -> onerror fired) ->
//!   INPUT (Faz 4): click an <input> (dispatchMouseEvent pair) ->
//!     insertText types into it -> dispatchKeyEvent("keyDown", Enter)
//!     recorded by a keydown listener -> verified via Runtime.evaluate ->
//!   clean exit 0
//!
//! Usage: core <firefox-binary> [profile-dir] [port]
//!   port: local HTTP server port for the interception continue-proof
//!         (default 8333); serve /ok.png (a real PNG) from its root.

const std = @import("std");
const linux = std.os.linux;

const driver = @import("driver");
const pm = driver.pm;

const usage =
    \\usage: core <firefox-binary> [profile-dir] [port]
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
    const port = if (argv.len >= 4) std.mem.sliceTo(argv[3], 0) else "8333";

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

    // ---- Faz 4: network interception ------------------------------------
    const base = try std.fmt.allocPrint(a, "http://127.0.0.1:{s}", .{port});

    try d.setInterception(target, true, driver.default_timeout_ms);
    std.debug.print("SMOKE PASS: setInterception(enabled=true)\n", .{});

    // 1) FULFILL: <script src=.../s1.js> answered from the driver with
    //    base64 JS — proves the browser never contacted the server and ran
    //    OUR body instead.
    const inject_script = try std.fmt.allocPrint(a, "window.__fulfilled=0;var s=document.createElement('script');s.src='{s}/s1.js';document.body.appendChild(s)", .{base});
    var evs = try d.evaluate(target, inject_script, 15_000);
    evs.deinit(a);
    if (evs.exception_text != null) return error.EvaluateFailed;
    try d.waitForIntercepted(15_000);
    const pend1 = try d.listInterceptedRequests();
    defer a.free(pend1);
    std.debug.print("intercepted #1 -> {s}\n", .{pend1});
    const rid1 = try firstRequestId(a, pend1);
    defer a.free(rid1);
    const js_body = "window.__fulfilled=42";
    const js_b64 = try base64Encode(a, js_body);
    defer a.free(js_b64);
    const hdr_js = [_]driver.browser.Header{.{ .name = "Content-Type", .value = "text/javascript" }};
    try d.fulfillInterceptedRequest(rid1, 200, "OK", &hdr_js, js_b64, driver.default_timeout_ms);
    std.debug.print("SMOKE PASS: fulfillInterceptedRequest -> base64 JS body\n", .{});
    try evalPoll(&d, a, target, "window.__fulfilled===42", "true", 15_000);
    std.debug.print("SMOKE PASS: fulfilled script executed (window.__fulfilled == 42)\n", .{});

    // 2) CONTINUE: <img src=.../ok.png> passes through to the local HTTP
    //    server; the 1x1 PNG decodes (naturalWidth == 1).
    const inject_img = try std.fmt.allocPrint(a, "var i=document.createElement('img');i.id='ok';i.src='{s}/ok.png';document.body.appendChild(i)", .{base});
    var evi = try d.evaluate(target, inject_img, 15_000);
    evi.deinit(a);
    if (evi.exception_text != null) return error.EvaluateFailed;
    try d.waitForIntercepted(15_000);
    const pend2 = try d.listInterceptedRequests();
    defer a.free(pend2);
    std.debug.print("intercepted #2 -> {s}\n", .{pend2});
    const rid2 = try firstRequestId(a, pend2);
    defer a.free(rid2);
    try d.continueInterceptedRequest(rid2, null, null, null, null, driver.default_timeout_ms);
    std.debug.print("SMOKE PASS: continueInterceptedRequest (resume, passthrough)\n", .{});
    try evalPoll(&d, a, target, "document.getElementById('ok').naturalWidth===1", "true", 15_000);
    std.debug.print("SMOKE PASS: continued image loaded from server (naturalWidth == 1)\n", .{});

    // 3) ABORT: <img src=.../abort.png> cancelled with NS_ERROR_ABORT —
    //    the request never reaches the server; onerror fires in the page.
    const inject_abort = try std.fmt.allocPrint(a, "window.__aborted=false;var i=document.createElement('img');i.id='ab';i.onerror=function(){{window.__aborted=true}};i.src='{s}/abort.png';document.body.appendChild(i)", .{base});
    var eva = try d.evaluate(target, inject_abort, 15_000);
    eva.deinit(a);
    if (eva.exception_text != null) return error.EvaluateFailed;
    try d.waitForIntercepted(15_000);
    const pend3 = try d.listInterceptedRequests();
    defer a.free(pend3);
    std.debug.print("intercepted #3 -> {s}\n", .{pend3});
    const rid3 = try firstRequestId(a, pend3);
    defer a.free(rid3);
    try d.abortInterceptedRequest(rid3, "NS_ERROR_ABORT", driver.default_timeout_ms);
    std.debug.print("SMOKE PASS: abortInterceptedRequest(errorCode=NS_ERROR_ABORT)\n", .{});
    try evalPoll(&d, a, target, "window.__aborted", "true", 15_000);
    std.debug.print("SMOKE PASS: aborted image never loaded (onerror fired)\n", .{});

    // 4) Toggle off — proves the switch works both ways.
    try d.setInterception(target, false, driver.default_timeout_ms);
    std.debug.print("SMOKE PASS: setInterception(enabled=false)\n", .{});

    // ---- Faz 4: input (klavye/mouse) ------------------------------------
    const inject_input =
        \\window.__keys=[];var i=document.createElement('input');i.id='txt';i.type='text';i.style.position='fixed';i.style.left='10px';i.style.top='10px';document.body.appendChild(i);document.addEventListener('keydown',function(e){window.__keys.push(e.key)})
    ;
    var evin = try d.evaluate(target, inject_input, 15_000);
    evin.deinit(a);
    if (evin.exception_text != null) return error.EvaluateFailed;

    // Click the input (dispatchMouseEvent mousedown+mouseup at its center).
    const center = try elementCenter(a, &d, target, "txt", 15_000);
    try d.click(target, center[0], center[1], driver.default_timeout_ms);
    std.debug.print("SMOKE PASS: click @ ({d},{d}) (dispatchMouseEvent pair)\n", .{ center[0], center[1] });
    try evalPoll(&d, a, target, "document.activeElement.id==='txt'", "true", 15_000);
    std.debug.print("SMOKE PASS: click focused the input (activeElement == txt)\n", .{});

    // insertText types into the focused input.
    try d.insertText(target, "faz4-hello", driver.default_timeout_ms);
    std.debug.print("SMOKE PASS: insertText(\"faz4-hello\")\n", .{});
    try evalPoll(&d, a, target, "document.getElementById('txt').value==='faz4-hello'", "true", 15_000);
    std.debug.print("SMOKE PASS: input value == faz4-hello\n", .{});

    // dispatchKeyEvent: keyDown Enter must reach the page listener.
    try d.dispatchKeyEvent(target, "keyDown", "Enter", 13, 0, "Enter", false, null, driver.default_timeout_ms);
    try evalPoll(&d, a, target, "window.__keys.length===1&&window.__keys[0]==='Enter'", "true", 15_000);
    std.debug.print("SMOKE PASS: dispatchKeyEvent keyDown Enter recorded (__keys == [\"Enter\"])\n", .{});

    // Faz 3: Target.closeTarget equivalent.
    const closed = try d.closeTarget(target, driver.default_timeout_ms);
    defer a.free(closed);
    std.debug.print("closeTarget -> {s}\n", .{closed});
    if (std.mem.indexOf(u8, closed, "{}") == null) return error.CloseTargetFailed;

    // ---- Faz 6: multi-context isolation (one instance, N contexts) ------
    // Two contexts on the SAME instance: independent pages + documents.
    const mctx_a = try d.newContext(driver.default_timeout_ms);
    defer a.free(mctx_a);
    const mctx_b = try d.newContext(driver.default_timeout_ms);
    defer a.free(mctx_b);
    const pta = try d.newPage(mctx_a, null, driver.default_timeout_ms);
    defer a.free(pta);
    const ptb = try d.newPage(mctx_b, null, driver.default_timeout_ms);
    defer a.free(ptb);

    const na = try d.navigate(pta, "data:text/html,<h1>alpha</h1>", driver.default_timeout_ms);
    defer a.free(na);
    const nb = try d.navigate(ptb, "data:text/html,<h1>beta</h1>", driver.default_timeout_ms);
    defer a.free(nb);

    var e_a = try d.evaluate(pta, "1+1", 15_000);
    defer e_a.deinit(a);
    var e_b = try d.evaluate(ptb, "2+3", 15_000);
    defer e_b.deinit(a);
    if (e_a.exception_text != null or !std.mem.eql(u8, e_a.value_json, "2")) return error.EvaluateFailed;
    if (e_b.exception_text != null or !std.mem.eql(u8, e_b.value_json, "5")) return error.EvaluateFailed;
    std.debug.print("SMOKE PASS: two contexts on one instance evaluate (a: 1+1=2, b: 2+3=5)\n", .{});

    // Document isolation: each page sees only its own DOM.
    var h_a = try d.evaluate(pta, "document.querySelector('h1').textContent", 15_000);
    defer h_a.deinit(a);
    var h_b = try d.evaluate(ptb, "document.querySelector('h1').textContent", 15_000);
    defer h_b.deinit(a);
    std.debug.print("isolation: ctx_a h1={s}, ctx_b h1={s}\n", .{ h_a.value_json, h_b.value_json });
    if (!std.mem.eql(u8, h_a.value_json, "\"alpha\"") or !std.mem.eql(u8, h_b.value_json, "\"beta\"")) {
        return error.IsolationFailed;
    }
    std.debug.print("SMOKE PASS: per-context document isolation\n", .{});

    // Close ctx_a: its page dies with it; ctx_b must keep working.
    try d.removeBrowserContext(mctx_a, driver.default_timeout_ms);
    std.debug.print("SMOKE PASS: removeBrowserContext(ctx_a)\n", .{});

    var e_b2 = try d.evaluate(ptb, "2+3", 15_000);
    defer e_b2.deinit(a);
    if (e_b2.exception_text != null or !std.mem.eql(u8, e_b2.value_json, "5")) return error.EvaluateFailed;
    std.debug.print("SMOKE PASS: ctx_b still evaluates after ctx_a closed\n", .{});

    // The closed context's page must be dead (removal took effect).
    if (d.evaluate(pta, "1+1", 8_000)) |res| {
        var r = res;
        r.deinit(a);
        std.debug.print("FAIL: ctx_a page still evaluates after context removal\n", .{});
        return error.ContextRemovalIneffective;
    } else |_| {
        std.debug.print("SMOKE PASS: ctx_a page is dead after removal (evaluate fails)\n", .{});
    }

    // Clean up ctx_b explicitly, then clean stop -> exit 0.
    const closed_b = try d.closeTarget(ptb, driver.default_timeout_ms);
    defer a.free(closed_b);
    try d.removeBrowserContext(mctx_b, driver.default_timeout_ms);
    std.debug.print("SMOKE PASS: ctx_b closed\n", .{});

    // ---- Faz 6: health-check (pipe gone -> instance dead) ----------------
    // A second instance, spawned directly through the process manager.
    const inst = try pm.Instance.spawn(a, exe, null, true);
    defer inst.deinit(a);
    if (inst.health() != .healthy) return error.HealthCheckFailed;
    std.debug.print("health-check: instance healthy right after spawn (pid {d})\n", .{inst.child.pid});

    // SIGKILL the browser externally; the pipe dies; health flips to dead.
    const kill_rc = linux.kill(inst.child.pid, .KILL);
    if (linux.errno(kill_rc) != .SUCCESS) return error.KillFailed;
    var dead = false;
    var tries: u32 = 0;
    while (tries < 50) : (tries += 1) {
        if (inst.health() == .dead) {
            dead = true;
            break;
        }
        sleepMs(100);
    }
    if (!dead) return error.HealthCheckFailed;
    std.debug.print("SMOKE PASS: health-check detected dead instance after external kill\n", .{});

    // Reap: a SIGKILLed child reports ChildSignaled (not exit 0).
    if (inst.stop(5_000)) |krc| {
        std.debug.print("note: killed instance exited with code {d}\n", .{krc});
    } else |err| switch (err) {
        error.ChildSignaled => std.debug.print("SMOKE PASS: killed instance reaped (ChildSignaled)\n", .{}),
        else => return err,
    }

    // Clean shutdown: closing the pipes makes the browser exit 0.
    const code = try d.stop();
    std.debug.print("browser exited with code {d}\n", .{code});
    if (code != 0) return error.BadBrowserExit;
}

/// First requestId of the pending-registry JSON (smoke helper).
fn firstRequestId(a: std.mem.Allocator, json: []const u8) ![]u8 {
    const parsed = try std.json.parseFromSlice(std.json.Value, a, json, .{});
    defer parsed.deinit();
    const v = parsed.value;
    if (v != .array or v.array.items.len == 0) return error.NoPendingRequest;
    const first = v.array.items[0];
    if (first != .object) return error.NoPendingRequest;
    const rid = first.object.get("requestId") orelse return error.NoPendingRequest;
    if (rid != .string) return error.NoPendingRequest;
    return a.dupe(u8, rid.string);
}

/// Element center via getBoundingClientRect (smoke helper).
fn elementCenter(a: std.mem.Allocator, d: *driver.Driver, target: []const u8, id: []const u8, timeout_ms: i32) !struct { f64, f64 } {
    const expr = try std.fmt.allocPrint(a, "(function(){{var r=document.getElementById('{s}').getBoundingClientRect();return [r.x+r.width/2, r.y+r.height/2]}})()", .{id});
    defer a.free(expr);
    var ev = try d.evaluate(target, expr, timeout_ms);
    defer ev.deinit(a);
    if (ev.exception_text != null or ev.value_json.len == 0) return error.NoElement;
    const parsed = try std.json.parseFromSlice(std.json.Value, a, ev.value_json, .{});
    defer parsed.deinit();
    const v = parsed.value;
    if (v != .array or v.array.items.len < 2) return error.NoElement;
    const x = switch (v.array.items[0]) {
        .integer => |i| @as(f64, @floatFromInt(i)),
        .float => |f| f,
        else => return error.NoElement,
    };
    const y = switch (v.array.items[1]) {
        .integer => |i| @as(f64, @floatFromInt(i)),
        .float => |f| f,
        else => return error.NoElement,
    };
    return .{ x, y };
}

/// Poll evaluate until value_json == want (smoke helper; ~100ms between
/// tries, capped by max_tries).
fn evalPoll(d: *driver.Driver, a: std.mem.Allocator, target: []const u8, expr: []const u8, want: []const u8, timeout_ms: i32) !void {
    const max_tries: u32 = @intCast(@max(1, @divTrunc(timeout_ms, 100)));
    var tries: u32 = 0;
    while (tries < max_tries) : (tries += 1) {
        var ev = try d.evaluate(target, expr, 5_000);
        defer ev.deinit(a);
        if (ev.exception_text == null and std.mem.eql(u8, ev.value_json, want)) return;
        sleepMs(100);
    }
    return error.PollTimeout;
}

/// Standard base64 encode (Juggler fulfill bodies are base64 strings).
fn base64Encode(a: std.mem.Allocator, data: []const u8) ![]u8 {
    const enc = std.base64.standard.Encoder;
    const out = try a.alloc(u8, enc.calcSize(data.len));
    _ = enc.encode(out, data);
    return out;
}

/// std.time.sleep is gone in 0.16; raw nanosleep keeps this dependency-free.
fn sleepMs(ms: u64) void {
    var ts = std.os.linux.timespec{
        .sec = @intCast(ms / 1000),
        .nsec = @intCast((ms % 1000) * std.time.ns_per_ms),
    };
    _ = std.os.linux.nanosleep(&ts, null);
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
