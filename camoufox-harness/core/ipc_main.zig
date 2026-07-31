//! Kahin sidecar: CDP-shaped NL-JSON stdio IPC in front of the Juggler pipe
//! driver (MASTER-PLAN Faz 5). `kahin/the_twins/mirage.py` spawns this
//! binary and speaks newline-delimited JSON over stdin/stdout; this process
//! owns the real Camoufox browser via the driver's Juggler pipe.
//!
//! Build (from camoufox-harness/core):
//!   zig build-exe --dep driver -Mroot=ipc_main.zig -Mdriver=driver.zig \
//!     -O ReleaseSafe -femit-bin=zig-out/bin/kahin-sidecar
//! Usage: kahin-sidecar <firefox-binary> [profile-dir]
//!
//! Wire protocol (one JSON object per line):
//!   req  -> {"id":N,"domain":"Page","command":"navigate","params":{...}}
//!   resp -> {"id":N,"result":{...}} | {"id":N,"error":{"code":-32000,"message":"..."}}
//!   evt  -> {"method":"Target.attachedToTarget","params":{...},"sessionId":"..."}
//!           {"method":"Target.detachedFromTarget","params":{...},"sessionId":"..."}
//!           {"method":"Console.messageAdded","params":{"message":{...}}}
//!
//! CDP-domain -> Juggler mapping (schema: protocol/schema/juggler-schema.json):
//!   Target.createTarget{url}           -> Browser.newPage + Page.navigate
//!   Target.closeTarget{targetId}       -> Page.close (page session)
//!   Target.getTargets                  -> derived from driver page state
//!   Page.navigate{url,referer?}        -> Page.navigate (main frame)
//!   Page.captureScreenshot{format,...} -> Page.screenshot{mimeType,...}
//!   Page.getFrameTree                  -> derived from driver state (main frame)
//!   Runtime.evaluate{expression}       -> Runtime.evaluate (best context)
//!   Emulation.setDeviceMetricsOverride -> Browser.setDefaultViewport
//!   Network.enable / Console.enable    -> no-op (domains do not exist in Juggler)
//!   Browser.*                          -> passthrough on the root session
//!   anything else on a page domain     -> passthrough on the current page session
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

const driver_mod = @import("driver");

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
    const verbose = argv.len >= 3 and std.mem.eql(u8, std.mem.sliceTo(argv[2], 0), "--verbose");
    const prof_arg: ?[]const u8 = if (verbose) (if (argv.len >= 4) std.mem.sliceTo(argv[3], 0) else null) else (if (argv.len >= 3) std.mem.sliceTo(argv[2], 0) else null);
    var prof_buf: [64]u8 = undefined;
    const profile: ?[]const u8 = prof_arg orelse
        std.fmt.bufPrint(&prof_buf, "/tmp/kahin-sidecar-{d}", .{linux.getpid()}) catch "kahin-sidecar-default";

    ignoreSigpipe();

    var gpa = std.heap.DebugAllocator(.{}).init;
    defer _ = gpa.deinit();
    const a = gpa.allocator();

    var d = try driver_mod.Driver.start(a, exe, profile, verbose);
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

        // Idle events flow upward continuously (oracle collectors are async).
        if (pollfds[1].revents & (linux.POLL.IN | linux.POLL.HUP) != 0) {
            drainEvents(&d, a, 0) catch {};
        }
        if (pollfds[0].revents & (linux.POLL.IN | linux.POLL.HUP) != 0) {
            const line = readLine(a) catch null orelse break; // stdin EOF -> take the browser down
            defer a.free(line);

            drainEvents(&d, a, null) catch {};
            processRequest(&d, a, arena.allocator(), line) catch |err| switch (err) {
                error.BrokenPipe => return,
                else => {}, // response already attempted; keep serving
            };
            drainEvents(&d, a, 2) catch {};
        }
    }

    _ = try d.stop();
    if (current_target) |t| a.free(t);
    pending.deinit(a);
    line_buf.deinit(a);
}

