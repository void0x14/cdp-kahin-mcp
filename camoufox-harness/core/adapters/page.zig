//! Juggler Page domain adapter (Faz 2): navigate + lifecycle state machine.
//!
//! Schema facts (protocol/schema/juggler-schema.json, Page domain):
//!   navigate(frameId: string, url: string, referer?: string) -> navigationId
//!   events: eventFired(frameId, name: "load"|"DOMContentLoaded"),
//!           navigationStarted(frameId, navigationId),
//!           navigationCommitted(frameId, navigationId?, url, name),
//!           navigationAborted(frameId, navigationId, errorText),
//!           frameAttached(frameId, parentFrameId?), frameDetached(frameId)
//!
//! NOT in the schema (confirmed absent; not implemented):
//!   Page.getFrameTree, Page.setLifecycleEventsEnabled, Page.lifecycleEvent
//!   — the lifecycle signal is the `Page.eventFired` event with name
//!   "load"/"DOMContentLoaded".

const std = @import("std");
const Allocator = std.mem.Allocator;

pub const method_navigate = "Page.navigate";

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

/// Navigation lifecycle state machine: idle -> waiting -> done | aborted.
///
/// Driven by Page.eventFired ("load"/"DOMContentLoaded") and
/// Page.navigationAborted events for the frame being navigated. Events for
/// other frames or in the wrong state are ignored.
///
/// String fields borrow from the caller (driver keeps frame_id stable as an
/// owned copy; event-derived values borrow the dispatch arena and must be
/// consumed before the next dispatch).
pub const Lifecycle = struct {
    pub const State = enum { idle, waiting, done, aborted };

    state: State = .idle,
    /// Main frame of the navigating page (owned by the driver, stable).
    frame_id: []const u8 = "",
    /// Set from the navigate response or navigationStarted event (borrowed).
    navigation_id: []const u8 = "",
    /// errorText from navigationAborted (borrowed; copy before next dispatch).
    abort_text: []const u8 = "",

    /// Enter the waiting state for `frame_id`. No-op unless idle/done/aborted.
    pub fn begin(self: *Lifecycle, frame_id: []const u8) void {
        if (self.state == .waiting) return;
        self.frame_id = frame_id;
        self.state = .waiting;
    }

    /// Record the navigation id (from the navigate response or event).
    pub fn setNavigationId(self: *Lifecycle, frame_id: []const u8, navigation_id: []const u8) void {
        if (self.state != .waiting or !std.mem.eql(u8, frame_id, self.frame_id)) return;
        self.navigation_id = navigation_id;
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
        self.abort_text = error_text;
    }

    /// Page.navigationStarted — adopt the navigation id if not yet known.
    pub fn onNavigationStarted(self: *Lifecycle, frame_id: []const u8, navigation_id: []const u8) void {
        if (self.state != .waiting or !std.mem.eql(u8, frame_id, self.frame_id)) return;
        self.navigation_id = navigation_id;
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
    var lc = Lifecycle{};
    lc.begin("f1");
    try testing.expectEqual(Lifecycle.State.waiting, lc.state);
    lc.onLoad("f1");
    try testing.expectEqual(Lifecycle.State.done, lc.state);
}

test "lifecycle: begin -> abort -> aborted with text" {
    var lc = Lifecycle{};
    lc.begin("f1");
    lc.onAbort("f1", "NS_BINDING_ABORTED");
    try testing.expectEqual(Lifecycle.State.aborted, lc.state);
    try testing.expectEqualStrings("NS_BINDING_ABORTED", lc.abort_text);
}

test "lifecycle: load for another frame is ignored" {
    var lc = Lifecycle{};
    lc.begin("f1");
    lc.onLoad("f2");
    try testing.expectEqual(Lifecycle.State.waiting, lc.state);
    lc.onLoad("f1");
    try testing.expectEqual(Lifecycle.State.done, lc.state);
}

test "lifecycle: events in idle state are no-ops" {
    var lc = Lifecycle{};
    lc.onLoad("f1");
    lc.onAbort("f1", "x");
    try testing.expectEqual(Lifecycle.State.idle, lc.state);
}

test "lifecycle: done state is terminal for later events" {
    var lc = Lifecycle{};
    lc.begin("f1");
    lc.onLoad("f1");
    lc.onAbort("f1", "late");
    try testing.expectEqual(Lifecycle.State.done, lc.state);
}

test "lifecycle: navigationStarted records navigation id" {
    var lc = Lifecycle{};
    lc.begin("f1");
    lc.onNavigationStarted("f1", "nav-x");
    try testing.expectEqualStrings("nav-x", lc.navigation_id);
}

test "lifecycle: setNavigationId for wrong frame ignored" {
    var lc = Lifecycle{};
    lc.begin("f1");
    lc.setNavigationId("f2", "nav-y");
    try testing.expectEqualStrings("", lc.navigation_id);
}

test "lifecycle: begin during waiting is a no-op" {
    var lc = Lifecycle{};
    lc.begin("f1");
    lc.begin("f2");
    try testing.expectEqualStrings("f1", lc.frame_id);
    try testing.expectEqual(Lifecycle.State.waiting, lc.state);
}

test "page: method name matches schema" {
    try testing.expectEqualStrings("Page.navigate", method_navigate);
}
