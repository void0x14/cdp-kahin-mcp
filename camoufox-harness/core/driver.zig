//! Faz 2 driver: orchestration layer over the Juggler pipe transport.
//!
//! Spawns Camoufox, performs the Browser.enable handshake, then exposes the
//! minimum live surface: browser contexts, pages, navigation with lifecycle
//! tracking, and Runtime.evaluate. Wire format stays Juggler (no CDP shim);
//! the internal call language is CDP-shaped: send(session, method, params).
//!
//! Session model: `Browser.attachedToTarget` events register new page
//! sessions (targetId -> sessionId map in `pages`/`by_session`). Events
//! without a sessionId belong to the root (browser) session.
//!
//! All reads funnel through `pump`, which routes responses to their pending
//! caller and feeds events into the page state (sessions, frames, execution
//! contexts, navigation lifecycle) — so synchronous calls never miss the
//! events they depend on.

const std = @import("std");
const Allocator = std.mem.Allocator;

const pipe = @import("src/transport/pipe.zig");
const session = @import("src/transport/session.zig");
const browser = @import("adapters/browser.zig");
const page = @import("adapters/page.zig");
const runtime = @import("adapters/runtime.zig");
const console = @import("adapters/console.zig");
const emulation = @import("adapters/emulation.zig");
const network = @import("adapters/network.zig");
const target = @import("adapters/target.zig");

pub const default_timeout_ms: i32 = 30_000;
pub const enable_timeout_ms: i32 = 30_000;

/// Cap on retained console messages / network events (ring drop of the
/// oldest). Kahin's own collectors keep bounded lists too.
const max_console_messages: usize = 2_000;
const max_network_events: usize = 5_000;

/// One response message returned by `send`; `raw` is an owned copy.
pub const SendResult = struct {
    is_error: bool,
    raw: []u8,

    pub fn deinit(self: *SendResult, allocator: Allocator) void {
        allocator.free(self.raw);
        self.* = undefined;
    }
};

const Pending = struct {
    done: bool = false,
    is_error: bool = false,
    raw: ?[]u8 = null,
};

const ContextInfo = struct {
    id: []u8,
    frame_id: ?[]u8,

    fn deinit(self: *ContextInfo, allocator: Allocator) void {
        allocator.free(self.id);
        if (self.frame_id) |f| allocator.free(f);
    }
};

/// One frame of the frame registry (Faz 3): id is the map key, parent/url
/// are owned copies. `parent_id == null` marks the main frame.
const FrameEntry = struct {
    parent_id: ?[]u8,
    url: ?[]u8,

    fn deinit(self: *FrameEntry, allocator: Allocator) void {
        if (self.parent_id) |p| allocator.free(p);
        if (self.url) |u| allocator.free(u);
    }
};

/// Per-page state, keyed by target id.
pub const Page = struct {
    /// Aliases the `pages` map key buffer.
    target_id: []u8,
    /// Session id from Browser.attachedToTarget; "" until attached.
    session_id: []u8,
    /// Owning context from Browser.attachedToTarget targetInfo (optional).
    browser_context_id: ?[]u8,
    /// Owned copy of the main frame id (the registry's parentless frame).
    main_frame_id: ?[]u8,
    /// Page-level URL: url of the last main-frame navigationCommitted /
    /// sameDocumentNavigation (owned). "" until first commit.
    current_url: ?[]u8,
    /// frameId -> FrameEntry (owning keys and values).
    frames: std.StringHashMap(FrameEntry),
    lifecycle: page.Lifecycle,
    contexts: std.array_list.Aligned(ContextInfo, null) = .empty,
};

/// Monotonic time in ms (std.time.milliTimestamp removed in 0.16; raw
/// syscall keeps this dependency-free).
fn nowMs() i64 {
    var ts: std.os.linux.timespec = undefined;
    _ = std.os.linux.clock_gettime(std.os.linux.CLOCK.MONOTONIC, &ts);
    return @as(i64, @intCast(ts.sec)) * std.time.ms_per_s + @divTrunc(@as(i64, @intCast(ts.nsec)), std.time.ns_per_ms);
}