fn processRequest(d: *driver_mod.Driver, a: Allocator, aa: Allocator, line: []const u8) !void {
    const parsed = std.json.parseFromSlice(std.json.Value, aa, line, .{}) catch {
        try respondErr(a, 0, -32700, "parse error");
        return;
    };
    const root = parsed.value;
    if (root != .object) {
        try respondErr(a, 0, -32600, "request must be an object");
        return;
    }
    const obj = root.object;
    const id_v = obj.get("id") orelse {
        try respondErr(a, 0, -32600, "missing id");
        return;
    };
    if (id_v != .integer) {
        try respondErr(a, 0, -32600, "id must be an integer");
        return;
    }
    const id: u32 = @intCast(id_v.integer);
    const dom_v = obj.get("domain") orelse {
        try respondErr(a, id, -32600, "missing domain");
        return;
    };
    const cmd_v = obj.get("command") orelse {
        try respondErr(a, id, -32600, "missing command");
        return;
    };
    if (dom_v != .string or cmd_v != .string) {
        try respondErr(a, id, -32600, "domain/command must be strings");
        return;
    }
    const domain = dom_v.string;
    const command = cmd_v.string;
    const params = if (obj.get("params")) |p| p else std.json.Value{ .object = .empty };

    // Domains absent from Juggler: the oracle enables them on start.
    if ((std.mem.eql(u8, domain, "Network") or std.mem.eql(u8, domain, "Console")) and
        std.mem.eql(u8, command, "enable"))
    {
        try respondOk(a, id, "{}");
        return;
    }

    if (std.mem.eql(u8, domain, "Target")) return handleTarget(d, a, id, command, params);
    if (std.mem.eql(u8, domain, "Browser")) return handleBrowser(d, a, id, command, params);
    if (std.mem.eql(u8, domain, "Page")) return handlePage(d, a, id, command, params);
    if (std.mem.eql(u8, domain, "Runtime")) return handleRuntime(d, a, id, command, params);
    if (std.mem.eql(u8, domain, "Emulation")) return handleEmulation(d, a, id, command, params);

    try respondErr(a, id, -32601, "unknown domain");
}

// === Target domain ===

fn handleTarget(d: *driver_mod.Driver, a: Allocator, id: u32, command: []const u8, params: std.json.Value) !void {
    if (std.mem.eql(u8, command, "createTarget")) {
        const url: []const u8 = blk: {
            if (params == .object) if (params.object.get("url")) |u| if (u == .string) break :blk u.string;
            break :blk "about:blank";
        };
        const target_id = try d.newPage(null, url, request_timeout_ms);
        defer a.free(target_id);
        try setCurrentTarget(a, target_id);
        const out = try std.json.Stringify.valueAlloc(a, .{ .targetId = target_id }, .{});
        defer a.free(out);
        return respondOk(a, id, out);
    }
    if (std.mem.eql(u8, command, "closeTarget")) {
        const tid = getStringParam(params, "targetId") orelse {
            try respondErr(a, id, -32602, "missing targetId");
            return;
        };
        const p = d.pages.get(tid) orelse {
            try respondErr(a, id, -32000, "unknown target");
            return;
        };
        var resp = try d.send(p.session_id, "Page.close", "{}", request_timeout_ms);
        defer resp.deinit(a);
        return respondFromRaw(a, id, resp.raw);
    }
    if (std.mem.eql(u8, command, "getTargets")) {
        const Info = struct { targetId: []const u8, type: []const u8 = "page", title: []const u8 = "", url: []const u8 = "", attached: bool = true };
        var infos: std.array_list.Aligned(Info, null) = .empty;
        defer infos.deinit(a);
        var it = d.pages.iterator();
        while (it.next()) |kv| try infos.append(a, .{ .targetId = kv.key_ptr.* });
        const out = try std.json.Stringify.valueAlloc(a, .{ .targetInfos = infos.items }, .{});
        defer a.free(out);
        return respondOk(a, id, out);
    }
    try respondErr(a, id, -32601, "unknown Target command");
}

