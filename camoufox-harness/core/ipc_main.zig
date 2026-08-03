//! Kahin sidecar: Juggler-native method-based JSON-over-stdio IPC in front
//! of the Juggler pipe driver (MASTER-PLAN Faz 5, Faz 9 Task 2).
//! `kahin/the_twins/mirage.py` spawns this binary and speaks
//! newline-delimited JSON over stdin/stdout; this process owns the real
//! Camoufox browser via the driver's Juggler pipe.
//!
//! Build (from camoufox-harness/core):
//!   zig build-exe --dep driver -Mroot=ipc_main.zig -Mdriver=driver.zig \
//!     -O ReleaseSafe -femit-bin=zig-out/bin/kahin-sidecar
//! Usage: kahin-sidecar <firefox-binary> [profile-dir]
//!
//! Wire protocol (one JSON object per line) — Juggler-native method wire;
//! the CDP-shaped {domain,command} request layer is gone:
//!   req  -> {"id":N,"method":"<Domain.method>","params":{...},"sessionId?":"..."}
//!   resp -> {"id":N,"result":{...}} | {"id":N,"error":{"code":N,"message":"..."}}
//!   evt  -> {"method":"<M>","params":{...},"sessionId":"..."}
//!           (Juggler event names forwarded VERBATIM — no CDP translation)
//!
//! Routing:
//!   Browser.health          -> answered locally (no wire; process health)
//!   Browser.close           -> Browser.close + sidecar shutdown
//!   Browser.newPage         -> driver newPage (registers + tracks current target)
//!   Page.navigate           -> driver navigate (frameId/loaderId shape)
//!   Page.captureScreenshot  -> Page.screenshot translation (mimeType/clip)
//!   Page.getFrameTree       -> derived from driver state (CDP name)
//!   Runtime.evaluate        -> driver evaluate with evaluateWithRetry (context race)
//!   Browser.*               -> root-session passthrough
//!   Page.*/Runtime.*/Network.*/Heap.* -> page-session passthrough
//!        (request sessionId ?? current page ?? error -32600)
//!   anything else           -> root-session passthrough so the real Juggler
//!        -32601 "Method not found" propagates back unchanged.
//!
//! No-op Network.enable / Console.enable handlers were REMOVED (Faz 9): the
//! calls now forward to the real Juggler domain (Juggler has Network.enable;
//! unknown methods like Console.enable surface the genuine -32601 error).
//!
//! Events are forwarded by an idle drain: browser bytes are copied into a
//! private forwarding buffer as they are read, so the driver's own pump
//! still consumes everything it needs (session/frame/context state stays
//! consistent). ponytail: events that arrive while a driver call is pumping
//! (response in flight) are consumed by that pump and not forwarded upward;
//! state is still correct.

const std = @import("std");
const linux = std.os.linux;
const Allocator = std.mem.Allocator;

const driver_mod = @import("driver.zig");
const pipe = @import("src/transport/pipe.zig");

const max_line: usize = 16 * 1024 * 1024;
const request_timeout_ms: i32 = 30_000;
const chunk_size: usize = 64 * 1024;

var line_buf: std.array_list.Aligned(u8, null) = .empty;
/// Set by Browser.close; the sidecar shuts down with the browser.
var running: bool = true;
/// Bytes pulled from the browser fd but not yet forwarded upward (may end
/// with a partial message that spans two read chunks).
var pending: std.array_list.Aligned(u8, null) = .empty;
/// targetId of the page the caller last created; page-scoped commands run on it.
var current_target: ?[]u8 = null;

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
        std.debug.print("usage: kahin-sidecar <firefox-binary> [profile-dir]\n", .{});
        return error.InvalidArgs;
    }
    const exe = std.mem.sliceTo(argv[1], 0);
    var visible = false;
    var verbose = false;
    const prof_arg: ?[]const u8 = blk: {
        var i: usize = 2;
        while (i < argv.len) : (i += 1) {
            const a = std.mem.sliceTo(argv[i], 0);
            if (std.mem.eql(u8, a, "--visible")) {
                visible = true;
                continue;
            }
            if (std.mem.eql(u8, a, "--verbose")) {
                verbose = true;
                continue;
            }
            break :blk a;
        }
        break :blk null;
    };
    var prof_buf: [64]u8 = undefined;
    const profile: ?[]const u8 = prof_arg orelse
        std.fmt.bufPrint(&prof_buf, "/tmp/kahin-sidecar-{d}", .{linux.getpid()}) catch "kahin-sidecar-default";

    ignoreSigpipe();

    var gpa = std.heap.DebugAllocator(.{}).init;
    defer _ = gpa.deinit();
    const a = gpa.allocator();

    var d = try driver_mod.Driver.start(a, exe, profile, verbose, visible);
    defer d.deinit();
    defer _ = d.stop() catch 0;

    var arena = std.heap.ArenaAllocator.init(a);
    defer arena.deinit();

    var pollfds = [_]linux.pollfd{
        .{ .fd = 0, .events = linux.POLL.IN, .revents = 0 },
        .{ .fd = d.reader.fd, .events = linux.POLL.IN, .revents = 0 },
    };
    while (running) {
        _ = arena.reset(.retain_capacity);
        _ = linux.poll(&pollfds, pollfds.len, -1); // block until stdin or browser speaks

        // Responses/events accumulate in `out`; flushed once per iteration
        // (single write sequence per turn).
        var out: std.array_list.Aligned(u8, null) = .empty;
        defer out.deinit(a);

        // Idle events flow upward continuously (oracle collectors are async).
        if (pollfds[1].revents & (linux.POLL.IN | linux.POLL.HUP | linux.POLL.ERR) != 0) {
            drainEvents(&d, a, 0, &out) catch {};
            // Browser fd went away (HUP) or errored: the browser is gone —
            // the sidecar shuts down WITH the browser (defer d.stop() reaps).
            if (pollfds[1].revents & (linux.POLL.HUP | linux.POLL.ERR) != 0) {
                running = false;
            }
        }
        if (pollfds[0].revents & (linux.POLL.IN | linux.POLL.HUP) != 0) {
            const line = readLine(a) catch null orelse break; // stdin EOF -> take the browser down
            defer a.free(line);

            drainEvents(&d, a, null, &out) catch {};
            processRequest(&d, a, arena.allocator(), line, &out) catch |err| switch (err) {
                error.BrokenPipe => return,
                else => {}, // response already attempted; keep serving
            };
            drainEvents(&d, a, 2, &out) catch {};
        }
        try writeAllStdout(out.items);
    }

    _ = d.stop() catch 0; // browser may already be gone (HUP exit path)
    if (current_target) |t| a.free(t);
    pending.deinit(a);
    line_buf.deinit(a);
}