pub const Driver = struct {
    allocator: Allocator,
    verbose: bool,
    child: pipe.Spawned,
    reader: pipe.Reader,
    router: session.Router,
    /// targetId -> Page (owns the Page structs).
    pages: std.StringHashMap(*Page),
    /// sessionId -> Page (same pointers as `pages`; keys freed in deinit).
    by_session: std.StringHashMap(*Page),
    /// errorText of the last aborted navigation (owned; freed on next use).
    last_abort_text: ?[]u8 = null,
    /// Normalized console messages (Runtime.console -> Console.messageAdded).
    console_messages: std.array_list.Aligned(console.Message, null) = .empty,
    /// Raw Network.* event JSON strings (passive passthrough, Faz 3).
    network_events: std.array_list.Aligned([]u8, null) = .empty,

    /// Wire a Driver onto existing fds (no spawn). Used by tests with a
    /// self-pipe; `start` spawns the browser and calls this.
    pub fn init(allocator: Allocator, read_fd: i32, write_fd: i32, verbose: bool) Driver {
        var d = Driver{
            .allocator = allocator,
            .verbose = verbose,
            .child = .{ .pid = -1, .read_fd = read_fd, .write_fd = write_fd },
            .reader = pipe.Reader.init(read_fd),
            .router = session.Router.init(allocator),
            .pages = std.StringHashMap(*Page).init(allocator),
            .by_session = std.StringHashMap(*Page).init(allocator),
        };
        d.router.registerSession(.{ .id = "", .target_type = "browser", .name = "root" }) catch unreachable;
        return d;
    }

    /// Spawn Camoufox with a Juggler pipe, then perform the Browser.enable
    /// handshake on the root session. `profile` must be a directory that
    /// exists (Firefox stalls otherwise) — it is created here if missing.
    pub fn start(allocator: Allocator, exe: []const u8, profile: ?[]const u8, verbose: bool) !Driver {
        const profile_path = try ensureProfile(allocator, profile);
        // ponytail: profile_path is arena-owned by the caller (or leaked if
        // caller passes a stack slice); argv lifetime ends at spawn.

        const browser_argv = [_][]const u8{
            exe,
            "-juggler-pipe",
            "-profile",
            profile_path,
            "-no-remote",
            "-headless",
        };

        var d = Driver.init(allocator, -1, -1, verbose);
        errdefer {
            // (Faz 2 Minor b) Error path must not leak the child: closing the
            // fds makes the browser exit, then reap it. `wait` after `closeFds`
            // is safe — the browser dies on pipe EOF.
            pipe.closeFds(&d.child);
            if (d.child.pid > 0) _ = pipe.wait(&d.child) catch {};
            d.deinit();
        }

        d.child = try pipe.spawn(allocator, &browser_argv);
        d.reader = pipe.Reader.init(d.child.read_fd);

        const params = try browser.enableParams(allocator, true);
        defer allocator.free(params);
        var resp = try d.send(null, browser.method_enable, params, enable_timeout_ms);
        defer resp.deinit(allocator);
        if (resp.is_error) return error.HandshakeFailed;
        return d;
    }
    pub fn deinit(self: *Driver) void {
        // `self.* = undefined` below poisons the struct before defers run, so
        // capture the allocator up front.
        const allocator = self.allocator;
        // Two passes: collect page pointers first, then free keys (the map
        // iterator must not run while its keys are being freed).
        var pages: std.array_list.Aligned(*Page, null) = .empty;
        defer pages.deinit(allocator);
        var it = self.pages.iterator();
        while (it.next()) |kv| pages.append(allocator, kv.value_ptr.*) catch {};
        for (pages.items) |p| self.freePage(p);
        self.pages.deinit();

        // by_session owns the session id buffers; Page.session_id aliases them.
        var sit = self.by_session.iterator();
        while (sit.next()) |kv| self.allocator.free(kv.key_ptr.*);
        self.by_session.deinit();

        for (self.console_messages.items) |*m| m.deinit(allocator);
        self.console_messages.deinit(allocator);
        for (self.network_events.items) |raw| allocator.free(raw);
        self.network_events.deinit(allocator);

        if (self.last_abort_text) |t| self.allocator.free(t);
        self.router.deinit();
        self.reader.deinit(self.allocator);
        self.* = undefined;
    }

    fn freePage(self: *Driver, p: *Page) void {
        for (p.contexts.items) |*c| c.deinit(self.allocator);
        p.contexts.deinit(self.allocator);
        if (p.main_frame_id) |f| self.allocator.free(f);
        if (p.browser_context_id) |b| self.allocator.free(b);
        if (p.current_url) |u| self.allocator.free(u);
        var fit = p.frames.iterator();
        while (fit.next()) |kv| {
            self.allocator.free(kv.key_ptr.*);
            kv.value_ptr.deinit(self.allocator);
        }
        p.frames.deinit();
        p.lifecycle.deinit();
        // p.session_id aliases the by_session key; that map owns it.
        self.allocator.free(p.target_id); // aliases the map key
        self.allocator.destroy(p);
    }

    /// CDP-shaped call: send one request (optionally to a session), pump
    /// until the matching response. Returns an owned copy of the response.
    pub fn send(
        self: *Driver,
        session_id: ?[]const u8,
        method: []const u8,
        params_json: []const u8,
        timeout_ms: i32,
    ) !SendResult {
        const id = self.router.nextId();
        var pend = Pending{};
        try self.router.registerPending(id, @ptrCast(&pend));
        errdefer _ = self.router.pending.fetchRemove(id);

        const req = try buildRequest(self.allocator, id, session_id, method, params_json);
        defer self.allocator.free(req);
        if (self.verbose) std.debug.print("SENT: {s}\n", .{req});

        pipe.writeMessage(self.allocator, self.child.write_fd, req) catch |err| switch (err) {
            error.BrokenPipe, error.WriteZero => return error.BrowserDead,
            else => return err,
        };

        const deadline = nowMs() + timeout_ms;
        while (!pend.done) {
            const rem = remainingMs(deadline) orelse return error.WaitTimeout;
            try self.pump(rem);
        }
        return .{ .is_error = pend.is_error, .raw = pend.raw.? };
    }

    /// Read + route one message. Responses resolve pending callers; events
    /// update session/frame/context/lifecycle state. Timeout maps to
    /// error.WaitTimeout.
    pub fn pump(self: *Driver, timeout_ms: i32) !void {
        const raw = self.reader.readMessage(self.allocator, timeout_ms) catch |err| switch (err) {
            error.Timeout => return error.WaitTimeout,
            else => return err,
        } orelse return error.BrowserClosed;
        defer self.allocator.free(raw);
        if (self.verbose) std.debug.print("RECV: {s}\n", .{raw});

        switch (try self.router.dispatch(raw)) {
            .response => |resp| {
                const pend: *Pending = @ptrCast(@alignCast(resp.context));
                pend.done = true;
                pend.is_error = resp.is_error;
                if (pend.raw == null) pend.raw = try self.allocator.dupe(u8, resp.raw);
            },
            .event => |ev| {
                if (self.verbose) std.debug.print("EVENT: {s} session={?s}\n", .{ ev.method, ev.session_id });
                try self.handleEvent(ev);
            },
            .invalid => {},
        }
    }

    /// Browser.createBrowserContext -> owned browserContextId.
    pub fn newContext(self: *Driver, timeout_ms: i32) ![]u8 {
        const params = try browser.createBrowserContextParams(self.allocator, null);
        defer self.allocator.free(params);
        var resp = try self.send(null, browser.method_create_browser_context, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
        return browser.parseBrowserContextId(self.allocator, resp.raw);
    }

    /// Browser.newPage; waits until the attachedToTarget event registered the
    /// page session. If `url` is given, navigates and waits for load. Note:
    /// Juggler's Browser.newPage has NO url param (schema fact) — navigation
    /// is a separate Page.navigate call.
    pub fn newPage(self: *Driver, browser_context_id: ?[]const u8, url: ?[]const u8, timeout_ms: i32) ![]u8 {
        const params = try browser.newPageParams(self.allocator, browser_context_id);
        defer self.allocator.free(params);
        var resp = try self.send(null, browser.method_new_page, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
        const target_id = try browser.parseTargetId(self.allocator, resp.raw);
        errdefer self.allocator.free(target_id);

        // The attachedToTarget event may precede or follow the response.
        const deadline = nowMs() + timeout_ms;
        while (true) {
            const p = self.pages.get(target_id) orelse {
                const rem = remainingMs(deadline) orelse return error.TargetNotAttached;
                try self.pump(rem);
                continue;
            };
            if (p.session_id.len > 0) break;
            const rem = remainingMs(deadline) orelse return error.TargetNotAttached;
            try self.pump(rem);
        }

        if (url) |u| {
            const nav_id = try self.navigate(target_id, u, timeout_ms);
            self.allocator.free(nav_id);
        }
        return target_id;
    }

    /// Browser.removeBrowserContext.
    pub fn removeBrowserContext(self: *Driver, browser_context_id: []const u8, timeout_ms: i32) !void {
        const params = try browser.removeBrowserContextParams(self.allocator, browser_context_id);
        defer self.allocator.free(params);
        var resp = try self.send(null, browser.method_remove_browser_context, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
    }

    /// Browser.setExtraHTTPHeaders.
    pub fn setExtraHTTPHeaders(self: *Driver, browser_context_id: ?[]const u8, headers: []const browser.Header, timeout_ms: i32) !void {
        const params = try browser.setExtraHTTPHeadersParams(self.allocator, browser_context_id, headers);
        defer self.allocator.free(params);
        var resp = try self.send(null, browser.method_set_extra_http_headers, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
    }

    /// Browser.close (root session).
    pub fn close(self: *Driver, timeout_ms: i32) !void {
        var resp = try self.send(null, browser.method_close, browser.closeParams(), timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
    }

    /// Page.navigate on the page's main frame, waiting for the load
    /// lifecycle event. Returns an owned navigationId.
    ///
    /// (Faz 2 Minor a fix) The lifecycle is keyed on the frame id only, so a
    /// stale eventFired from the previous document (e.g. the initial
    /// about:blank load racing our navigate) can complete it early. The
    /// return is therefore gated on the navigation being genuinely done
    /// (navigationSatisfied): the started event for our navigation id, or
    /// the committed URL matching the target — a screenshot right after
    /// navigate() captures the NEW document. Redirects pass via the started
    /// event (final URL may differ).
    pub fn navigate(self: *Driver, target_id: []const u8, url: []const u8, timeout_ms: i32) ![]u8 {
        const p = self.pages.get(target_id) orelse return error.UnknownTarget;
        if (p.session_id.len == 0) return error.TargetNotAttached;
        try self.waitForMainFrame(p, nowMs() + timeout_ms);

        p.lifecycle.begin(p.main_frame_id.?);
        const params = try page.navigateParams(self.allocator, p.main_frame_id.?, url, null);
        defer self.allocator.free(params);
        var resp = try self.send(p.session_id, page.method_navigate, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;

        const nav_id = try page.parseNavigationId(self.allocator, resp.raw);
        errdefer self.allocator.free(nav_id);
        p.lifecycle.setNavigationId(p.main_frame_id.?, nav_id);

        const deadline = nowMs() + timeout_ms;
        try self.waitForLoad(p, deadline);
        // Return gate: keep pumping until the navigation is genuinely done
        // (see navigationSatisfied).
        while (p.lifecycle.state != .aborted and !self.navigationSatisfied(p, nav_id, url)) {
            if (p.lifecycle.state == .waiting) {
                try self.waitForLoad(p, deadline);
            } else {
                const rem = remainingMs(deadline) orelse return error.WaitTimeout;
                try self.pump(rem);
            }
        }
        if (p.lifecycle.state == .aborted) {
            if (self.last_abort_text) |t| self.allocator.free(t);
            self.last_abort_text = try self.allocator.dupe(u8, p.lifecycle.abort_text);
            return error.NavigationAborted;
        }
        return nav_id;
    }

    /// The navigate return gate (Faz 2 Minor a): the lifecycle alone
    /// completes early on a stale load event from the previous document.
    /// Satisfaction requires the done state plus:
    ///   - our navigation started event was observed (the browser accepted
    ///     OUR navigation), AND
    ///   - our navigation committed (the new document exists — its contexts
    ///     are live, so a follow-up evaluate/screenshot cannot hit the
    ///     previous document), or the committed URL matches the requested
    ///     one (same-document navigations).
    /// A newer navigation superseding ours passes on URL match only.
    fn navigationSatisfied(self: *Driver, p: *Page, nav_id: []const u8, url: []const u8) bool {
        if (p.lifecycle.state != .done) return false;
        if (p.lifecycle.navigation_id.len == 0 or std.mem.eql(u8, p.lifecycle.navigation_id, nav_id)) {
            if (p.lifecycle.nav_started) {
                return p.lifecycle.committed_current or self.urlCommitted(p, url);
            }
            return self.urlCommitted(p, url);
        }
        return self.urlCommitted(p, url);
    }

    /// True when the page's committed URL equals `url` ("" while unknown —
    /// the gate keeps pumping).
    fn urlCommitted(self: *Driver, p: *Page, url: []const u8) bool {
        _ = self;
        if (p.current_url) |u| return std.mem.eql(u8, u, url);
        return false;
    }

    /// Runtime.evaluate on the page's (main-frame preferred) execution
    /// context. Returns an owned EvaluateResult.
    pub fn evaluate(self: *Driver, target_id: []const u8, expression: []const u8, timeout_ms: i32) !runtime.EvaluateResult {
        const p = self.pages.get(target_id) orelse return error.UnknownTarget;
        if (p.session_id.len == 0) return error.TargetNotAttached;
        try self.waitForContext(p, nowMs() + timeout_ms);
        const ctx = pickContext(p) orelse return error.NoExecutionContext;

        const params = try runtime.evaluateParams(self.allocator, ctx.id, expression, true);
        defer self.allocator.free(params);
        var resp = try self.send(p.session_id, runtime.method_evaluate, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
        return runtime.parseEvaluateResult(self.allocator, resp.raw);
    }

    /// Target.getTargets equivalent (no wire call — Juggler has no Target
    /// domain): CDP-shaped target list from the driver's page map.
    /// Returns owned JSON: {"targetInfos":[{targetId,type:"page",
    /// browserContextId?,url}]}.
    pub fn getTargets(self: *Driver) ![]u8 {
        var infos: std.array_list.Aligned(target.TargetInfo, null) = .empty;
        defer infos.deinit(self.allocator);
        var it = self.pages.iterator();
        while (it.next()) |kv| {
            const p = kv.value_ptr.*;
            try infos.append(self.allocator, .{
                .targetId = p.target_id,
                .url = p.current_url orelse "",
                .browserContextId = p.browser_context_id,
            });
        }
        return target.buildTargetInfos(self.allocator, infos.items);
    }

    /// Target.createTarget equivalent: Browser.newPage on the default
    /// context (Juggler's newPage has no url param — the url is a separate
    /// Page.navigate inside newPage's wrapper) then navigate. Returns the
    /// CDP result {"targetId": "..."}.
    pub fn createTarget(self: *Driver, url: []const u8, timeout_ms: i32) ![]u8 {
        const target_id = try self.newPage(null, url, timeout_ms);
        defer self.allocator.free(target_id);
        return target.buildCreateTargetResult(self.allocator, target_id);
    }

    /// Target.closeTarget equivalent: Page.close (schema) on the target's
    /// session, then wait for Browser.detachedFromTarget to clean the state.
    /// Result: "{}" (CDP shape).
    pub fn closeTarget(self: *Driver, target_id: []const u8, timeout_ms: i32) ![]u8 {
        const p = self.pages.get(target_id) orelse return error.UnknownTarget;
        if (p.session_id.len == 0) return error.TargetNotAttached;
        var resp = try self.send(p.session_id, page.method_close, page.closeParams(), timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
        const deadline = nowMs() + timeout_ms;
        while (self.pages.get(target_id) != null) {
            const rem = remainingMs(deadline) orelse return error.WaitTimeout;
            try self.pump(rem);
        }
        return self.allocator.dupe(u8, target.closeTargetResult());
    }

    /// Emulation.setDeviceMetricsOverride equivalent:
    /// Browser.setDefaultViewport (schema). Context-wide; per-page size
    /// changes go through setViewportSize.
    pub fn setDefaultViewport(
        self: *Driver,
        browser_context_id: ?[]const u8,
        width: f64,
        height: f64,
        device_scale_factor: ?f64,
        timeout_ms: i32,
    ) !void {
        const params = try emulation.setDefaultViewportParams(self.allocator, browser_context_id, .{
            .viewportSize = .{ .width = width, .height = height },
            .deviceScaleFactor = device_scale_factor,
        });
        defer self.allocator.free(params);
        var resp = try self.send(null, emulation.method_set_default_viewport, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
    }

    /// Page.setViewportSize on the target's session.
    pub fn setViewportSize(self: *Driver, target_id: []const u8, width: f64, height: f64, timeout_ms: i32) !void {
        const p = self.pages.get(target_id) orelse return error.UnknownTarget;
        if (p.session_id.len == 0) return error.TargetNotAttached;
        const params = try emulation.setViewportSizeParams(self.allocator, .{ .width = width, .height = height });
        defer self.allocator.free(params);
        var resp = try self.send(p.session_id, emulation.method_set_viewport_size, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
    }

    /// Page.captureScreenshot equivalent: Page.screenshot (schema).
    /// Returns RAW image bytes (the Juggler `data` string is base64 and is
    /// decoded here).
    ///
    /// The `clip` parameter is REQUIRED in this Juggler build — the schema
    /// marks it mandatory (no `optional` flag; observed: "Object
    /// \"<root>.clip\" is undefined, but has some scheme" when omitted). The
    /// clip is computed from the page itself: viewport size via
    /// window.innerWidth/Height, full-page size via
    /// documentElement.scrollWidth/Height (Juggler has no getLayoutMetrics;
    /// this evaluate is the schema-faithful substitute).
    pub fn screenshot(self: *Driver, target_id: []const u8, full_page: bool, timeout_ms: i32) ![]u8 {
        const p = self.pages.get(target_id) orelse return error.UnknownTarget;
        if (p.session_id.len == 0) return error.TargetNotAttached;
        try self.waitForMainFrame(p, nowMs() + timeout_ms);

        const expr = if (full_page)
            "[document.documentElement.scrollWidth, document.documentElement.scrollHeight]"
        else
            "[window.innerWidth, window.innerHeight]";
        const size = try self.evalSize(p, expr, timeout_ms);
        if (size.w <= 0 or size.h <= 0) return error.SizeUnavailable;

        const params = try page.screenshotParams(self.allocator, "image/png", .{ .width = size.w, .height = size.h }, null, null);
        defer self.allocator.free(params);
        var resp = try self.send(p.session_id, page.method_screenshot, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
        const b64 = try page.parseScreenshotData(self.allocator, resp.raw);
        defer self.allocator.free(b64);
        return page.decodeScreenshot(self.allocator, b64);
    }

    /// Evaluate a [width, height] pair in the page (viewport or full
    /// content size).
    fn evalSize(self: *Driver, p: *Page, expr: []const u8, timeout_ms: i32) !struct { w: f64, h: f64 } {
        var res = try self.evaluate(p.target_id, expr, timeout_ms);
        defer res.deinit(self.allocator);
        if (res.exception_text != null or res.value_json.len == 0) return error.SizeUnavailable;
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, res.value_json, .{});
        defer parsed.deinit();
        const v = parsed.value;
        if (v != .array or v.array.items.len < 2) return error.SizeUnavailable;
        const w = numF64(v.array.items[0]) orelse return error.SizeUnavailable;
        const h = numF64(v.array.items[1]) orelse return error.SizeUnavailable;
        return .{ .w = w, .h = h };
    }

    /// Page.getFrameTree equivalent (no wire call — Juggler has no
    /// getFrameTree): nested tree from the frame registry, CDP shape:
    /// {"frameTree":{"frame":{"id","parentId"?,"url"},"childFrames":[...]}}.
    pub fn getFrameTree(self: *Driver, target_id: []const u8) ![]u8 {
        const p = self.pages.get(target_id) orelse return error.UnknownTarget;
        return buildFrameTreeJson(self.allocator, p);
    }

    /// Console messages normalized to the Console.messageAdded
    /// params.message shape, as a JSON array.
    pub fn getConsoleMessages(self: *Driver) ![]u8 {
        var out: std.Io.Writer.Allocating = .init(self.allocator);
        defer out.deinit();
        const w = &out.writer;
        try w.writeAll("[");
        for (self.console_messages.items, 0..) |*m, i| {
            if (i > 0) try w.writeAll(",");
            const j = try console.toJson(self.allocator, m);
            defer self.allocator.free(j);
            try w.writeAll(j);
        }
        try w.writeAll("]");
        return self.allocator.dupe(u8, out.written());
    }

    /// Raw Network.* events (passive passthrough, no interception) as a JSON
    /// array of the original wire events, newest last. `limit` 0 = all.
    pub fn getNetworkEvents(self: *Driver, limit: usize) ![]u8 {
        const n = @min(limit, self.network_events.items.len);
        const first = self.network_events.items.len - n;
        var out: std.Io.Writer.Allocating = .init(self.allocator);
        defer out.deinit();
        const w = &out.writer;
        try w.writeAll("[");
        for (self.network_events.items[first..], 0..) |raw, i| {
            if (i > 0) try w.writeAll(",");
            try w.writeAll(raw);
        }
        try w.writeAll("]");
        return self.allocator.dupe(u8, out.written());
    }

    /// Pump until at least one console message arrived (smoke helper).
    pub fn waitForConsole(self: *Driver, timeout_ms: i32) !void {
        const deadline = nowMs() + timeout_ms;
        while (self.console_messages.items.len == 0) {
            const rem = remainingMs(deadline) orelse return error.WaitTimeout;
            try self.pump(rem);
        }
    }

    /// Console.enable equivalent — no-op by design: Juggler has no Console
    /// domain; Runtime.console events flow unconditionally (schema fact).
    pub fn enableConsole(self: *Driver) void {
        _ = self;
    }

    /// Network.enable equivalent — no-op by design: Juggler emits Network.*
    /// events unconditionally; there is no Network.enable in the schema.
    pub fn enableNetwork(self: *Driver) void {
        _ = self;
    }

    /// Runtime.enable equivalent — no-op by design: no Runtime.enable in the
    /// schema; executionContextCreated/Destroyed events flow unconditionally
    /// (observed in Faz 2 smoke).
    pub fn enableRuntime(self: *Driver) void {
        _ = self;
    }

    /// Close the pipes (browser exits cleanly) and reap it. Returns the
    /// browser's exit code.
    pub fn stop(self: *Driver) !u8 {
        pipe.closeFds(&self.child);
        return pipe.wait(&self.child);
    }

    fn waitForMainFrame(self: *Driver, p: *Page, deadline_ms: i64) !void {
        while (p.main_frame_id == null) {
            const rem = remainingMs(deadline_ms) orelse return error.WaitTimeout;
            try self.pump(rem);
        }
    }

    fn waitForLoad(self: *Driver, p: *Page, deadline_ms: i64) !void {
        while (p.lifecycle.state == .waiting) {
            const rem = remainingMs(deadline_ms) orelse return error.WaitTimeout;
            try self.pump(rem);
        }
    }

    fn waitForContext(self: *Driver, p: *Page, deadline_ms: i64) !void {
        while (p.contexts.items.len == 0) {
            const rem = remainingMs(deadline_ms) orelse return error.WaitTimeout;
            try self.pump(rem);
        }
    }

    fn handleEvent(self: *Driver, ev: session.Router.Event) !void {
        if (std.mem.eql(u8, ev.method, "Browser.attachedToTarget")) {
            try self.onAttachedToTarget(ev.raw);
        } else if (std.mem.eql(u8, ev.method, "Browser.detachedFromTarget")) {
            try self.onDetachedFromTarget(ev.raw);
        } else if (std.mem.eql(u8, ev.method, "Runtime.executionContextCreated")) {
            try self.onExecutionContextCreated(ev);
        } else if (std.mem.eql(u8, ev.method, "Runtime.executionContextsCleared")) {
            try self.onExecutionContextsCleared(ev);
        } else if (std.mem.eql(u8, ev.method, "Runtime.executionContextDestroyed")) {
            try self.onExecutionContextDestroyed(ev);
        } else if (std.mem.eql(u8, ev.method, "Page.eventFired")) {
            try self.onEventFired(ev);
        } else if (std.mem.eql(u8, ev.method, "Page.navigationAborted")) {
            try self.onNavigationAborted(ev);
        } else if (std.mem.eql(u8, ev.method, "Page.navigationStarted")) {
            try self.onNavigationStarted(ev);
        } else if (std.mem.eql(u8, ev.method, "Page.frameAttached")) {
            try self.onFrameAttached(ev);
        } else if (std.mem.eql(u8, ev.method, "Page.frameDetached")) {
            try self.onFrameDetached(ev);
        } else if (std.mem.eql(u8, ev.method, "Page.navigationCommitted")) {
            try self.onNavigationCommitted(ev);
        } else if (std.mem.eql(u8, ev.method, "Page.sameDocumentNavigation")) {
            try self.onSameDocument(ev);
        } else if (std.mem.eql(u8, ev.method, "Runtime.console")) {
            try self.onConsoleEvent(ev);
        } else if (std.mem.startsWith(u8, ev.method, "Network.")) {
            try self.onNetworkEvent(ev);
        }
        // Other events (Page.ready, screencastFrame, Browser.*, ...) are ignored.
    }

    /// Browser.attachedToTarget {sessionId, targetInfo{type, targetId, ...}}
    /// arrives on the ROOT session; the new page session id is in params.
    fn onAttachedToTarget(self: *Driver, raw: []const u8) !void {
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const sid = params.object.get("sessionId") orelse return;
        const ti = params.object.get("targetInfo") orelse return;
        if (sid != .string or ti != .object) return;
        const tid = ti.object.get("targetId") orelse return;
        const ttype = ti.object.get("type") orelse return;
        if (tid != .string or ttype != .string) return;
        var bc_owned: ?[]u8 = null;
        if (ti.object.get("browserContextId")) |bc| {
            if (bc == .string) bc_owned = try self.allocator.dupe(u8, bc.string);
        }
        if (self.verbose) {
            std.debug.print("  -> attachedToTarget sessionId={s} targetId={s} type={s}\n", .{ sid.string, tid.string, ttype.string });
        }

        try self.router.registerSession(.{ .id = sid.string, .target_type = ttype.string, .name = "" });

        const tid_owned = try self.allocator.dupe(u8, tid.string);
        const sid_owned = try self.allocator.dupe(u8, sid.string);
        const gop = try self.pages.getOrPut(tid_owned);
        if (gop.found_existing) {
            self.allocator.free(tid_owned); // key already stored; target_id aliases it
            // Old session id buffer is the by_session map key; re-key first.
            // ("" placeholder never enters by_session and has no buffer.)
            const old_sid = gop.value_ptr.*.session_id;
            if (self.by_session.fetchRemove(old_sid)) |bkv| self.allocator.free(bkv.key);
            gop.value_ptr.*.session_id = sid_owned;
            // Re-key the context id if it changed (rare).
            if (gop.value_ptr.*.browser_context_id) |old_bc| self.allocator.free(old_bc);
            gop.value_ptr.*.browser_context_id = bc_owned;
        } else {
            const p = try self.allocator.create(Page);
            p.* = .{
                .target_id = tid_owned,
                .session_id = sid_owned,
                .browser_context_id = bc_owned,
                .main_frame_id = null,
                .current_url = null,
                .frames = std.StringHashMap(FrameEntry).init(self.allocator),
                .lifecycle = .{ .allocator = self.allocator },
                .contexts = .empty,
            };
            gop.value_ptr.* = p;
        }
        // by_session owns the session id buffer; Page.session_id aliases it.
        const sgop = try self.by_session.getOrPut(sid_owned);
        if (sgop.found_existing) {
            self.allocator.free(sid_owned); // same id already mapped
        }
        gop.value_ptr.*.session_id = @constCast(sgop.key_ptr.*);
        sgop.value_ptr.* = gop.value_ptr.*;
    }

    fn onDetachedFromTarget(self: *Driver, raw: []const u8) !void {
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const sid = params.object.get("sessionId") orelse return;
        const tid = params.object.get("targetId") orelse return;
        if (sid != .string or tid != .string) return;

        self.router.removeSession(sid.string);
        if (self.pages.fetchRemove(tid.string)) |kv| {
            // by_session key aliases p.session_id; drop + free it, then the page.
            if (self.by_session.fetchRemove(kv.value.session_id)) |bkv| self.allocator.free(bkv.key);
            self.freePage(kv.value);
        }
    }

    /// Runtime.executionContextCreated {executionContextId, auxData{frameId?}}
    /// on a page session.
    fn onExecutionContextCreated(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return; // unknown session
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, ev.raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const ctx = params.object.get("executionContextId") orelse return;
        if (ctx != .string) return;

        var frame_id: ?[]u8 = null;
        defer if (frame_id) |f| self.allocator.free(f);
        if (params.object.get("auxData")) |ad| {
            if (ad == .object) {
                if (ad.object.get("frameId")) |f| {
                    if (f == .string) frame_id = try self.allocator.dupe(u8, f.string);
                }
            }
        }
        try p.contexts.append(self.allocator, .{
            .id = try self.allocator.dupe(u8, ctx.string),
            .frame_id = frame_id,
        });
        frame_id = null; // ownership moved into the list
    }

    fn onExecutionContextsCleared(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return;
        for (p.contexts.items) |*c| c.deinit(self.allocator);
        p.contexts.clearRetainingCapacity();
    }

    /// Runtime.executionContextDestroyed {executionContextId} on a page
    /// session: drop the context so pickContext never selects a dead id.
    fn onExecutionContextDestroyed(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return;
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, ev.raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const ctx = params.object.get("executionContextId") orelse return;
        if (ctx != .string) return;
        for (p.contexts.items, 0..) |*c, i| {
            if (std.mem.eql(u8, c.id, ctx.string)) {
                var removed = p.contexts.swapRemove(i);
                removed.deinit(self.allocator);
                return;
            }
        }
    }

    /// Page.eventFired {frameId, name: "load"|"DOMContentLoaded"}.
    fn onEventFired(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return;
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, ev.raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const name = params.object.get("name") orelse return;
        const frame_id = params.object.get("frameId") orelse return;
        if (name != .string or frame_id != .string) return;
        // Both "load" and "DOMContentLoaded" mean the document is ready;
        // the state machine completes on either.
        if (std.mem.eql(u8, name.string, "load") or std.mem.eql(u8, name.string, "DOMContentLoaded")) {
            p.lifecycle.onLoad(frame_id.string);
        }
    }

    fn onNavigationAborted(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return;
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, ev.raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const frame_id = params.object.get("frameId") orelse return;
        const nav_id = params.object.get("navigationId") orelse return;
        const error_text = params.object.get("errorText") orelse return;
        if (frame_id != .string or nav_id != .string or error_text != .string) return;
        p.lifecycle.onAbort(frame_id.string, nav_id.string, error_text.string);
    }

    fn onNavigationStarted(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return;
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, ev.raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const frame_id = params.object.get("frameId") orelse return;
        const nav_id = params.object.get("navigationId") orelse return;
        if (frame_id != .string or nav_id != .string) return;
        p.lifecycle.onNavigationStarted(frame_id.string, nav_id.string);
    }

    /// Page.frameAttached {frameId, parentFrameId?}: registers the frame in
    /// the registry (the parentless frame is the page's main frame).
    fn onFrameAttached(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return;
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, ev.raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const frame_id = params.object.get("frameId") orelse return;
        if (frame_id != .string) return;

        var parent_owned: ?[]u8 = null;
        if (params.object.get("parentFrameId")) |pf| {
            if (pf == .string) parent_owned = try self.allocator.dupe(u8, pf.string);
        }
        if (parent_owned == null) {
            // Main frame: a new main frame id means a new document — reset.
            if (p.main_frame_id) |mf| {
                if (!std.mem.eql(u8, mf, frame_id.string)) {
                    self.allocator.free(mf);
                    p.main_frame_id = null;
                }
            }
            if (p.main_frame_id == null) {
                p.main_frame_id = try self.allocator.dupe(u8, frame_id.string);
            }
        }

        const key = try self.allocator.dupe(u8, frame_id.string);
        const gop = try p.frames.getOrPut(key);
        if (gop.found_existing) {
            self.allocator.free(key); // same content already stored
            if (gop.value_ptr.parent_id) |op| self.allocator.free(op);
        } else {
            gop.value_ptr.* = .{ .parent_id = null, .url = null };
        }
        gop.value_ptr.parent_id = parent_owned;
    }

    /// Page.frameDetached {frameId}: drop the registry entry; a detached
    /// main frame clears the main frame id so the next frameAttached
    /// re-registers the new one.
    fn onFrameDetached(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return;
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, ev.raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const frame_id = params.object.get("frameId") orelse return;
        if (frame_id != .string) return;

        if (p.frames.fetchRemove(frame_id.string)) |kv| {
            self.allocator.free(kv.key);
            var entry = kv.value; // const capture; copy to mutate
            entry.deinit(self.allocator);
        }
        if (p.main_frame_id) |mf| {
            if (std.mem.eql(u8, mf, frame_id.string)) {
                self.allocator.free(mf);
                p.main_frame_id = null;
            }
        }
    }

    /// Page.navigationCommitted {frameId, navigationId, url}: record the
    /// committed URL in the registry and, for main frames, on the page
    /// (TargetInfo.url + the navigate URL gate).
    fn onNavigationCommitted(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return;
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, ev.raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const frame_id = params.object.get("frameId") orelse return;
        const url = params.object.get("url") orelse return;
        if (frame_id != .string or url != .string) return;

        if (p.frames.getPtr(frame_id.string)) |f| {
            if (f.url) |u| self.allocator.free(u);
            f.url = try self.allocator.dupe(u8, url.string);
        }
        const is_main = if (p.main_frame_id) |mf| std.mem.eql(u8, mf, frame_id.string) else false;
        if (!is_main) return;
        if (p.current_url) |u| self.allocator.free(u);
        p.current_url = try self.allocator.dupe(u8, url.string);
        // Feed the navigate gate (the event also carries the navigationId).
        if (params.object.get("navigationId")) |nav_id| {
            if (nav_id == .string) p.lifecycle.onCommitted(frame_id.string, nav_id.string);
        }
    }

    /// Page.sameDocumentNavigation {frameId, navigationId, url}: hashchange
    /// / pushState. Main-frame ones update the page url and complete a
    /// waiting navigation (no load event follows).
    fn onSameDocument(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return;
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, ev.raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const frame_id = params.object.get("frameId") orelse return;
        const url = params.object.get("url") orelse return;
        if (frame_id != .string or url != .string) return;
        const is_main = if (p.main_frame_id) |mf| std.mem.eql(u8, mf, frame_id.string) else false;
        if (!is_main) return;
        if (p.current_url) |u| self.allocator.free(u);
        p.current_url = try self.allocator.dupe(u8, url.string);
        p.lifecycle.onSameDocument(frame_id.string);
    }

    /// Runtime.console {executionContextId, args, type, location} →
    /// normalized Console.messageAdded message (ring-dropped at the cap).
    fn onConsoleEvent(self: *Driver, ev: session.Router.Event) !void {
        var msg = console.normalize(self.allocator, ev.raw) catch return; // malformed console events are non-fatal
        if (self.console_messages.items.len >= max_console_messages) {
            var old = self.console_messages.orderedRemove(0);
            old.deinit(self.allocator);
        }
        self.console_messages.append(self.allocator, msg) catch |err| {
            msg.deinit(self.allocator);
            return err;
        };
    }

    /// Network.* events: passive passthrough — the raw wire event JSON is
    /// retained (Juggler's Network event names are already CDP-shaped).
    /// Ring-dropped at the cap.
    fn onNetworkEvent(self: *Driver, ev: session.Router.Event) !void {
        if (self.network_events.items.len >= max_network_events) {
            self.allocator.free(self.network_events.orderedRemove(0));
        }
        self.network_events.append(self.allocator, try self.allocator.dupe(u8, ev.raw)) catch |err| return err;
    }
};

/// Preferred evaluate context: the latest context of the main frame; falls
/// back to the latest context of the session.
fn pickContext(p: *Page) ?*ContextInfo {
    var chosen: ?*ContextInfo = null;
    for (p.contexts.items) |*c| {
        if (c.frame_id) |f| {
            if (p.main_frame_id) |mf| {
                if (std.mem.eql(u8, f, mf)) chosen = c;
            }
        }
    }
    return chosen orelse if (p.contexts.items.len > 0) &p.contexts.items[p.contexts.items.len - 1] else null;
}

/// Build the wire request: {"id":N,"sessionId":"...","method":"...","params":{...}}
/// (sessionId omitted for the root session).
fn buildRequest(
    allocator: Allocator,
    id: u32,
    session_id: ?[]const u8,
    method: []const u8,
    params_json: []const u8,
) Allocator.Error![]u8 {
    if (session_id) |sid| {
        return std.fmt.allocPrint(
            allocator,
            "{{\"id\":{d},\"sessionId\":\"{s}\",\"method\":\"{s}\",\"params\":{s}}}",
            .{ id, sid, method, params_json },
        );
    }
    return std.fmt.allocPrint(
        allocator,
        "{{\"id\":{d},\"method\":\"{s}\",\"params\":{s}}}",
        .{ id, method, params_json },
    );
}

/// Milliseconds left until `deadline_ms` (wall clock; fine for timeouts),
/// or null when expired.
fn remainingMs(deadline_ms: i64) ?i32 {
    const now = nowMs();
    const rem = deadline_ms - now;
    if (rem <= 0) return null;
    if (rem > 2147483647) return 2147483647;
    return @intCast(rem);
}

/// Number value of a JSON node, or null for non-numbers.
fn numF64(v: std.json.Value) ?f64 {
    return switch (v) {
        .integer => |i| @floatFromInt(i),
        .float => |f| f,
        else => null,
    };
}

/// CDP Page.getFrameTree result from the frame registry: the parentless
/// frame is the root; children are found by parentId (Faz 3). Owned JSON:
/// {"frameTree":{"frame":{"id","parentId"?,"url"},"childFrames":[...]}}.
fn buildFrameTreeJson(allocator: Allocator, p: *Page) ![]u8 {
    // Write the nested tree by hand; Stringify of a recursive structure
    // would need a custom type for the recursion.
    var out: std.Io.Writer.Allocating = .init(allocator);
    defer out.deinit();
    const w = &out.writer;
    try w.writeAll("{\"frameTree\":");
    try writeFrameNode(allocator, w, p, p.main_frame_id orelse return error.NoMainFrame);
    try w.writeAll("}");
    return allocator.dupe(u8, out.written());
}

fn writeFrameNode(allocator: Allocator, w: anytype, p: *Page, frame_id: []const u8) !void {
    const e = p.frames.get(frame_id) orelse return error.UnknownFrame;
    try w.writeAll("{\"frame\":{\"id\":");
    try std.json.Stringify.value(frame_id, .{}, w);
    if (e.parent_id) |pid| {
        try w.writeAll(",\"parentId\":");
        try std.json.Stringify.value(pid, .{}, w);
    }
    try w.writeAll(",\"url\":");
    try std.json.Stringify.value(e.url orelse "", .{}, w);
    try w.writeAll("},\"childFrames\":[");
    var first = true;
    var it = p.frames.iterator();
    while (it.next()) |kv| {
        if (kv.value_ptr.parent_id == null) continue;
        if (!std.mem.eql(u8, kv.value_ptr.parent_id.?, frame_id)) continue;
        if (!first) try w.writeAll(",");
        first = false;
        try writeFrameNode(allocator, w, p, kv.key_ptr.*);
    }
    try w.writeAll("]}");
}

fn ensureProfile(allocator: Allocator, profile: ?[]const u8) ![]const u8 {
    const path = if (profile) |p| p else try std.fmt.allocPrint(
        allocator,
        "/tmp/kahin-core-{d}-faz2",
        .{std.os.linux.getpid()},
    );
    // std.fs.makeDirAbsolute is gone in 0.16; raw syscall keeps this simple.
    const path_z = try allocator.dupeZ(u8, path);
    defer allocator.free(path_z);
    const rc = std.os.linux.mkdir(path_z.ptr, 0o700);
    switch (std.os.linux.errno(rc)) {
        .SUCCESS, .EXIST => {},
        else => return error.MkdirFailed,
    }
    return path;
}

const testing = std.testing;

fn testPipe() ![2]i32 {
    var fds: [2]i32 = undefined;
    if (std.os.linux.errno(std.os.linux.pipe2(&fds, .{})) != .SUCCESS) return error.PipeFailed;
    return fds;
}

fn writeFake(d: *Driver, fds: [2]i32, json: []const u8) !void {
    _ = d;
    try pipe.writeMessage(testing.allocator, fds[1], json);
}

test "driver: buildRequest shapes match Juggler wire format" {
    const root = try buildRequest(testing.allocator, 1, null, "Browser.enable", "{}");
    defer testing.allocator.free(root);
    try testing.expectEqualStrings("{\"id\":1,\"method\":\"Browser.enable\",\"params\":{}}", root);

    const sub = try buildRequest(testing.allocator, 2, "sess-1", "Page.navigate", "{\"frameId\":\"f\"}");
    defer testing.allocator.free(sub);
    try testing.expectEqualStrings(
        "{\"id\":2,\"sessionId\":\"sess-1\",\"method\":\"Page.navigate\",\"params\":{\"frameId\":\"f\"}}",
        sub,
    );
}

test "driver: attachedToTarget registers targetId -> sessionId" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"sess-1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t-1\",\"browserContextId\":\"ctx-1\"}}}");
    try d.pump(1000);

    const p = d.pages.get("t-1") orelse return error.TestUnexpected;
    try testing.expectEqualStrings("sess-1", p.session_id);
    try testing.expect(d.by_session.get("sess-1") == p);
    try testing.expect(d.router.sessions.contains("sess-1"));
}

test "driver: frameAttached without parent sets main frame" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f-main\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f-sub\",\"parentFrameId\":\"f-main\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);

    const p = d.pages.get("t1") orelse return error.TestUnexpected;
    try testing.expectEqualStrings("f-main", p.main_frame_id.?);
}

test "driver: executionContextCreated recorded per session" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f1\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Runtime.executionContextCreated\",\"params\":{\"executionContextId\":\"ctx-9\",\"auxData\":{\"frameId\":\"f1\"}},\"sessionId\":\"s1\"}");
    try d.pump(1000);

    const p = d.pages.get("t1") orelse return error.TestUnexpected;
    try testing.expectEqual(@as(usize, 1), p.contexts.items.len);
    try testing.expectEqualStrings("ctx-9", p.contexts.items[0].id);
    try testing.expectEqualStrings("f1", p.contexts.items[0].frame_id.?);
    try testing.expectEqualStrings("ctx-9", pickContext(p).?.id);
}

test "driver: eventFired load drives lifecycle to done" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f1\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);

    const p = d.pages.get("t1") orelse return error.TestUnexpected;
    p.lifecycle.begin("f1");
    try writeFake(&d, fds, "{\"method\":\"Page.eventFired\",\"params\":{\"frameId\":\"f1\",\"name\":\"load\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try testing.expectEqual(page.Lifecycle.State.done, p.lifecycle.state);
}

test "driver: navigationAborted drives lifecycle to aborted" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f1\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);

    const p = d.pages.get("t1") orelse return error.TestUnexpected;
    p.lifecycle.begin("f1");
    p.lifecycle.setNavigationId("f1", "n1");
    try writeFake(&d, fds, "{\"method\":\"Page.navigationAborted\",\"params\":{\"frameId\":\"f1\",\"navigationId\":\"n1\",\"errorText\":\"NS_BINDING_ABORTED\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try testing.expectEqual(page.Lifecycle.State.aborted, p.lifecycle.state);
}

test "driver: navigationAborted for a superseded navigation is ignored" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f1\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);

    const p = d.pages.get("t1") orelse return error.TestUnexpected;
    p.lifecycle.begin("f1");
    p.lifecycle.setNavigationId("f1", "n1");
    try writeFake(&d, fds, "{\"method\":\"Page.navigationAborted\",\"params\":{\"frameId\":\"f1\",\"navigationId\":\"n0\",\"errorText\":\"NS_BINDING_ABORTED\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try testing.expectEqual(page.Lifecycle.State.waiting, p.lifecycle.state);
    try testing.expectEqualStrings("", p.lifecycle.abort_text);
}

test "driver: executionContextDestroyed removes the context" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f1\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Runtime.executionContextCreated\",\"params\":{\"executionContextId\":\"ctx-1\",\"auxData\":{\"frameId\":\"f1\"}},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Runtime.executionContextCreated\",\"params\":{\"executionContextId\":\"ctx-2\",\"auxData\":{\"frameId\":\"f1\"}},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Runtime.executionContextDestroyed\",\"params\":{\"executionContextId\":\"ctx-1\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);

    const p = d.pages.get("t1") orelse return error.TestUnexpected;
    try testing.expectEqual(@as(usize, 1), p.contexts.items.len);
    try testing.expectEqualStrings("ctx-2", p.contexts.items[0].id);
    try testing.expectEqualStrings("ctx-2", pickContext(p).?.id);
}

test "driver: executionContextDestroyed for unknown id is a no-op" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Runtime.executionContextCreated\",\"params\":{\"executionContextId\":\"ctx-1\",\"auxData\":{\"frameId\":\"f1\"}},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Runtime.executionContextDestroyed\",\"params\":{\"executionContextId\":\"ctx-9\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);

    const p = d.pages.get("t1") orelse return error.TestUnexpected;
    try testing.expectEqual(@as(usize, 1), p.contexts.items.len);
    try testing.expectEqualStrings("ctx-1", p.contexts.items[0].id);
}