// === Browser domain ===

fn handleBrowser(d: *driver_mod.Driver, a: Allocator, id: u32, command: []const u8, params: std.json.Value) !void {
    // Handshake already performed inside Driver.start.
    if (std.mem.eql(u8, command, "enable")) return respondOk(a, id, "{}");

    if (std.mem.eql(u8, command, "createBrowserContext")) {
        const ctx = try d.newContext(request_timeout_ms);
        defer a.free(ctx);
        const out = try std.json.Stringify.valueAlloc(a, .{ .browserContextId = ctx }, .{});
        defer a.free(out);
        return respondOk(a, id, out);
    }
    if (std.mem.eql(u8, command, "removeBrowserContext")) {
        const ctx = getStringParam(params, "browserContextId") orelse {
            try respondErr(a, id, -32602, "missing browserContextId");
            return;
        };
        d.removeBrowserContext(ctx, request_timeout_ms) catch {
            try respondErr(a, id, -32000, "removeBrowserContext failed");
            return;
        };
        return respondOk(a, id, "{}");
    }
    if (std.mem.eql(u8, command, "newPage")) {
        const ctx = getStringParam(params, "browserContextId");
        const url = getStringParam(params, "url");
        const target_id = try d.newPage(ctx, url, request_timeout_ms);
        defer a.free(target_id);
        try setCurrentTarget(a, target_id);
        const out = try std.json.Stringify.valueAlloc(a, .{ .targetId = target_id }, .{});
        defer a.free(out);
        return respondOk(a, id, out);
    }
    if (std.mem.eql(u8, command, "close")) {
        d.close(request_timeout_ms) catch {}; // browser exits before replying
        try respondOk(a, id, "{}");
        running = false;
        return;
    }
    if (std.mem.eql(u8, command, "setExtraHTTPHeaders")) {
        return handleSetExtraHTTPHeaders(d, a, id, params);
    }
    return handlePassthrough(d, a, id, "Browser", command, params);
}

fn handleSetExtraHTTPHeaders(d: *driver_mod.Driver, a: Allocator, id: u32, params: std.json.Value) !void {
    // CDP param names match Juggler's {headers:[{name,value}]} verbatim.
    if (params == .object and params.object.get("headers") == null) {
        try respondErr(a, id, -32602, "missing headers");
        return;
    }
    const params_json = try std.json.Stringify.valueAlloc(a, params, .{});
    defer a.free(params_json);
    var resp = d.send(null, "Browser.setExtraHTTPHeaders", params_json, request_timeout_ms) catch {
        try respondErr(a, id, -32000, "setExtraHTTPHeaders failed");
        return;
    };
    defer resp.deinit(a);
    try respondFromRaw(a, id, resp.raw);
}

// === Page domain ===

fn handlePage(d: *driver_mod.Driver, a: Allocator, id: u32, command: []const u8, params: std.json.Value) !void {
    if (std.mem.eql(u8, command, "navigate")) {
        const url = getStringParam(params, "url") orelse {
            try respondErr(a, id, -32602, "missing url");
            return;
        };
        const p = try ensurePage(d, a);
        const nav_id = d.navigate(p.target_id, url, request_timeout_ms) catch |err| switch (err) {
            error.NavigationAborted => {
                try respondErr(a, id, -32000, d.last_abort_text orelse "navigation aborted");
                return;
            },
            else => {
                try respondErr(a, id, -32000, "navigate failed");
                return;
            },
        };
        defer a.free(nav_id);
        const frame_id = p.main_frame_id orelse "";
        const out = try std.json.Stringify.valueAlloc(a, .{ .frameId = frame_id, .loaderId = nav_id }, .{});
        defer a.free(out);
        return respondOk(a, id, out);
    }
    if (std.mem.eql(u8, command, "captureScreenshot")) {
        const p = try ensurePage(d, a);
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
        return respondFromRaw(a, id, resp.raw);
    }
    if (std.mem.eql(u8, command, "getFrameTree")) {
        const p = try ensurePage(d, a);
        const frame_id = p.main_frame_id orelse "";
        const Frame = struct { id: []const u8, name: []const u8 = "", url: []const u8 = "" };
        const Node = struct { frame: Frame, childFrames: []const Frame = &.{} };
        const out = try std.json.Stringify.valueAlloc(a, .{ .frameTree = Node{ .frame = .{ .id = frame_id } } }, .{});
        defer a.free(out);
        return respondOk(a, id, out);
    }
    // Passthrough: any other Page.* maps onto the current page session.
    return handlePassthrough(d, a, id, "Page", command, params);
}