/// Route one method-based request. Responses are appended to `out` (the
/// caller flushes to stdout once per turn).
fn processRequest(d: *driver_mod.Driver, a: Allocator, aa: Allocator, line: []const u8, out: *std.array_list.Aligned(u8, null)) !void {
    const parsed = std.json.parseFromSlice(std.json.Value, aa, line, .{}) catch {
        try respondErr(a, out, 0, -32700, "parse error");
        return;
    };
    const root = parsed.value;
    if (root != .object) {
        try respondErr(a, out, 0, -32600, "request must be an object");
        return;
    }
    const obj = root.object;
    const id_v = obj.get("id") orelse {
        try respondErr(a, out, 0, -32600, "missing id");
        return;
    };
    if (id_v != .integer) {
        try respondErr(a, out, 0, -32600, "id must be an integer");
        return;
    }
    const id: u32 = @intCast(id_v.integer);
    const method_v = obj.get("method") orelse {
        try respondErr(a, out, id, -32600, "missing method");
        return;
    };
    if (method_v != .string) {
        try respondErr(a, out, id, -32600, "method must be a string");
        return;
    }
    const method = method_v.string;
    const params = if (obj.get("params")) |p| p else std.json.Value{ .object = .empty };
    const session_v = obj.get("sessionId");
    const session_id: ?[]const u8 = if (session_v != null and session_v.? == .string and session_v.?.string.len > 0) session_v.?.string else null;

    // === Sidecar-local / driver-specialized methods (no raw wire) ===
    if (std.mem.eql(u8, method, "Browser.health")) return handleHealth(d, a, out, id);
    if (std.mem.eql(u8, method, "Browser.close")) return handleClose(d, a, out, id);
    if (std.mem.eql(u8, method, "Browser.newPage")) return handleNewPage(d, a, out, id, params);
    if (std.mem.eql(u8, method, "Page.navigate")) return handleNavigate(d, a, out, id, params, session_id);
    if (std.mem.eql(u8, method, "Page.captureScreenshot")) return handleScreenshot(d, a, out, id, params, session_id);
    if (std.mem.eql(u8, method, "Page.getFrameTree")) return handleFrameTree(d, a, out, id, session_id);
    if (std.mem.eql(u8, method, "Runtime.evaluate")) return handleEvaluate(d, a, out, id, params, session_id);

    // === Default routing ===
    // Browser.* and unknown methods -> root session; page domains ->
    // request sessionId, else the current page, else -32600.
    const target_session: ?[]const u8 = if (isPageDomain(method))
        session_id orelse blk: {
            const p = currentPage(d) orelse {
                try respondErr(a, out, id, -32600, "no page session");
                return;
            };
            break :blk p.session_id;
        }
    else
        null;

    return passthrough(d, a, out, id, target_session, method, params);
}

/// Page-oriented Juggler method families: forwarded on the page session.
fn isPageDomain(method: []const u8) bool {
    return std.mem.startsWith(u8, method, "Page.") or
        std.mem.startsWith(u8, method, "Runtime.") or
        std.mem.startsWith(u8, method, "Network.") or
        std.mem.startsWith(u8, method, "Heap.");
}

