//! Juggler Page domain adapter (Faz 2 + 3): navigate, close, screenshot +
//! lifecycle state machine.
//!
//! Schema facts (protocol/schema/juggler-schema.json, Page domain):
//!   navigate(frameId: string, url: string, referer?: string) -> navigationId
//!   close(runBeforeUnload?: boolean)
//!   screenshot(mimeType: "image/png"|"image/jpeg", clip?: Page.Clip,
//!              quality?: number, omitDeviceScaleFactor?: boolean)
//!         -> data: string (base64)
//!   events: eventFired(frameId, name: "load"|"DOMContentLoaded"),
//!           navigationStarted(frameId, navigationId),
//!           navigationCommitted(frameId, navigationId?, url, name),
//!           navigationAborted(frameId, navigationId, errorText),
//!           sameDocumentNavigation(frameId, navigationId, url),
//!           frameAttached(frameId, parentFrameId?), frameDetached(frameId)
//!
//! NOT in the schema (confirmed absent; not implemented):
//!   Page.getFrameTree (driver generates it from the frame registry),
//!   Page.setLifecycleEventsEnabled, Page.lifecycleEvent,
//!   Page.getLayoutMetrics (the full-page screenshot uses clip +
//!   a scroll-size evaluate instead) — the lifecycle signal is the
//!   `Page.eventFired` event with name "load"/"DOMContentLoaded".

const std = @import("std");
const Allocator = std.mem.Allocator;

pub const method_navigate = "Page.navigate";
pub const method_close = "Page.close";
pub const method_screenshot = "Page.screenshot";

/// Page.Clip per schema: {x, y, width, height} (all numbers, required).
pub const Clip = struct {
    x: f64 = 0,
    y: f64 = 0,
    width: f64,
    height: f64,
};

/// Page.navigate params. `referer` omitted when null.
pub fn navigateParams(
    allocator: Allocator,
    frame_id: []const u8,
    url: []const u8,
    referer: ?[]const u8,
) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{ .frameId = frame_id, .url = url, .referer = referer },
        .{ .emit_null_optional_fields = false },
    );
}

/// Parse the `navigationId` string out of a navigate response. The schema
/// marks it nullable (same-document/about:blank navigations can omit it):
/// null yields an empty string, the real id is adopted from the
/// navigationStarted event (driver.onNavigationStarted).
pub fn parseNavigationId(allocator: Allocator, response_raw: []const u8) ![]u8 {
    const parsed = try std.json.parseFromSlice(std.json.Value, allocator, response_raw, .{});
    defer parsed.deinit();
    const root = parsed.value;
    if (root != .object) return error.InvalidResponse;
    if (root.object.get("error")) |_| return error.JugglerError;
    const result = root.object.get("result") orelse return error.InvalidResponse;
    if (result != .object) return error.InvalidResponse;
    const id = result.object.get("navigationId") orelse return error.InvalidResponse;
    if (id == .null) return allocator.dupe(u8, "");
    if (id != .string) return error.InvalidResponse;
    return allocator.dupe(u8, id.string);
}

/// Page.close params. `run_before_unload` omitted (schema: optional,
/// defaults to false — closing a target must not fire beforeunload dialogs).
pub fn closeParams() []const u8 {
    return "{}";
}

/// Page.screenshot params. `clip`/`quality`/`omit_device_scale_factor`
/// omitted when null. mimeType comes from the schema enum.
pub fn screenshotParams(
    allocator: Allocator,
    mime_type: []const u8,
    clip: ?Clip,
    quality: ?u32,
    omit_device_scale_factor: ?bool,
) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{
            .mimeType = mime_type,
            .clip = clip,
            .quality = quality,
            .omitDeviceScaleFactor = omit_device_scale_factor,
        },
        .{ .emit_null_optional_fields = false },
    );
}

/// Parse the `data` string (base64) out of a screenshot response.
pub fn parseScreenshotData(allocator: Allocator, response_raw: []const u8) ![]u8 {
    const parsed = try std.json.parseFromSlice(std.json.Value, allocator, response_raw, .{});
    defer parsed.deinit();
    const root = parsed.value;
    if (root != .object) return error.InvalidResponse;
    if (root.object.get("error")) |_| return error.JugglerError;
    const result = root.object.get("result") orelse return error.InvalidResponse;
    if (result != .object) return error.InvalidResponse;
    const data = result.object.get("data") orelse return error.InvalidResponse;
    if (data != .string) return error.InvalidResponse;
    return allocator.dupe(u8, data.string);
}

