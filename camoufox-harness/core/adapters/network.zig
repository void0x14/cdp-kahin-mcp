//! Juggler Network adapter (Faz 3 + Faz 4).
//!
//! Schema facts (Network domain events):
//!   requestWillBeSent(frameId?, requestId, redirectedFrom?, postData?,
//!                     headers: Network.HTTPHeader[], isIntercepted, url,
//!                     method, navigationId?, cause, internalCause)
//!   responseReceived(securityDetails?, requestId, fromCache, remoteIPAddress?,
//!                    remotePort?, status, statusText, headers,
//!                    timing: Network.ResourceTiming, fromServiceWorker)
//!   requestFinished(requestId, responseEndTime, transferSize,
//!                   encodedBodySize, protocolVersion?)
//!   requestFailed(requestId, errorCode)
//!
//! Faz 3: passive passthrough. The Juggler event names already match their
//! CDP twins verbatim (requestWillBeSent / responseReceived /
//! requestFinished / requestFailed), so Kahin's `Network.*`-prefix
//! collector works unchanged: pure passthrough, no rename, no re-shape.
//! `Network.enable` has no wire equivalent — Juggler emits these events
//! unconditionally (driver.enableNetwork is a documented no-op).
//!
//! Faz 4: interception. Schema facts (methods, Network domain):
//!   setRequestInterception(enabled: boolean)            // page-scoped toggle
//!   abortInterceptedRequest(requestId: string, errorCode: string)
//!   resumeInterceptedRequest(requestId: string, url?, method?, headers?,
//!                            postData?)                 // the "continue"
//!   fulfillInterceptedRequest(requestId: string, status: number,
//!                             statusText: string, headers: HTTPHeader[],
//!                             base64body?: string)
//!   getResponseBody(requestId: string) -> base64body, evicted?   // NOT wired
//! and Browser domain:
//!   setRequestInterception(browserContextId?: string, enabled: boolean)
//!                          // context-scoped toggle
//!
//! There is NO `Network.requestIntercepted` event and NO
//! `Network.continueInterceptedRequest` method in this Juggler build. The
//! interception signal is `Network.requestWillBeSent` with
//! `isIntercepted: true` (the request is paused until the client calls one
//! of the three decision methods with its requestId). CDP's
//! requestIntercepted/continueInterceptedRequest therefore map onto
//! requestWillBeSent(isIntercepted) / resumeInterceptedRequest (see the
//! task-4 report mapping table). No invented names.

const std = @import("std");
const Allocator = std.mem.Allocator;

const browser = @import("browser.zig");

pub const event_request_will_be_sent = "Network.requestWillBeSent";
pub const event_response_received = "Network.responseReceived";
pub const event_request_finished = "Network.requestFinished";
pub const event_request_failed = "Network.requestFailed";

/// Faz 4 method names, verbatim from the schema.
pub const method_set_request_interception = "Network.setRequestInterception";
pub const method_abort_intercepted_request = "Network.abortInterceptedRequest";
pub const method_resume_intercepted_request = "Network.resumeInterceptedRequest";
pub const method_fulfill_intercepted_request = "Network.fulfillInterceptedRequest";
/// Context-scoped toggle (Browser domain, schema).
pub const method_set_request_interception_context = "Browser.setRequestInterception";

/// Page-scoped interception toggle params: {"enabled":<bool>}.
pub fn setInterceptionParams(allocator: Allocator, enabled: bool) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{ .enabled = enabled },
        .{ .emit_null_optional_fields = false },
    );
}

/// Context-scoped interception toggle params; browserContextId omitted when
/// null (default context): {"browserContextId"?:...,"enabled":<bool>}.
pub fn setContextInterceptionParams(
    allocator: Allocator,
    browser_context_id: ?[]const u8,
    enabled: bool,
) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{ .browserContextId = browser_context_id, .enabled = enabled },
        .{ .emit_null_optional_fields = false },
    );
}

/// Network.resumeInterceptedRequest params (the CDP continue equivalent).
/// All optional overrides omitted when null.
pub fn resumeParams(
    allocator: Allocator,
    request_id: []const u8,
    url: ?[]const u8,
    method: ?[]const u8,
    headers: ?[]const browser.Header,
    post_data: ?[]const u8,
) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{
            .requestId = request_id,
            .url = url,
            .method = method,
            .headers = headers,
            .postData = post_data,
        },
        .{ .emit_null_optional_fields = false },
    );
}