/// Raw forward to the browser: the Juggler response (result OR its genuine
/// error, e.g. -32601 "Method not found") is re-wrapped in the IPC envelope.
fn passthrough(d: *driver_mod.Driver, a: Allocator, out: *std.array_list.Aligned(u8, null), id: u32, session_id: ?[]const u8, method: []const u8, params: std.json.Value) !void {
    const params_json = try std.json.Stringify.valueAlloc(a, params, .{});
    defer a.free(params_json);
    var resp = d.send(session_id, method, params_json, request_timeout_ms) catch {
        try respondErr(a, out, id, -32000, "Juggler call failed");
        return;
    };
    defer resp.deinit(a);
    try respondFromRaw(a, out, id, resp.raw);
}

// === Sidecar-local handlers ===

/// Browser.health — process-manager status, no Juggler wire. Works even
/// when the browser is gone (alive=false).
fn handleHealth(d: *driver_mod.Driver, a: Allocator, out: *std.array_list.Aligned(u8, null), id: u32) !void {
    const alive = if (d.instance) |inst| inst.health() == .healthy else false;
    const pid: i32 = if (d.instance) |inst| inst.child.pid else -1;
    const state: []const u8 = if (alive) "running" else "dead";
    const json = try std.json.Stringify.valueAlloc(a, .{ .alive = alive, .pid = pid, .state = state }, .{});
    defer a.free(json);
    return respondOk(a, out, id, json);
}

/// Browser.close — browser exits before replying; the sidecar shuts down
/// with it.
fn handleClose(d: *driver_mod.Driver, a: Allocator, out: *std.array_list.Aligned(u8, null), id: u32) !void {
    d.close(request_timeout_ms) catch {};
    try respondOk(a, out, id, "{}");
    running = false;
}

/// Browser.newPage — create the page through the driver (registered on
/// attachedToTarget), record it as the current target, optionally navigate.
fn handleNewPage(d: *driver_mod.Driver, a: Allocator, out: *std.array_list.Aligned(u8, null), id: u32, params: std.json.Value) !void {
    const ctx = getStringParam(params, "browserContextId");
    const url = getStringParam(params, "url");
    const target_id = try d.newPage(ctx, url, request_timeout_ms);
    defer a.free(target_id);
    try setCurrentTarget(a, target_id);
    const json = try std.json.Stringify.valueAlloc(a, .{ .targetId = target_id }, .{});
    defer a.free(json);
    return respondOk(a, out, id, json);
}

// === Page / Runtime specialized handlers ===

/// The page a session-scoped call operates on: the request's explicit
/// sessionId, else the current (last created) page.
fn resolvePageFor(d: *driver_mod.Driver, session_id: ?[]const u8) ?*driver_mod.Page {
    if (session_id) |sid| return d.by_session.get(sid);
    return currentPage(d);
}

/// Page.navigate — driver-managed navigation (lifecycle + load wait).
/// Response carries the CDP-compatible {frameId, loaderId} shape.
fn handleNavigate(d: *driver_mod.Driver, a: Allocator, out: *std.array_list.Aligned(u8, null), id: u32, params: std.json.Value, session_id: ?[]const u8) !void {
    const url = getStringParam(params, "url") orelse {
        try respondErr(a, out, id, -32602, "missing url");
        return;
    };
    const p = resolvePageFor(d, session_id) orelse {
        try respondErr(a, out, id, -32600, "no page session");
        return;
    };
    const nav_id = d.navigate(p.target_id, url, request_timeout_ms) catch |err| switch (err) {
        error.NavigationAborted => {
            try respondErr(a, out, id, -32000, d.last_abort_text orelse "navigation aborted");
            return;
        },
        else => {
            try respondErr(a, out, id, -32000, "navigate failed");
            return;
        },
    };
    defer a.free(nav_id);
    const frame_id = p.main_frame_id orelse "";
    const json = try std.json.Stringify.valueAlloc(a, .{ .frameId = frame_id, .loaderId = nav_id }, .{});
    defer a.free(json);
    return respondOk(a, out, id, json);
}

/// Page.screenshot translation: {format} -> {mimeType, clip, ...}.
fn handleScreenshot(d: *driver_mod.Driver, a: Allocator, out: *std.array_list.Aligned(u8, null), id: u32, params: std.json.Value, session_id: ?[]const u8) !void {
    const p = resolvePageFor(d, session_id) orelse {
        try respondErr(a, out, id, -32600, "no page session");
        return;
    };
    const format = getStringParam(params, "format") orelse "png";
    const mime = if (std.mem.eql(u8, format, "jpeg")) "image/jpeg" else "image/png";
    const quality = getIntParam(params, "quality");
    const omit = getBoolParam(params, "omitDeviceScaleFactor");
    const Clip = struct { x: f64, y: f64, width: f64, height: f64 };
    const clip_override = if (params == .object) params.object.get("clip") else null;
    const payload = if (clip_override != null and clip_override.? == .object)
        try std.json.Stringify.valueAlloc(
            a,
            .{ .mimeType = mime, .clip = clip_override.?, .quality = quality, .omitDeviceScaleFactor = omit },
            .{ .emit_null_optional_fields = false },
        )
    else
        try std.json.Stringify.valueAlloc(
            a,
            .{ .mimeType = mime, .clip = Clip{ .x = 0, .y = 0, .width = 1280, .height = 720 }, .quality = quality, .omitDeviceScaleFactor = omit },
            .{ .emit_null_optional_fields = false },
        );
    defer a.free(payload);
    var resp = try d.send(p.session_id, "Page.screenshot", payload, request_timeout_ms);
    defer resp.deinit(a);
    return respondFromRaw(a, out, id, resp.raw);
}