/// Decode the screenshot base64 string into raw image bytes. Juggler
/// returns `data` base64-encoded (Playwright semantics); Kahin's
/// engine.screenshot() hands raw bytes to base64.b64encode, so the driver
/// decodes here.
pub fn decodeScreenshot(allocator: Allocator, b64: []const u8) ![]u8 {
    const dec = std.base64.standard.Decoder;
    const size = dec.calcSizeForSlice(b64) catch return error.InvalidBase64;
    const out = try allocator.alloc(u8, size);
    errdefer allocator.free(out);
    dec.decode(out, b64) catch return error.InvalidBase64;
    return out;
}

/// Navigation lifecycle state machine: idle -> waiting -> done | aborted.
///
/// Driven by Page.eventFired ("load"/"DOMContentLoaded") and
/// Page.navigationAborted events for the frame being navigated. Events for
/// other frames or in the wrong state are ignored.
///
/// String fields: frame_id borrows from the driver (page-owned, stable);
/// navigation_id and abort_text are OWNED by the lifecycle (duped on store,
/// freed on replace/begin/deinit) — event-derived values borrow the
/// dispatch arena and would dangle after the handler returns.
pub const Lifecycle = struct {
    pub const State = enum { idle, waiting, done, aborted };

    allocator: std.mem.Allocator,
    state: State = .idle,
    /// Main frame of the navigating page (owned by the driver, stable).
    frame_id: []const u8 = "",
    /// Set from the navigate response or navigationStarted event (owned).
    navigation_id: []const u8 = "",
    /// True once a navigationStarted event carried the CURRENT navigation
    /// id — proof the browser accepted our navigation (redirects included).
    /// Reset on begin(); a stale started event (previous document) can
    /// never set it because its id differs from the response id.
    nav_started: bool = false,
    /// True once a main-frame navigationCommitted carried the CURRENT
    /// navigation id — the new document is committed and its execution
    /// contexts exist, so a follow-up evaluate/screenshot cannot hit the
    /// previous document. Redirects commit a different URL, so this flag
    /// (not a URL match) is what the gate needs for them.
    committed_current: bool = false,
    /// errorText from navigationAborted (owned).
    abort_text: []const u8 = "",

    /// Enter the waiting state for `frame_id`. No-op unless idle/done/aborted.
    pub fn begin(self: *Lifecycle, frame_id: []const u8) void {
        if (self.state == .waiting) return;
        self.frame_id = frame_id;
        self.state = .waiting;
        self.nav_started = false;
        self.committed_current = false;
        self.replaceNavigationId("");
        if (self.abort_text.len > 0) {
            self.allocator.free(self.abort_text);
            self.abort_text = "";
        }
    }

    /// Free owned strings.
    pub fn deinit(self: *Lifecycle) void {
        self.replaceNavigationId("");
        if (self.abort_text.len > 0) {
            self.allocator.free(self.abort_text);
            self.abort_text = "";
        }
    }

    /// Store `id` as an owned copy; "" clears. OOM keeps the previous id.
    fn replaceNavigationId(self: *Lifecycle, id: []const u8) void {
        if (self.navigation_id.len > 0) self.allocator.free(self.navigation_id);
        self.navigation_id = if (id.len == 0)
            ""
        else
            self.allocator.dupe(u8, id) catch return;
    }

    /// Record the navigation id (from the navigate response or event).
    /// Adopts in the waiting AND done states: the navigate response may
    /// arrive after a (possibly stale) load event already completed the
    /// lifecycle — the response id is still authoritative for the URL gate
    /// (driver.navigate). An already-set id is overwritten (the response id
    /// wins over event-derived ids).
    pub fn setNavigationId(self: *Lifecycle, frame_id: []const u8, navigation_id: []const u8) void {
        if ((self.state != .waiting and self.state != .done) or !std.mem.eql(u8, frame_id, self.frame_id)) return;
        self.replaceNavigationId(navigation_id);
    }

    /// Page.eventFired with name "load" (or "DOMContentLoaded") -> done.
    pub fn onLoad(self: *Lifecycle, frame_id: []const u8) void {
        if (self.state != .waiting or !std.mem.eql(u8, frame_id, self.frame_id)) return;
        self.state = .done;
    }

    /// Page.navigationAborted -> aborted, remember the error text.
    pub fn onAbort(self: *Lifecycle, frame_id: []const u8, error_text: []const u8) void {
        if (self.state != .waiting or !std.mem.eql(u8, frame_id, self.frame_id)) return;
        self.state = .aborted;
        if (self.abort_text.len > 0) self.allocator.free(self.abort_text);
        self.abort_text = self.allocator.dupe(u8, error_text) catch return;
    }

    /// Page.navigationStarted — adopt the navigation id if not yet known and
    /// mark the navigation as genuinely started when the event's id matches
    /// the one we already know (from the navigate response). An unknown id
    /// here is adopted but unproven — a stale event from the previous
    /// document carries a different id and can never set the flag.
    ///
    /// Runs in the waiting AND done states: a stale load event from the
    /// previous document can complete the lifecycle before our navigation
    /// starts (state .done) — the real started event must still be able to
    /// mark the navigation as genuine.
    /// Page.navigationStarted — prove the navigation is genuine: the event
    /// carries the id we know from the navigate RESPONSE (the only
    /// authoritative id). Events arriving before the response are events of
    /// the PREVIOUS document (e.g. the initial about:blank navigation racing
    /// our request) and are ignored — their id is never adopted.
    ///
    /// Runs in the waiting AND done states: a stale load event from the
    /// previous document can complete the lifecycle before our navigation
    /// starts (state .done) — the real started event must still be able to
    /// mark the navigation as genuine.
    pub fn onNavigationStarted(self: *Lifecycle, frame_id: []const u8, navigation_id: []const u8) void {
        if ((self.state != .waiting and self.state != .done) or !std.mem.eql(u8, frame_id, self.frame_id)) return;
        if (self.navigation_id.len == 0) return; // response id unknown yet — stale document events
        self.nav_started = std.mem.eql(u8, self.navigation_id, navigation_id);
    }

    /// Page.navigationCommitted for the navigation frame: mark the current
    /// navigation as committed when the event carries our navigation id
    /// (the new document exists; its contexts are usable). Commits observed
    /// before the response id is known are the previous document's (e.g.
    /// about:blank) and never set the flag.
    pub fn onCommitted(self: *Lifecycle, frame_id: []const u8, navigation_id: []const u8) void {
        if (!std.mem.eql(u8, frame_id, self.frame_id)) return;
        if (self.navigation_id.len == 0) return; // response id unknown yet — stale document commits
        self.committed_current = std.mem.eql(u8, self.navigation_id, navigation_id);
    }

    /// Page.sameDocumentNavigation — completes a waiting navigation (no load
    /// event follows a same-document navigation).
    pub fn onSameDocument(self: *Lifecycle, frame_id: []const u8) void {
        if (self.state != .waiting or !std.mem.eql(u8, frame_id, self.frame_id)) return;
        self.state = .done;
    }
};

