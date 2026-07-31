//! Juggler Browser domain adapter (Faz 2).
//!
//! Pure param marshaling: builds the `params` object of each Browser method
//! request. Method names and param names are taken verbatim from
//! protocol/schema/juggler-schema.json (Faz 0 output) — do NOT guess.
//!
//! The driver (../driver.zig) wraps these into full requests and handles the
//! wire I/O: {"id":N,"method":"Browser.<m>","params":<params>}.
//!
//! Schema facts used here (juggler-schema.json, Browser domain):
//!   enable(attachToDefaultContext: boolean, userPrefs?: UserPreference[])
//!   createBrowserContext(removeOnDetach?: boolean) -> browserContextId
//!   removeBrowserContext(browserContextId: string)
//!   newPage(browserContextId?: string) -> targetId        // NOTE: no `url` param
//!   close()
//!   setExtraHTTPHeaders(browserContextId?: string, headers: Network.HTTPHeader[])
//!   events: attachedToTarget(sessionId, targetInfo), detachedFromTarget(sessionId, targetId)

const std = @import("std");
const Allocator = std.mem.Allocator;

pub const method_enable = "Browser.enable";
pub const method_create_browser_context = "Browser.createBrowserContext";
pub const method_remove_browser_context = "Browser.removeBrowserContext";
pub const method_new_page = "Browser.newPage";
pub const method_close = "Browser.close";
pub const method_set_extra_http_headers = "Browser.setExtraHTTPHeaders";

/// Network.HTTPHeader per schema: {name: string, value: string}.
pub const Header = struct {
    name: []const u8,
    value: []const u8,
};

/// Browser.enable params: {"attachToDefaultContext":<bool>}
pub fn enableParams(allocator: Allocator, attach_to_default_context: bool) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{ .attachToDefaultContext = attach_to_default_context },
        .{ .emit_null_optional_fields = false },
    );
}

/// Browser.createBrowserContext params; `remove_on_detach` omitted when null.
pub fn createBrowserContextParams(allocator: Allocator, remove_on_detach: ?bool) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{ .removeOnDetach = remove_on_detach },
        .{ .emit_null_optional_fields = false },
    );
}

/// Browser.removeBrowserContext params.
pub fn removeBrowserContextParams(allocator: Allocator, browser_context_id: []const u8) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{ .browserContextId = browser_context_id },
        .{ .emit_null_optional_fields = false },
    );
}

/// Browser.newPage params; `browser_context_id` omitted when null (default context).
pub fn newPageParams(allocator: Allocator, browser_context_id: ?[]const u8) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{ .browserContextId = browser_context_id },
        .{ .emit_null_optional_fields = false },
    );
}

/// Browser.close params — empty object.
pub fn closeParams() []const u8 {
    return "{}";
}

/// Browser.setExtraHTTPHeaders params.
pub fn setExtraHTTPHeadersParams(
    allocator: Allocator,
    browser_context_id: ?[]const u8,
    headers: []const Header,
) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{ .browserContextId = browser_context_id, .headers = headers },
        .{ .emit_null_optional_fields = false },
    );
}

/// Parse the `browserContextId` string out of a createBrowserContext response.
pub fn parseBrowserContextId(allocator: Allocator, response_raw: []const u8) ![]u8 {
    const parsed = try std.json.parseFromSlice(std.json.Value, allocator, response_raw, .{});
    defer parsed.deinit();
    const root = parsed.value;
    if (root != .object) return error.InvalidResponse;
    if (root.object.get("error")) |_| return error.JugglerError;
    const result = root.object.get("result") orelse return error.InvalidResponse;
    if (result != .object) return error.InvalidResponse;
    const id = result.object.get("browserContextId") orelse return error.InvalidResponse;
    if (id != .string) return error.InvalidResponse;
    return allocator.dupe(u8, id.string);
}