/// Page.getFrameTree — CDP name; derived from driver frame state (Juggler
/// has frameTree, but this keeps the CDP-shaped answer the oracle expects).
fn handleFrameTree(d: *driver_mod.Driver, a: Allocator, out: *std.array_list.Aligned(u8, null), id: u32, session_id: ?[]const u8) !void {
    const p = resolvePageFor(d, session_id) orelse {
        try respondErr(a, out, id, -32600, "no page session");
        return;
    };
    const frame_id = p.main_frame_id orelse "";
    const Frame = struct { id: []const u8, name: []const u8 = "", url: []const u8 = "" };
    const Node = struct { frame: Frame, childFrames: []const Frame = &.{} };
    const json = try std.json.Stringify.valueAlloc(a, .{ .frameTree = Node{ .frame = .{ .id = frame_id } } }, .{});
    defer a.free(json);
    return respondOk(a, out, id, json);
}

/// Runtime.evaluate — keeps the evaluateWithRetry context-race handling.
fn handleEvaluate(d: *driver_mod.Driver, a: Allocator, out: *std.array_list.Aligned(u8, null), id: u32, params: std.json.Value, session_id: ?[]const u8) !void {
    const expr = getStringParam(params, "expression") orelse {
        try respondErr(a, out, id, -32602, "missing expression");
        return;
    };
    const p = resolvePageFor(d, session_id) orelse {
        try respondErr(a, out, id, -32600, "no page session");
        return;
    };
    var res = evaluateWithRetry(d, p.target_id, expr) catch |err| switch (err) {
        error.NoExecutionContext => {
            try respondErr(a, out, id, -32000, "no execution context");
            return;
        },
        else => {
            try respondErr(a, out, id, -32000, "evaluate failed");
            return;
        },
    };
    defer res.deinit(a);

    if (res.exception_text) |et| {
        const json = try std.json.Stringify.valueAlloc(
            a,
            .{
                .result = .{ .type = "undefined" },
                .exceptionDetails = .{ .text = et, .stack = res.exception_stack orelse "" },
            },
            .{ .emit_null_optional_fields = false },
        );
        defer a.free(json);
        return respondOk(a, out, id, json);
    }
    const json = if (res.value_json.len == 0)
        try a.dupe(u8, "{\"result\":{\"type\":\"undefined\"}}")
    else
        try std.fmt.allocPrint(a, "{{\"result\":{{\"type\":\"{s}\",\"value\":{s}}}}}", .{ jsonType(res.value_json), res.value_json });
    defer a.free(json);
    return respondOk(a, out, id, json);
}

/// Context destroy/create events race evaluate on a navigating page; let
/// pending events settle (pump), then retry once with a fresh context.
fn evaluateWithRetry(d: *driver_mod.Driver, tid: []const u8, expr: []const u8) @TypeOf(d.evaluate(tid, expr, 0)) {
    return d.evaluate(tid, expr, request_timeout_ms) catch |err| switch (err) {
        error.JugglerError, error.WaitTimeout => blk: {
            d.pump(100) catch {};
            break :blk d.evaluate(tid, expr, request_timeout_ms);
        },
        else => return err,
    };
}

// === Shared plumbing ===

/// Wrap a Juggler response `{"id":N,"result":...}` / `{"id":N,"error":...}`
/// in the IPC envelope, dropping the Juggler id for ours.
fn respondFromRaw(a: Allocator, out: *std.array_list.Aligned(u8, null), id: u32, raw: []const u8) !void {
    const parsed = std.json.parseFromSlice(std.json.Value, a, raw, .{}) catch {
        try respondErr(a, out, id, -32603, "bad Juggler response");
        return;
    };
    defer parsed.deinit();
    const root = parsed.value;
    if (root != .object) {
        try respondErr(a, out, id, -32603, "bad Juggler response");
        return;
    }
    if (root.object.get("error")) |e| {
        if (e != .object) {
            try respondErr(a, out, id, -32603, "Juggler error");
            return;
        }
        const code: i64 = blk: {
            if (e.object.get("code")) |c| if (c == .integer) break :blk c.integer;
            break :blk -32000;
        };
        const msg = blk: {
            if (e.object.get("message")) |m| if (m == .string) break :blk m.string;
            break :blk "Juggler error";
        };
        try respondErr(a, out, id, @intCast(code), msg);
        return;
    }
    const result = root.object.get("result") orelse {
        try respondErr(a, out, id, -32603, "Juggler response has no result");
        return;
    };
    const json = try std.json.Stringify.valueAlloc(a, result, .{});
    defer a.free(json);
    try respondOk(a, out, id, json);
}