const testing = std.testing;

test "page: navigate params carry schema names" {
    const p = try navigateParams(testing.allocator, "frame-1", "data:text/html,<h1>hi</h1>", null);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings(
        "{\"frameId\":\"frame-1\",\"url\":\"data:text/html,<h1>hi</h1>\"}",
        p,
    );
}

test "page: navigate params include referer when given" {
    const p = try navigateParams(testing.allocator, "frame-1", "https://example.com", "https://ref.example/");
    defer testing.allocator.free(p);
    try testing.expectEqualStrings(
        "{\"frameId\":\"frame-1\",\"url\":\"https://example.com\",\"referer\":\"https://ref.example/\"}",
        p,
    );
}

test "page: parseNavigationId" {
    const id = try parseNavigationId(testing.allocator, "{\"id\":4,\"result\":{\"navigationId\":\"nav-1\"}}");
    defer testing.allocator.free(id);
    try testing.expectEqualStrings("nav-1", id);
}

test "page: parseNavigationId accepts null (same-document navigation)" {
    const id = try parseNavigationId(testing.allocator, "{\"id\":4,\"result\":{\"navigationId\":null}}");
    defer testing.allocator.free(id);
    try testing.expectEqualStrings("", id);
}

test "page: parseNavigationId rejects error responses" {
    try testing.expectError(
        error.JugglerError,
        parseNavigationId(testing.allocator, "{\"id\":4,\"error\":{\"code\":-32000}}"),
    );
}