// === Runtime domain ===

fn handleRuntime(d: *driver_mod.Driver, a: Allocator, id: u32, command: []const u8, params: std.json.Value) !void {
    if (std.mem.eql(u8, command, "evaluate")) {
        const expr = getStringParam(params, "expression") orelse {
            try respondErr(a, id, -32602, "missing expression");
            return;
        };
        const p = try ensurePage(d, a);
        var res = evaluateWithRetry(d, p.target_id, expr) catch |err| switch (err) {
            error.NoExecutionContext => {
                try respondErr(a, id, -32000, "no execution context");
                return;
            },
            else => {
                try respondErr(a, id, -32000, "evaluate failed");
                return;
            },
        };
        defer res.deinit(a);

        if (res.exception_text) |et| {
            const out = try std.json.Stringify.valueAlloc(
                a,
                .{
                    .result = .{ .type = "undefined" },
                    .exceptionDetails = .{ .text = et, .stack = res.exception_stack orelse "" },
                },
                .{ .emit_null_optional_fields = false },
            );
            defer a.free(out);
            return respondOk(a, id, out);
        }
        const out = if (res.value_json.len == 0)
            try a.dupe(u8, "{\"result\":{\"type\":\"undefined\"}}")
        else
            try std.fmt.allocPrint(a, "{{\"result\":{{\"type\":\"{s}\",\"value\":{s}}}}}", .{ jsonType(res.value_json), res.value_json });
        defer a.free(out);
        return respondOk(a, id, out);
    }
    return handlePassthrough(d, a, id, "Runtime", command, params);
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

// === Emulation domain ===

fn handleEmulation(d: *driver_mod.Driver, a: Allocator, id: u32, command: []const u8, params: std.json.Value) !void {
    if (std.mem.eql(u8, command, "setDeviceMetricsOverride")) {
        const w = getIntParam(params, "width") orelse {
            try respondErr(a, id, -32602, "missing width");
            return;
        };
        const h = getIntParam(params, "height") orelse {
            try respondErr(a, id, -32602, "missing height");
            return;
        };
        const dsf: f64 = getNumParam(params, "deviceScaleFactor") orelse 1.0;
        const payload = try std.json.Stringify.valueAlloc(
            a,
            .{ .viewport = .{ .viewportSize = .{ .width = w, .height = h }, .deviceScaleFactor = dsf } },
            .{ .emit_null_optional_fields = false },
        );
        defer a.free(payload);
        var resp = try d.send(null, "Browser.setDefaultViewport", payload, request_timeout_ms);
        defer resp.deinit(a);
        return respondFromRaw(a, id, resp.raw);
    }
    try respondErr(a, id, -32601, "unknown Emulation command");
}

// === Shared plumbing ===

/// CDP-shaped request -> raw Juggler call on the root (Browser) or current
/// page session; the Juggler response is re-wrapped in the IPC envelope.
fn handlePassthrough(d: *driver_mod.Driver, a: Allocator, id: u32, domain: []const u8, command: []const u8, params: std.json.Value) !void {
    const session_id: ?[]const u8 = if (std.mem.eql(u8, domain, "Browser"))
        null
    else blk: {
        const p = try ensurePage(d, a);
        break :blk p.session_id;
    };
    const params_json = try std.json.Stringify.valueAlloc(a, params, .{});
    defer a.free(params_json);
    const method = try std.fmt.allocPrint(a, "{s}.{s}", .{ domain, command });
    defer a.free(method);
    var resp = d.send(session_id, method, params_json, request_timeout_ms) catch {
        try respondErr(a, id, -32000, "Juggler call failed");
        return;
    };
    defer resp.deinit(a);
    try respondFromRaw(a, id, resp.raw);
}

/// Wrap a Juggler response `{"id":N,"result":...}` / `{"id":N,"error":...}`
/// in the IPC envelope, dropping the Juggler id for ours.
fn respondFromRaw(a: Allocator, id: u32, raw: []const u8) !void {
    const parsed = std.json.parseFromSlice(std.json.Value, a, raw, .{}) catch {
        try respondErr(a, id, -32603, "bad Juggler response");
        return;
    };
    defer parsed.deinit();
    const root = parsed.value;
    if (root != .object) {
        try respondErr(a, id, -32603, "bad Juggler response");
        return;
    }
    if (root.object.get("error")) |e| {
        if (e != .object) {
            try respondErr(a, id, -32603, "Juggler error");
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
        try respondErr(a, id, @intCast(code), msg);
        return;
    }
    const result = root.object.get("result") orelse {
        try respondErr(a, id, -32603, "Juggler response has no result");
        return;
    };
    const out = try std.json.Stringify.valueAlloc(a, result, .{});
    defer a.free(out);
    try respondOk(a, id, out);
}

/// Write one JSON line (newline-terminated).
fn writeOut(a: Allocator, json: []const u8) !void {
    const line = try std.fmt.allocPrint(a, "{s}\n", .{json});
    defer a.free(line);
    try writeAllStdout(line);
}

fn respondOk(a: Allocator, id: u32, result_json: []const u8) !void {
    const out = try std.fmt.allocPrint(a, "{{\"id\":{d},\"result\":{s}}}\n", .{ id, result_json });
    defer a.free(out);
    try writeAllStdout(out);
}

fn respondErr(a: Allocator, id: u32, code: i32, message: []const u8) !void {
    const payload = try std.json.Stringify.valueAlloc(a, .{ .code = code, .message = message }, .{});
    defer a.free(payload);
    const out = try std.fmt.allocPrint(a, "{{\"id\":{d},\"error\":{s}}}\n", .{ id, payload });
    defer a.free(out);
    try writeAllStdout(out);
}

/// Idle event drain: pull buffered browser data off the fd and forward every
/// complete message upward. Messages are copied out of `pending` BEFORE the
/// driver's pump ever sees them, so nothing is deleted from the driver's
/// buffer and the driver's session/frame/context state stays consistent.
/// `poll_ms` > 0 also waits briefly for events arriving right after the
/// previous response; 0 = nonblocking read (fd already readable).
fn drainEvents(d: *driver_mod.Driver, a: Allocator, poll_ms: ?i32) !void {
    if (poll_ms) |t| {
        if (t > 0) {
            var pfd = [_]linux.pollfd{.{ .fd = d.reader.fd, .events = linux.POLL.IN, .revents = 0 }};
            _ = linux.poll(&pfd, pfd.len, t);
            if (pfd[0].revents != 0) try readChunk(d, a);
        } else {
            try readChunk(d, a); // fd already readable (EAGAIN when drained)
        }
    }
    try forwardPending(d, a);
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
        },
        else => {},
    }
}