/// Append one JSON line (newline-terminated) to `out`.
fn writeOut(a: Allocator, out: *std.array_list.Aligned(u8, null), json: []const u8) !void {
    try out.appendSlice(a, json);
    try out.append(a, '\n');
}

fn respondOk(a: Allocator, out: *std.array_list.Aligned(u8, null), id: u32, result_json: []const u8) !void {
    const line = try std.fmt.allocPrint(a, "{{\"id\":{d},\"result\":{s}}}", .{ id, result_json });
    defer a.free(line);
    try writeOut(a, out, line);
}

fn respondErr(a: Allocator, out: *std.array_list.Aligned(u8, null), id: u32, code: i32, message: []const u8) !void {
    const payload = try std.json.Stringify.valueAlloc(a, .{ .code = code, .message = message }, .{});
    defer a.free(payload);
    const line = try std.fmt.allocPrint(a, "{{\"id\":{d},\"error\":{s}}}", .{ id, payload });
    defer a.free(line);
    try writeOut(a, out, line);
}

/// Idle event drain: pull buffered browser data off the fd and forward every
/// complete message upward. Messages are copied out of `pending` BEFORE the
/// driver's pump ever sees them, so nothing is deleted from the driver's
/// buffer and the driver's session/frame/context state stays consistent.
/// `poll_ms` > 0 also waits briefly for events arriving right after the
/// previous response; 0 = nonblocking read (fd already readable).
fn drainEvents(d: *driver_mod.Driver, a: Allocator, poll_ms: ?i32, out: *std.array_list.Aligned(u8, null)) !void {
    if (poll_ms) |t| {
        if (t > 0) {
            var pfd = [_]linux.pollfd{.{ .fd = d.reader.fd, .events = linux.POLL.IN, .revents = 0 }};
            _ = linux.poll(&pfd, pfd.len, t);
            if (pfd[0].revents != 0) try readChunk(d, a);
        } else {
            try readChunk(d, a); // fd already readable (EAGAIN when drained)
        }
    }
    try forwardPending(a, out);
}

/// Read one chunk from the browser fd into the driver's buffer AND into a
/// private copy (`pending`) used for forwarding; the driver never sees the
/// copy, so its pump can consume the same bytes with no interference.
fn readChunk(d: *driver_mod.Driver, a: Allocator) !void {
    var chunk: [chunk_size]u8 = undefined;
    const n = linux.read(d.reader.fd, &chunk, chunk.len);
    switch (linux.errno(n)) {
        .SUCCESS => if (n > 0) {
            const slice = chunk[0..n];
            try d.reader.buf.appendSlice(a, slice);
            try pending.appendSlice(a, slice);
        } else {
            // Clean EOF (read 0): every browser-side write end is closed —
            // the browser exited. Same shutdown signal as POLL.HUP.
            running = false;
        },
        else => {},
    }
}

/// Forward every complete message in `pending`; a trailing partial message
/// stays until the next readChunk completes it. If the driver's own pump
/// read the tail of a split message (its \x00 landed in the driver's buffer,
/// not ours), the partial is stale and is dropped. Event names are passed
/// through VERBATIM (Juggler-native; the CDP Runtime.console ->
/// Console.messageAdded translation was removed in Faz 9).
fn forwardPending(a: Allocator, out: *std.array_list.Aligned(u8, null)) !void {
    while (std.mem.indexOfScalar(u8, pending.items, 0)) |idx| {
        const msg = pending.items[0..idx];
        try emitEvent(a, out, msg);
        const rest = pending.items[idx + 1 ..];
        std.mem.copyForwards(u8, pending.items[0..rest.len], rest);
        pending.shrinkRetainingCapacity(rest.len);
    }
    if (pending.items.len > 0) return;
    pending.clearRetainingCapacity();
}

/// Forward one Juggler event upward, names VERBATIM. Only the sessionId is
/// normalized (null when absent).
fn emitEvent(a: Allocator, out: *std.array_list.Aligned(u8, null), msg: []const u8) !void {
    const parsed = std.json.parseFromSlice(std.json.Value, a, msg, .{}) catch return;
    defer parsed.deinit();
    const root = parsed.value;
    if (root != .object) return;
    const method_v = root.object.get("method") orelse return;
    if (method_v != .string) return;
    const session_v = root.object.get("sessionId");
    const session: ?[]const u8 = if (session_v != null and session_v.? == .string) session_v.?.string else null;
    const params = root.object.get("params") orelse return;

    const Evt = struct { method: []const u8, params: std.json.Value, sessionId: ?[]const u8 = null };
    const json = try std.json.Stringify.valueAlloc(a, Evt{ .method = method_v.string, .params = params, .sessionId = session }, .{ .emit_null_optional_fields = false });
    defer a.free(json);
    try writeOut(a, out, json);
}