test "driver: frameDetached clears main frame, re-attach re-registers" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f-old\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameDetached\",\"params\":{\"frameId\":\"f-old\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);

    const p = d.pages.get("t1") orelse return error.TestUnexpected;
    try testing.expect(p.main_frame_id == null);

    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f-new\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try testing.expectEqualStrings("f-new", p.main_frame_id.?);
}

test "driver: frameDetached for sub-frame keeps main frame" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f-main\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameDetached\",\"params\":{\"frameId\":\"f-sub\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);

    const p = d.pages.get("t1") orelse return error.TestUnexpected;
    try testing.expectEqualStrings("f-main", p.main_frame_id.?);
}

test "driver: removeBrowserContext/close/setExtraHTTPHeaders send schema methods" {
    var cmd: [2]i32 = undefined;
    var resp: [2]i32 = undefined;
    if (std.os.linux.errno(std.os.linux.pipe2(&cmd, .{})) != .SUCCESS) return error.PipeFailed;
    if (std.os.linux.errno(std.os.linux.pipe2(&resp, .{})) != .SUCCESS) return error.PipeFailed;
    defer {
        _ = std.os.linux.close(cmd[0]);
        _ = std.os.linux.close(cmd[1]);
        _ = std.os.linux.close(resp[0]);
        _ = std.os.linux.close(resp[1]);
    }
    var d = Driver.init(testing.allocator, resp[0], cmd[1], false);
    defer d.deinit();

    const T = struct {
        fn run(cmd_read: i32, resp_write: i32) void {
            var r = pipe.Reader.init(cmd_read);
            defer r.deinit(testing.allocator);
            const expected = [_][]const u8{
                "{\"id\":1,\"method\":\"Browser.removeBrowserContext\",\"params\":{\"browserContextId\":\"ctx-1\"}}",
                "{\"id\":2,\"method\":\"Browser.close\",\"params\":{}}",
                "{\"id\":3,\"method\":\"Browser.setExtraHTTPHeaders\",\"params\":{\"headers\":[]}}",
            };
            const replies = [_][]const u8{
                "{\"id\":1,\"result\":{}}",
                "{\"id\":2,\"result\":{}}",
                "{\"id\":3,\"result\":{}}",
            };
            for (expected, 0..) |want, i| {
                const msg = (r.readMessage(testing.allocator, 5000) catch return) orelse return;
                defer testing.allocator.free(msg);
                if (!std.mem.eql(u8, msg, want)) return;
                pipe.writeMessage(testing.allocator, resp_write, replies[i]) catch return;
            }
        }
    };
    const thread = try std.Thread.spawn(.{}, T.run, .{ cmd[0], resp[1] });
    defer thread.join();

    try d.removeBrowserContext("ctx-1", 2000);
    try d.close(2000);
    const headers = [_]browser.Header{};
    try d.setExtraHTTPHeaders(null, &headers, 2000);
}