/// Forward every complete message in `pending`; a trailing partial message
/// stays until the next readChunk completes it. If the driver's own pump
/// read the tail of a split message (its \x00 landed in the driver's buffer,
/// not ours), the partial is stale and is dropped.
fn forwardPending(d: *driver_mod.Driver, a: Allocator) !void {
    if (pending.items.len > 0 and
        (d.reader.buf.items.len < pending.items.len or
        !std.mem.eql(u8, d.reader.buf.items[0..pending.items.len], pending.items)))
    {
        pending.clearRetainingCapacity();
    }
    while (std.mem.indexOfScalar(u8, pending.items, 0)) |idx| {
        const msg = pending.items[0..idx];
        try emitEvent(a, msg);
        const rest = pending.items[idx + 1 ..];
        std.mem.copyForwards(u8, pending.items[0..rest.len], rest);
        pending.shrinkRetainingCapacity(rest.len);
    }
}

fn emitEvent(a: Allocator, msg: []const u8) !void {
    const parsed = std.json.parseFromSlice(std.json.Value, a, msg, .{}) catch return;
    defer parsed.deinit();
    const root = parsed.value;
    if (root != .object) return;
    const method_v = root.object.get("method") orelse return;
    if (method_v != .string) return;
    const session_v = root.object.get("sessionId");
    const session: ?[]const u8 = if (session_v != null and session_v.? == .string) session_v.?.string else null;
    const params = root.object.get("params") orelse return;
    const params_v = params;

    const Evt = struct { method: []const u8, params: std.json.Value, sessionId: ?[]const u8 = null };

    if (std.mem.eql(u8, method_v.string, "Runtime.console")) {
        try emitConsole(a, params_v, session);
        return;
    }
    const name: []const u8 = if (std.mem.eql(u8, method_v.string, "Browser.attachedToTarget"))
        "Target.attachedToTarget"
    else if (std.mem.eql(u8, method_v.string, "Browser.detachedFromTarget"))
        "Target.detachedFromTarget"
    else
        method_v.string;

    const out = try std.json.Stringify.valueAlloc(a, Evt{ .method = name, .params = params_v, .sessionId = session }, .{ .emit_null_optional_fields = false });
    defer a.free(out);
    try writeOut(a, out);
}