/// The page a page-oriented command operates on: the last created/used
/// target (set by Browser.newPage; the caller creates the page explicitly —
/// no implicit about:blank fallback since Faz 9).
fn currentPage(d: *driver_mod.Driver) ?*driver_mod.Page {
    const t = current_target orelse return null;
    return d.pages.get(t);
}

fn setCurrentTarget(a: Allocator, target_id: []const u8) !void {
    if (current_target) |t| a.free(t);
    current_target = try a.dupe(u8, target_id);
}

// === Param helpers ===

fn getStringParam(params: std.json.Value, name: []const u8) ?[]const u8 {
    if (params != .object) return null;
    const v = params.object.get(name) orelse return null;
    if (v != .string) return null;
    return v.string;
}

fn getIntParam(params: std.json.Value, name: []const u8) ?i64 {
    if (params != .object) return null;
    const v = params.object.get(name) orelse return null;
    if (v != .integer) return null;
    return v.integer;
}

fn getNumParam(params: std.json.Value, name: []const u8) ?f64 {
    if (params != .object) return null;
    const v = params.object.get(name) orelse return null;
    return switch (v) {
        .integer => |i| @floatFromInt(i),
        .float => |f| f,
        else => null,
    };
}

fn getBoolParam(params: std.json.Value, name: []const u8) ?bool {
    if (params != .object) return null;
    const v = params.object.get(name) orelse return null;
    if (v != .bool) return null;
    return v.bool;
}

/// CDP-ish remote-object type from the serialized JSON value. ponytail:
/// heuristic on the first byte; the oracle does not consume `type`.
fn jsonType(v: []const u8) []const u8 {
    if (v.len == 0) return "undefined";
    return switch (v[0]) {
        '{' => "object",
        '[' => "array",
        '"' => "string",
        't', 'f' => "boolean",
        'n' => "null",
        else => "number",
    };
}

// === stdin/stdout plumbing ===

/// Read one newline-terminated line from stdin (raw syscalls; no stdio
/// buffering). Returns null on clean EOF.
fn readLine(a: Allocator) !?[]u8 {
    while (true) {
        if (std.mem.indexOfScalar(u8, line_buf.items, '\n')) |idx| {
            const line = try a.dupe(u8, line_buf.items[0..idx]);
            const rest = line_buf.items[idx + 1 ..];
            std.mem.copyForwards(u8, line_buf.items[0..rest.len], rest);
            line_buf.shrinkRetainingCapacity(rest.len);
            return line;
        }
        if (line_buf.items.len >= max_line) return error.LineTooLong;

        var chunk: [chunk_size]u8 = undefined;
        const n = linux.read(0, &chunk, chunk.len);
        switch (linux.errno(n)) {
            .SUCCESS => {
                const len: usize = @intCast(n);
                if (len == 0) {
                    if (line_buf.items.len > 0) {
                        const line = try a.dupe(u8, line_buf.items);
                        line_buf.clearRetainingCapacity();
                        return line;
                    }
                    return null;
                }
                try line_buf.appendSlice(a, chunk[0..len]);
            },
            .INTR => {},
            else => return error.ReadFailed,
        }
    }
}