/// Parse the `targetId` string out of a newPage response.
pub fn parseTargetId(allocator: Allocator, response_raw: []const u8) ![]u8 {
    const parsed = try std.json.parseFromSlice(std.json.Value, allocator, response_raw, .{});
    defer parsed.deinit();
    const root = parsed.value;
    if (root != .object) return error.InvalidResponse;
    if (root.object.get("error")) |_| return error.JugglerError;
    const result = root.object.get("result") orelse return error.InvalidResponse;
    if (result != .object) return error.InvalidResponse;
    const id = result.object.get("targetId") orelse return error.InvalidResponse;
    if (id != .string) return error.InvalidResponse;
    return allocator.dupe(u8, id.string);
}

const testing = std.testing;

test "browser: enable params carry schema name attachToDefaultContext" {
    const p = try enableParams(testing.allocator, true);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings("{\"attachToDefaultContext\":true}", p);
}

test "browser: createBrowserContext omits removeOnDetach when null" {
    const p = try createBrowserContextParams(testing.allocator, null);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings("{}", p);
}

test "browser: createBrowserContext with removeOnDetach" {
    const p = try createBrowserContextParams(testing.allocator, true);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings("{\"removeOnDetach\":true}", p);
}

test "browser: removeBrowserContext param name from schema" {
    const p = try removeBrowserContextParams(testing.allocator, "ctx-1");
    defer testing.allocator.free(p);
    try testing.expectEqualStrings("{\"browserContextId\":\"ctx-1\"}", p);
}

test "browser: newPage omits browserContextId when null" {
    const p = try newPageParams(testing.allocator, null);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings("{}", p);
}

test "browser: newPage carries browserContextId" {
    const p = try newPageParams(testing.allocator, "ctx-1");
    defer testing.allocator.free(p);
    try testing.expectEqualStrings("{\"browserContextId\":\"ctx-1\"}", p);
}

test "browser: newPage has no url param (schema fact)" {
    // Regression guard: Juggler's Browser.newPage takes browserContextId only.
    const p = try newPageParams(testing.allocator, "ctx-1");
    defer testing.allocator.free(p);
    try testing.expect(std.mem.indexOf(u8, p, "url") == null);
}

test "browser: setExtraHTTPHeaders marshals HTTPHeader array" {
    const headers = [_]Header{.{ .name = "X-Foo", .value = "bar" }};
    const p = try setExtraHTTPHeadersParams(testing.allocator, "ctx-1", &headers);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings(
        "{\"browserContextId\":\"ctx-1\",\"headers\":[{\"name\":\"X-Foo\",\"value\":\"bar\"}]}",
        p,
    );
}

test "browser: setExtraHTTPHeaders without context" {
    const headers = [_]Header{};
    const p = try setExtraHTTPHeadersParams(testing.allocator, null, &headers);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings("{\"headers\":[]}", p);
}

test "browser: close params is empty object" {
    try testing.expectEqualStrings("{}", closeParams());
}

test "browser: parseBrowserContextId" {
    const id = try parseBrowserContextId(testing.allocator, "{\"id\":2,\"result\":{\"browserContextId\":\"ctx-7\"}}");
    defer testing.allocator.free(id);
    try testing.expectEqualStrings("ctx-7", id);
}

test "browser: parseTargetId" {
    const id = try parseTargetId(testing.allocator, "{\"id\":3,\"result\":{\"targetId\":\"t-9\"}}");
    defer testing.allocator.free(id);
    try testing.expectEqualStrings("t-9", id);
}

test "browser: parse helpers reject error responses" {
    try testing.expectError(error.JugglerError, parseBrowserContextId(testing.allocator, "{\"id\":2,\"error\":{\"code\":-1}}"));
    try testing.expectError(error.JugglerError, parseTargetId(testing.allocator, "{\"id\":3,\"error\":{\"code\":-1}}"));
}

test "browser: method names match schema" {
    try testing.expectEqualStrings("Browser.enable", method_enable);
    try testing.expectEqualStrings("Browser.createBrowserContext", method_create_browser_context);
    try testing.expectEqualStrings("Browser.removeBrowserContext", method_remove_browser_context);
    try testing.expectEqualStrings("Browser.newPage", method_new_page);
    try testing.expectEqualStrings("Browser.close", method_close);
    try testing.expectEqualStrings("Browser.setExtraHTTPHeaders", method_set_extra_http_headers);
}