/// Runtime.console {executionContextId, args, type, location{url,lineNumber,columnNumber}}
/// -> Console.messageAdded {message:{type,args,url,line,column,executionContextId}}.
fn emitConsole(a: Allocator, params: std.json.Value, session: ?[]const u8) !void {
    _ = session;
    if (params != .object) return;
    const type_s: []const u8 = if (params.object.get("type")) |t| (if (t == .string) t.string else "") else "";
    const args = if (params.object.get("args")) |av| av else std.json.Value{ .null = {} };
    const ctx_s: []const u8 = if (params.object.get("executionContextId")) |c| (if (c == .string) c.string else "") else "";
    var url: ?[]const u8 = null;
    var line: ?i64 = null;
    var column: ?i64 = null;
    if (params.object.get("location")) |loc| {
        if (loc == .object) {
            if (loc.object.get("url")) |u| {
                if (u == .string) url = u.string;
            }
            if (loc.object.get("lineNumber")) |l| {
                if (l == .integer) line = l.integer;
            }
            if (loc.object.get("columnNumber")) |c| {
                if (c == .integer) column = c.integer;
            }
        }
    }
    const Msg = struct {
        type: []const u8,
        args: std.json.Value,
        url: ?[]const u8 = null,
        line: ?i64 = null,
        column: ?i64 = null,
        executionContextId: []const u8,
    };
    const P = struct { message: Msg };
    const out = try std.json.Stringify.valueAlloc(
        a,
        P{ .message = .{ .type = type_s, .args = args, .url = url, .line = line, .column = column, .executionContextId = ctx_s } },
        .{ .emit_null_optional_fields = false },
    );
    defer a.free(out);
    try writeOut(a, out);
}

/// The page commands operate on: the last created/used target, or a fresh
/// about:blank page when nothing exists yet (oracle's start-then-navigate
/// flow works without an explicit createTarget).
fn ensurePage(d: *driver_mod.Driver, a: Allocator) !*driver_mod.Page {
    if (currentPage(d)) |p| return p;
    const target_id = try d.newPage(null, null, request_timeout_ms);
    defer a.free(target_id);
    try setCurrentTarget(a, target_id);
    return currentPage(d) orelse error.NoPage;
}

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