fn writeAllStdout(bytes: []const u8) !void {
    var off: usize = 0;
    while (off < bytes.len) {
        const n = linux.write(1, bytes[off..].ptr, bytes.len - off);
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

fn ignoreSigpipe() void {
    const set: linux.sigset_t = @splat(0);
    var act = linux.Sigaction{
        .handler = .{ .handler = linux.SIG.IGN },
        .mask = set,
        .flags = 0,
    };
    _ = linux.sigaction(linux.SIG.PIPE, &act, null);
}

// === Router tests (Faz 9 Task 2: Juggler-native method wire) ===

const testing = std.testing;

fn testPipe() ![2]i32 {
    var fds: [2]i32 = undefined;
    if (std.os.linux.errno(std.os.linux.pipe2(&fds, .{})) != .SUCCESS) return error.PipeFailed;
    return fds;
}

/// Fake-browser responder thread: read `expected.len` requests, assert each
/// VERBATIM against `expected`, answer with the matching `replies`, then
/// flush `extra` (post-response events). A failed assert aborts the thread;
/// the main side then finds no response and the test fails by timeout.
const FakePeer = struct {
    fn thread(cmd_read: i32, resp_write: i32, expected: []const []const u8, replies: []const []const u8, extra: []const []const u8) void {
        var r = pipe.Reader.init(cmd_read);
        defer r.deinit(testing.allocator);
        for (expected, 0..) |want, i| {
            const msg = (r.readMessage(testing.allocator, 5000) catch return) orelse return;
            defer testing.allocator.free(msg);
            if (!std.mem.eql(u8, msg, want)) {
                std.debug.print("FAKE peer: expected {s}\nFAKE peer: got      {s}\n", .{ want, msg });
                return;
            }
            pipe.writeMessage(testing.allocator, resp_write, replies[i]) catch return;
        }
        for (extra) |ev| {
            pipe.writeMessage(testing.allocator, resp_write, ev) catch return;
        }
    }
};

/// Two-pipe wire rig: `d` reads `resp[0]`, writes `cmd[1]`; the fake browser
/// reads `cmd[0]` and writes `resp[1]`.
fn testRig() !struct { d: driver_mod.Driver, cmd: [2]i32, resp: [2]i32 } {
    var cmd: [2]i32 = undefined;
    var resp: [2]i32 = undefined;
    if (std.os.linux.errno(std.os.linux.pipe2(&cmd, .{})) != .SUCCESS) return error.PipeFailed;
    if (std.os.linux.errno(std.os.linux.pipe2(&resp, .{})) != .SUCCESS) return error.PipeFailed;
    return .{ .d = driver_mod.Driver.init(testing.allocator, resp[0], cmd[1], false), .cmd = cmd, .resp = resp };
}

/// Preload a page session (attachedToTarget + frameAttached) into the driver.
fn seedPage(d: *driver_mod.Driver, resp: [2]i32, target_id: []const u8, session_id: []const u8, frame_id: []const u8) !void {
    const ev1 = try std.fmt.allocPrint(
        testing.allocator,
        "{{\"method\":\"Browser.attachedToTarget\",\"params\":{{\"sessionId\":\"{s}\",\"targetInfo\":{{\"type\":\"page\",\"targetId\":\"{s}\"}}}}}}",
        .{ session_id, target_id },
    );
    defer testing.allocator.free(ev1);
    try pipe.writeMessage(testing.allocator, resp[1], ev1);
    try d.pump(1000);
    if (frame_id.len > 0) {
        const ev2 = try std.fmt.allocPrint(
            testing.allocator,
            "{{\"method\":\"Page.frameAttached\",\"params\":{{\"frameId\":\"{s}\"}},\"sessionId\":\"{s}\"}}",
            .{ frame_id, session_id },
        );
        defer testing.allocator.free(ev2);
        try pipe.writeMessage(testing.allocator, resp[1], ev2);
        try d.pump(1000);
    }
}

/// Run one request through processRequest, return the buffered response line.
fn runRequest(d: *driver_mod.Driver, line: []const u8) ![]const u8 {
    var out: std.array_list.Aligned(u8, null) = .empty;
    defer out.deinit(testing.allocator);
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    try processRequest(d, testing.allocator, arena.allocator(), line, &out);
    return testing.allocator.dupe(u8, out.items);
}

test "router: Browser.health answered locally (no wire)" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    const line = try runRequest(&d, "{\"id\":7,\"method\":\"Browser.health\",\"params\":{}}");
    defer testing.allocator.free(line);
    // init-built driver has no process-manager instance -> dead.
    try testing.expectEqualStrings("{\"id\":7,\"result\":{\"alive\":false,\"pid\":-1,\"state\":\"dead\"}}\n", line);
}

test "router: page-session method without a page errors -32600" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    // no current target, no sessionId -> -32600, nothing written to the wire
    const line1 = try runRequest(&d, "{\"id\":3,\"method\":\"Page.navigate\",\"params\":{\"url\":\"about:blank\"}}");
    defer testing.allocator.free(line1);
    try testing.expectEqualStrings("{\"id\":3,\"error\":{\"code\":-32600,\"message\":\"no page session\"}}\n", line1);

    // unknown sessionId -> also -32600
    const line2 = try runRequest(&d, "{\"id\":4,\"method\":\"Runtime.evaluate\",\"params\":{\"expression\":\"1+1\"},\"sessionId\":\"nope\"}");
    defer testing.allocator.free(line2);
    try testing.expectEqualStrings("{\"id\":4,\"error\":{\"code\":-32600,\"message\":\"no page session\"}}\n", line2);
}

test "router: Page.navigate forwards to the page session (frameId/loaderId)" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    try seedPage(&d, rig.resp, "t1", "s1", "f1");
    try setCurrentTarget(testing.allocator, "t1");
    defer {
        if (current_target) |t| testing.allocator.free(t);
        current_target = null;
    }

    const expected = [_][]const u8{"{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Page.navigate\",\"params\":{\"frameId\":\"f1\",\"url\":\"about:blank\"}}"};
    const reply = [_][]const u8{"{\"id\":1,\"result\":{\"navigationId\":\"nav-1\"}}"};
    const events = [_][]const u8{
        "{\"method\":\"Page.navigationStarted\",\"params\":{\"frameId\":\"f1\",\"navigationId\":\"nav-1\"},\"sessionId\":\"s1\"}",
        "{\"method\":\"Page.navigationCommitted\",\"params\":{\"frameId\":\"f1\",\"navigationId\":\"nav-1\",\"url\":\"about:blank\",\"name\":\"navigate\"},\"sessionId\":\"s1\"}",
        "{\"method\":\"Page.eventFired\",\"params\":{\"frameId\":\"f1\",\"name\":\"load\"},\"sessionId\":\"s1\"}",
    };
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &events });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":1,\"method\":\"Page.navigate\",\"params\":{\"url\":\"about:blank\"}}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":1,\"result\":{\"frameId\":\"f1\",\"loaderId\":\"nav-1\"}}\n", line);
}