test "lifecycle: begin -> load -> done" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    try testing.expectEqual(Lifecycle.State.waiting, lc.state);
    lc.onLoad("f1");
    try testing.expectEqual(Lifecycle.State.done, lc.state);
}

test "lifecycle: begin -> abort -> aborted with text" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    lc.onAbort("f1", "NS_BINDING_ABORTED");
    try testing.expectEqual(Lifecycle.State.aborted, lc.state);
    try testing.expectEqualStrings("NS_BINDING_ABORTED", lc.abort_text);
}

test "lifecycle: load for another frame is ignored" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    lc.onLoad("f2");
    try testing.expectEqual(Lifecycle.State.waiting, lc.state);
    lc.onLoad("f1");
    try testing.expectEqual(Lifecycle.State.done, lc.state);
}

test "lifecycle: events in idle state are no-ops" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.onLoad("f1");
    lc.onAbort("f1", "x");
    try testing.expectEqual(Lifecycle.State.idle, lc.state);
}

test "lifecycle: done state is terminal for later events" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    lc.onLoad("f1");
    lc.onAbort("f1", "late");
    try testing.expectEqual(Lifecycle.State.done, lc.state);
}

test "lifecycle: navigationStarted before any response id proves nothing and adopts nothing" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    lc.onNavigationStarted("f1", "nav-x");
    try testing.expect(!lc.nav_started);
    try testing.expectEqualStrings("", lc.navigation_id);
    // Only the response id is authoritative; a matching started event then
    // proves the navigation.
    lc.setNavigationId("f1", "nav-x");
    lc.onNavigationStarted("f1", "nav-x");
    try testing.expect(lc.nav_started);
}

test "lifecycle: setNavigationId for wrong frame ignored" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    lc.setNavigationId("f2", "nav-y");
    try testing.expectEqualStrings("", lc.navigation_id);
}

test "lifecycle: begin during waiting is a no-op" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    lc.begin("f2");
    try testing.expectEqualStrings("f1", lc.frame_id);
    try testing.expectEqual(Lifecycle.State.waiting, lc.state);
}

test "page: method name matches schema" {
    try testing.expectEqualStrings("Page.navigate", method_navigate);
}

test "lifecycle: setNavigationId adopts in done state (response after stale load)" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    lc.onLoad("f1"); // stale load completes early
    try testing.expectEqual(Lifecycle.State.done, lc.state);
    lc.setNavigationId("f1", "nav-23");
    try testing.expectEqualStrings("nav-23", lc.navigation_id);
}

test "lifecycle: setNavigationId still ignores wrong frame in done state" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    lc.onLoad("f1");
    lc.setNavigationId("f2", "nav-x");
    try testing.expectEqualStrings("", lc.navigation_id);
}

test "lifecycle: sameDocumentNavigation completes a waiting navigation" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    lc.onSameDocument("f1");
    try testing.expectEqual(Lifecycle.State.done, lc.state);
}

test "lifecycle: sameDocumentNavigation for another frame is ignored" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    lc.onSameDocument("f2");
    try testing.expectEqual(Lifecycle.State.waiting, lc.state);
}

test "lifecycle: started event matching the response id marks the navigation genuine" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    lc.setNavigationId("f1", "nav-23"); // response first
    lc.onNavigationStarted("f1", "nav-23");
    try testing.expect(lc.nav_started);
    try testing.expectEqualStrings("nav-23", lc.navigation_id);
}

test "lifecycle: stale started event (different id) never sets nav_started" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    lc.onNavigationStarted("f1", "nav-22"); // stale event from previous document
    lc.setNavigationId("f1", "nav-23"); // response id differs
    try testing.expect(!lc.nav_started);
}

test "lifecycle: real started event after stale-load completion (done state) sets nav_started" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    lc.onLoad("f1"); // stale load completes the lifecycle early
    lc.setNavigationId("f1", "nav-23"); // response id authoritative
    lc.onNavigationStarted("f1", "nav-23"); // our started event arrives in done state
    try testing.expect(lc.nav_started);
}

test "lifecycle: commit with our navigation id marks committed_current" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    lc.setNavigationId("f1", "nav-23");
    lc.onCommitted("f1", "nav-23");
    try testing.expect(lc.committed_current);
}

test "lifecycle: commit with a stale navigation id never marks committed_current" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    lc.setNavigationId("f1", "nav-23");
    lc.onCommitted("f1", "nav-22"); // about:blank commit of the previous document
    try testing.expect(!lc.committed_current);
}