/// Network.fulfillInterceptedRequest params; base64body omitted when null.
pub fn fulfillParams(
    allocator: Allocator,
    request_id: []const u8,
    status: u32,
    status_text: []const u8,
    headers: []const browser.Header,
    base64_body: ?[]const u8,
) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{
            .requestId = request_id,
            .status = status,
            .statusText = status_text,
            .headers = headers,
            .base64body = base64_body,
        },
        .{ .emit_null_optional_fields = false },
    );
}

/// Network.abortInterceptedRequest params. errorCode is a Firefox
/// Components.results member (e.g. "NS_ERROR_ABORT") — the Juggler side
/// cancels the channel with it.
pub fn abortParams(
    allocator: Allocator,
    request_id: []const u8,
    error_code: []const u8,
) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{ .requestId = request_id, .errorCode = error_code },
        .{ .emit_null_optional_fields = false },
    );
}

/// One parsed Network.requestWillBeSent params object (the fields the
/// interception registry needs). All strings owned; call `deinit`.
pub const InterceptedInfo = struct {
    request_id: []u8,
    url: []u8,
    method: []u8,
    is_intercepted: bool,

    pub fn deinit(self: *InterceptedInfo, allocator: Allocator) void {
        allocator.free(self.request_id);
        allocator.free(self.url);
        allocator.free(self.method);
        self.* = undefined;
    }
};

/// Parse `isIntercepted`/`requestId`/`url`/`method` out of a
/// Network.requestWillBeSent wire event. Malformed events return
/// error.InvalidEvent (callers treat interception registration as
/// non-fatal).
pub fn parseIntercepted(allocator: Allocator, raw: []const u8) !InterceptedInfo {
    const parsed = std.json.parseFromSlice(std.json.Value, allocator, raw, .{}) catch return error.InvalidEvent;
    defer parsed.deinit();
    const root = parsed.value;
    if (root != .object) return error.InvalidEvent;
    const params = root.object.get("params") orelse return error.InvalidEvent;
    if (params != .object) return error.InvalidEvent;
    const request_id = params.object.get("requestId") orelse return error.InvalidEvent;
    const url = params.object.get("url") orelse return error.InvalidEvent;
    const method = params.object.get("method") orelse return error.InvalidEvent;
    const intercepted = params.object.get("isIntercepted") orelse return error.InvalidEvent;
    if (request_id != .string or url != .string or method != .string or intercepted != .bool) {
        return error.InvalidEvent;
    }
    return .{
        .request_id = try allocator.dupe(u8, request_id.string),
        .url = try allocator.dupe(u8, url.string),
        .method = try allocator.dupe(u8, method.string),
        .is_intercepted = intercepted.bool,
    };
}

const testing = std.testing;

test "network: event names match schema (passthrough invariant)" {
    try testing.expectEqualStrings("Network.requestWillBeSent", event_request_will_be_sent);
    try testing.expectEqualStrings("Network.responseReceived", event_response_received);
    try testing.expectEqualStrings("Network.requestFinished", event_request_finished);
    try testing.expectEqualStrings("Network.requestFailed", event_request_failed);
}

test "network: passthrough events keep the Network. prefix (Kahin collector contract)" {
    try testing.expect(std.mem.startsWith(u8, event_request_will_be_sent, "Network."));
    try testing.expect(std.mem.startsWith(u8, event_response_received, "Network."));
    try testing.expect(std.mem.startsWith(u8, event_request_finished, "Network."));
    try testing.expect(std.mem.startsWith(u8, event_request_failed, "Network."));
}

test "network: interception method names match schema" {
    try testing.expectEqualStrings("Network.setRequestInterception", method_set_request_interception);
    try testing.expectEqualStrings("Network.abortInterceptedRequest", method_abort_intercepted_request);
    try testing.expectEqualStrings("Network.resumeInterceptedRequest", method_resume_intercepted_request);
    try testing.expectEqualStrings("Network.fulfillInterceptedRequest", method_fulfill_intercepted_request);
    try testing.expectEqualStrings("Browser.setRequestInterception", method_set_request_interception_context);
}

test "network: no requestIntercepted event / continue method in schema (Faz 4 fact)" {
    // Regression guard: this Juggler build has no such names — the
    // interception signal is requestWillBeSent.isIntercepted and the
    // continue is resumeInterceptedRequest. If a future schema gains them,
    // this test forces a deliberate decision before wiring changes.
    try testing.expect(!std.mem.eql(u8, event_request_will_be_sent, "Network.requestIntercepted"));
    try testing.expect(!std.mem.eql(u8, method_resume_intercepted_request, "Network.continueInterceptedRequest"));
}