test "router: page-domain method uses the explicit sessionId" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    // page seeded, but NO current target: routing must pick the sessionId
    try seedPage(&d, rig.resp, "t1", "s1", "f1");

    const expected = [_][]const u8{"{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Page.frameTree\",\"params\":{}}"};
    const reply = [_][]const u8{"{\"id\":1,\"result\":{\"frameTree\":{\"frame\":{\"id\":\"f1\"}}}}"};
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &[_][]const u8{} });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":5,\"method\":\"Page.frameTree\",\"params\":{},\"sessionId\":\"s1\"}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":5,\"result\":{\"frameTree\":{\"frame\":{\"id\":\"f1\"}}}}\n", line);
}

test "router: unknown method forwards to root and propagates the Juggler error" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    // Console.enable used to be a local no-op — it must now reach the
    // browser and surface the real -32601 error (no-op removed).
    const expected = [_][]const u8{"{\"id\":1,\"method\":\"Console.enable\",\"params\":{}}"};
    const reply = [_][]const u8{"{\"id\":1,\"error\":{\"code\":-32601,\"message\":\"Method not found: Console.enable\"}}"};
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &[_][]const u8{} });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":6,\"method\":\"Console.enable\",\"params\":{}}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":6,\"error\":{\"code\":-32601,\"message\":\"Method not found: Console.enable\"}}\n", line);
}

test "router: Network.enable is forwarded (no-op removed)" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    // Network.enable is a page-domain method: it routes on the request's
    // sessionId (no-op handler was removed — must reach the browser).
    try seedPage(&d, rig.resp, "t1", "s1", "f1");
    const expected = [_][]const u8{"{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Network.enable\",\"params\":{}}"};
    const reply = [_][]const u8{"{\"id\":1,\"result\":{}}"};
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &[_][]const u8{} });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":9,\"method\":\"Network.enable\",\"params\":{},\"sessionId\":\"s1\"}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":9,\"result\":{}}\n", line);
}

test "router: Browser.* methods go to the root session" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    const expected = [_][]const u8{"{\"id\":1,\"method\":\"Browser.createBrowserContext\",\"params\":{}}"};
    const reply = [_][]const u8{"{\"id\":1,\"result\":{\"browserContextId\":\"ctx-1\"}}"};
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &[_][]const u8{} });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":8,\"method\":\"Browser.createBrowserContext\",\"params\":{}}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":8,\"result\":{\"browserContextId\":\"ctx-1\"}}\n", line);
}

test "router: Runtime.evaluate keeps the evaluateWithRetry path" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    try seedPage(&d, rig.resp, "t1", "s1", "f1");
    try pipe.writeMessage(
        testing.allocator,
        rig.resp[1],
        "{\"method\":\"Runtime.executionContextCreated\",\"params\":{\"executionContextId\":\"ctx-9\",\"auxData\":{\"frameId\":\"f1\"}},\"sessionId\":\"s1\"}",
    );
    try d.pump(1000);
    try setCurrentTarget(testing.allocator, "t1");
    defer {
        if (current_target) |t| testing.allocator.free(t);
        current_target = null;
    }

    const expected = [_][]const u8{"{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Runtime.evaluate\",\"params\":{\"executionContextId\":\"ctx-9\",\"expression\":\"1+1\",\"returnByValue\":true}}"};
    const reply = [_][]const u8{"{\"id\":1,\"result\":{\"result\":{\"type\":\"number\",\"value\":2}}}"};
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &[_][]const u8{} });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":10,\"method\":\"Runtime.evaluate\",\"params\":{\"expression\":\"1+1\"}}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":10,\"result\":{\"result\":{\"type\":\"number\",\"value\":2}}}\n", line);
}

test "router: events forwarded verbatim (no CDP translation)" {
    const a = testing.allocator;
    var out: std.array_list.Aligned(u8, null) = .empty;
    defer out.deinit(a);

    // Runtime.console stays Runtime.console (Console.messageAdded is gone)
    try emitEvent(a, &out, "{\"method\":\"Runtime.console\",\"params\":{\"type\":\"log\"},\"sessionId\":\"s1\"}");
    try testing.expectEqualStrings("{\"method\":\"Runtime.console\",\"params\":{\"type\":\"log\"},\"sessionId\":\"s1\"}\n", out.items);

    out.clearRetainingCapacity();
    // Browser.attachedToTarget stays Browser.* (no Target. rename)
    try emitEvent(a, &out, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try testing.expectEqualStrings("{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}\n", out.items);

    out.clearRetainingCapacity();
    // no sessionId -> field omitted
    try emitEvent(a, &out, "{\"method\":\"Page.eventFired\",\"params\":{\"frameId\":\"f1\",\"name\":\"load\"}}");
    try testing.expectEqualStrings("{\"method\":\"Page.eventFired\",\"params\":{\"frameId\":\"f1\",\"name\":\"load\"}}\n", out.items);
}