test "lifecycle: commit before the response id is known is the previous document and never marks committed_current" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    lc.onCommitted("f1", "nav-22"); // about:blank commit racing our request
    try testing.expect(!lc.committed_current);
    lc.setNavigationId("f1", "nav-23"); // response arrives later
    try testing.expect(!lc.committed_current); // the flag is never backfilled
    lc.onCommitted("f1", "nav-23"); // our real commit
    try testing.expect(lc.committed_current);
}

test "lifecycle: started event before the response id is known is never adopted nor proven" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    lc.onNavigationStarted("f1", "nav-22"); // stale about:blank navigation
    try testing.expect(!lc.nav_started);
    try testing.expectEqualStrings("", lc.navigation_id); // not adopted
    lc.setNavigationId("f1", "nav-23");
    lc.onNavigationStarted("f1", "nav-23");
    try testing.expect(lc.nav_started);
}

test "lifecycle: begin resets committed_current" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    lc.setNavigationId("f1", "nav-1");
    lc.onCommitted("f1", "nav-1");
    try testing.expect(lc.committed_current);
    lc.onLoad("f1"); // complete the navigation
    lc.begin("f1"); // next navigation
    try testing.expect(!lc.committed_current);
}

test "lifecycle: begin resets nav_started" {
    var lc = Lifecycle{ .allocator = testing.allocator };
    defer lc.deinit();
    lc.begin("f1");
    lc.setNavigationId("f1", "nav-1");
    lc.onNavigationStarted("f1", "nav-1");
    try testing.expect(lc.nav_started);
    lc.onLoad("f1");
    lc.begin("f1"); // next navigation
    try testing.expect(!lc.nav_started);
}

test "page: close params is empty object (runBeforeUnload omitted)" {
    try testing.expectEqualStrings("{}", closeParams());
    try testing.expect(std.mem.indexOf(u8, closeParams(), "runBeforeUnload") == null);
}

test "page: screenshot params carry schema names and enum mimeType" {
    const p = try screenshotParams(testing.allocator, "image/png", null, null, null);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings("{\"mimeType\":\"image/png\"}", p);
}

test "page: screenshot params include clip rect for full page" {
    const p = try screenshotParams(
        testing.allocator,
        "image/jpeg",
        .{ .width = 100, .height = 2000 },
        null,
        null,
    );
    defer testing.allocator.free(p);
    try testing.expectEqualStrings(
        "{\"mimeType\":\"image/jpeg\",\"clip\":{\"x\":0,\"y\":0,\"width\":100,\"height\":2000}}",
        p,
    );
}

test "page: screenshot params carry quality and omitDeviceScaleFactor" {
    const p = try screenshotParams(testing.allocator, "image/png", null, 80, true);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings(
        "{\"mimeType\":\"image/png\",\"quality\":80,\"omitDeviceScaleFactor\":true}",
        p,
    );
}

test "page: parseScreenshotData extracts base64 string" {
    const data = try parseScreenshotData(
        testing.allocator,
        "{\"id\":9,\"result\":{\"data\":\"iVBORw0KGgo=\"}}",
    );
    defer testing.allocator.free(data);
    try testing.expectEqualStrings("iVBORw0KGgo=", data);
}

test "page: decodeScreenshot yields PNG magic bytes" {
    // base64 of the 8-byte PNG signature \x89PNG\r\n\x1a\n
    const raw = try decodeScreenshot(testing.allocator, "iVBORw0KGgo=");
    defer testing.allocator.free(raw);
    try testing.expectEqual(@as(usize, 8), raw.len);
    try testing.expectEqualSlices(u8, "\x89PNG\r\n\x1a\n", raw);
}

test "page: decodeScreenshot rejects invalid base64" {
    try testing.expectError(error.InvalidBase64, decodeScreenshot(testing.allocator, "!!!not-b64!!!"));
}

test "page: parseScreenshotData rejects error responses" {
    try testing.expectError(
        error.JugglerError,
        parseScreenshotData(testing.allocator, "{\"id\":9,\"error\":{\"code\":-32000}}"),
    );
}

test "page: close method name matches schema" {
    try testing.expectEqualStrings("Page.close", method_close);
}

test "page: screenshot method name matches schema" {
    try testing.expectEqualStrings("Page.screenshot", method_screenshot);
}