test "driver: send writes request and matches response by id" {
    // Real browser wiring uses two pipes (cmd: parent->child, resp: child->parent);
    // a single pipe would loop the request back to the driver.
    var cmd: [2]i32 = undefined;
    var resp: [2]i32 = undefined;
    if (std.os.linux.errno(std.os.linux.pipe2(&cmd, .{})) != .SUCCESS) return error.PipeFailed;
    if (std.os.linux.errno(std.os.linux.pipe2(&resp, .{})) != .SUCCESS) return error.PipeFailed;
    defer {
        _ = std.os.linux.close(cmd[0]);
        _ = std.os.linux.close(cmd[1]);
        _ = std.os.linux.close(resp[0]);
        _ = std.os.linux.close(resp[1]);
    }
    var d = Driver.init(testing.allocator, resp[0], cmd[1], false);
    defer d.deinit();

    // Responder thread: assert the exact request the driver wrote (first
    // request on a fresh driver has id 1), then answer it on the resp pipe.
    const T = struct {
        fn run(cmd_read: i32, resp_write: i32) void {
            var r = pipe.Reader.init(cmd_read);
            defer r.deinit(testing.allocator);
            const msg = (r.readMessage(testing.allocator, 5000) catch return) orelse return;
            defer testing.allocator.free(msg);
            if (!std.mem.eql(u8, msg, "{\"id\":1,\"method\":\"Browser.createBrowserContext\",\"params\":{}}")) return;
            pipe.writeMessage(testing.allocator, resp_write, "{\"id\":1,\"result\":{\"browserContextId\":\"ctx-9\"}}") catch return;
        }
    };
    const thread = try std.Thread.spawn(.{}, T.run, .{ cmd[0], resp[1] });
    defer thread.join();

    var res = try d.send(null, browser.method_create_browser_context, "{}", 2000);
    defer res.deinit(testing.allocator);
    try testing.expect(!res.is_error);
    const id = try browser.parseBrowserContextId(testing.allocator, res.raw);
    defer testing.allocator.free(id);
    try testing.expectEqualStrings("ctx-9", id);
}