test "network: setInterception params carry schema name enabled" {
    const p = try setInterceptionParams(testing.allocator, true);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings("{\"enabled\":true}", p);
    const p2 = try setInterceptionParams(testing.allocator, false);
    defer testing.allocator.free(p2);
    try testing.expectEqualStrings("{\"enabled\":false}", p2);
}

test "network: setContextInterception omits browserContextId when null" {
    const p = try setContextInterceptionParams(testing.allocator, null, true);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings("{\"enabled\":true}", p);
}

test "network: setContextInterception carries browserContextId" {
    const p = try setContextInterceptionParams(testing.allocator, "ctx-1", false);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings("{\"browserContextId\":\"ctx-1\",\"enabled\":false}", p);
}

test "network: resume params minimal (continue passthrough)" {
    const p = try resumeParams(testing.allocator, "r-1", null, null, null, null);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings("{\"requestId\":\"r-1\"}", p);
}

test "network: resume params carry all optional overrides" {
    const headers = [_]browser.Header{.{ .name = "X-Foo", .value = "bar" }};
    const p = try resumeParams(testing.allocator, "r-1", "http://x/2", "POST", &headers, "body");
    defer testing.allocator.free(p);
    try testing.expectEqualStrings(
        "{\"requestId\":\"r-1\",\"url\":\"http://x/2\",\"method\":\"POST\",\"headers\":[{\"name\":\"X-Foo\",\"value\":\"bar\"}],\"postData\":\"body\"}",
        p,
    );
}

test "network: fulfill params with base64 body" {
    const headers = [_]browser.Header{.{ .name = "Content-Type", .value = "text/javascript" }};
    const p = try fulfillParams(testing.allocator, "r-1", 200, "OK", &headers, "d2luZG93Lng9MQ==");
    defer testing.allocator.free(p);
    try testing.expectEqualStrings(
        "{\"requestId\":\"r-1\",\"status\":200,\"statusText\":\"OK\",\"headers\":[{\"name\":\"Content-Type\",\"value\":\"text/javascript\"}],\"base64body\":\"d2luZG93Lng9MQ==\"}",
        p,
    );
}

test "network: fulfill params omit base64body when null" {
    const headers = [_]browser.Header{};
    const p = try fulfillParams(testing.allocator, "r-1", 204, "No Content", &headers, null);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings("{\"requestId\":\"r-1\",\"status\":204,\"statusText\":\"No Content\",\"headers\":[]}", p);
}

test "network: abort params carry schema name errorCode" {
    const p = try abortParams(testing.allocator, "r-1", "NS_ERROR_ABORT");
    defer testing.allocator.free(p);
    try testing.expectEqualStrings("{\"requestId\":\"r-1\",\"errorCode\":\"NS_ERROR_ABORT\"}", p);
}

test "network: parseIntercepted extracts ids from intercepted request" {
    var info = try parseIntercepted(
        testing.allocator,
        "{\"method\":\"Network.requestWillBeSent\",\"sessionId\":\"s1\",\"params\":{\"requestId\":\"r-1\",\"isIntercepted\":true,\"url\":\"http://127.0.0.1:8333/ok.png\",\"method\":\"GET\",\"headers\":[],\"cause\":\"script\",\"internalCause\":\"script\"}}",
    );
    defer info.deinit(testing.allocator);
    try testing.expect(info.is_intercepted);
    try testing.expectEqualStrings("r-1", info.request_id);
    try testing.expectEqualStrings("http://127.0.0.1:8333/ok.png", info.url);
    try testing.expectEqualStrings("GET", info.method);
}

test "network: parseIntercepted reports non-intercepted flag" {
    var info = try parseIntercepted(
        testing.allocator,
        "{\"method\":\"Network.requestWillBeSent\",\"params\":{\"requestId\":\"r-2\",\"isIntercepted\":false,\"url\":\"http://x/\",\"method\":\"GET\",\"headers\":[],\"cause\":\"script\",\"internalCause\":\"script\"}}",
    );
    defer info.deinit(testing.allocator);
    try testing.expect(!info.is_intercepted);
    try testing.expectEqualStrings("r-2", info.request_id);
}

test "network: parseIntercepted rejects malformed events" {
    try testing.expectError(
        error.InvalidEvent,
        parseIntercepted(testing.allocator, "{\"method\":\"Network.requestWillBeSent\",\"params\":{}}"),
    );
    try testing.expectError(
        error.InvalidEvent,
        parseIntercepted(testing.allocator, "not json"),
    );
}
