//! Juggler console adapter (Faz 3).
//!
//! Schema fact: there is NO `Console` domain and no `Console.messageAdded`.
//! The console event is `Runtime.console`:
//!   params: {executionContextId: string, args: Runtime.RemoteObject[],
//!            type: string, location: Runtime.ScriptLocation}
//!
//! Kahin's consumer expects CDP `Console.messageAdded` events whose
//! `params.message` object it appends verbatim (oracle.py `_on_console_event`).
//! This adapter normalizes the Juggler event into that message shape:
//!   Juggler type  -> message.level   (passthrough: "log"/"warning"/"error"/...)
//!   args[].value  -> message.text    (joined with " "; non-string values
//!                                     re-serialized as JSON)
//!   executionContextId -> message.executionContextId
//!   location       -> message.location{url, lineNumber, columnNumber}
//!
//! `Console.enable` has no wire equivalent — Runtime.console events flow
//! unconditionally (driver.enableConsole is a documented no-op).

const std = @import("std");
const Allocator = std.mem.Allocator;

/// Normalized console message (CDP Console.messageAdded -> params.message
/// shape). All fields owned; call `deinit`.
pub const Message = struct {
    level: []u8 = "",
    text: []u8 = "",
    execution_context_id: []u8 = "",
    location_url: []u8 = "",
    location_line: i64 = 0,
    location_column: i64 = 0,

    pub fn deinit(self: *Message, allocator: Allocator) void {
        if (self.level.len > 0) allocator.free(self.level);
        if (self.text.len > 0) allocator.free(self.text);
        if (self.execution_context_id.len > 0) allocator.free(self.execution_context_id);
        if (self.location_url.len > 0) allocator.free(self.location_url);
        self.* = .{};
    }
};

/// Parse a raw `Runtime.console` event and normalize it into a Message.
/// The input must be the full wire event
///   {"method":"Runtime.console","params":{...},"sessionId":"..."}
/// (anything else -> error.InvalidEvent).
pub fn normalize(allocator: Allocator, raw_event: []const u8) !Message {
    const parsed = try std.json.parseFromSlice(std.json.Value, allocator, raw_event, .{});
    defer parsed.deinit();
    const root = parsed.value;
    if (root != .object) return error.InvalidEvent;
    const method = root.object.get("method") orelse return error.InvalidEvent;
    if (method != .string or !std.mem.eql(u8, method.string, "Runtime.console")) return error.InvalidEvent;
    const params = root.object.get("params") orelse return error.InvalidEvent;
    if (params != .object) return error.InvalidEvent;

    var msg = Message{};
    errdefer msg.deinit(allocator);

    if (params.object.get("type")) |t| {
        if (t != .string) return error.InvalidEvent;
        msg.level = try allocator.dupe(u8, t.string);
    }
    if (params.object.get("executionContextId")) |c| {
        if (c != .string) return error.InvalidEvent;
        msg.execution_context_id = try allocator.dupe(u8, c.string);
    }
    if (params.object.get("location")) |loc| {
        if (loc == .object) {
            if (loc.object.get("url")) |u| {
                if (u == .string) msg.location_url = try allocator.dupe(u8, u.string);
            }
            if (loc.object.get("lineNumber")) |ln| {
                if (ln == .integer) msg.location_line = ln.integer;
            }
            if (loc.object.get("columnNumber")) |cn| {
                if (cn == .integer) msg.location_column = cn.integer;
            }
        }
    }

    // args: join every RemoteObject's `value`. String values join raw;
    // anything else (number/bool/object/...) is re-serialized as JSON.
    var text = std.array_list.Aligned(u8, null).empty;
    defer text.deinit(allocator);
    if (params.object.get("args")) |args| {
        if (args != .array) return error.InvalidEvent;
        for (args.array.items, 0..) |arg, i| {
            if (i > 0) try text.appendSlice(allocator, " ");
            if (arg != .object) continue;
            const value = arg.object.get("value") orelse continue;
            if (value == .string) {
                try text.appendSlice(allocator, value.string);
            } else {
                const s = try std.json.Stringify.valueAlloc(allocator, value, .{});
                defer allocator.free(s);
                try text.appendSlice(allocator, s);
            }
        }
    }
    msg.text = try text.toOwnedSlice(allocator);
    return msg;
}

/// Serialize a Message into the CDP `Console.messageAdded` params.message
/// object shape (owned JSON):
///   {"level":..,"text":..,"executionContextId":..,
///    "location":{"url":..,"lineNumber":..,"columnNumber":..}}
pub fn toJson(allocator: Allocator, msg: *const Message) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{
            .level = msg.level,
            .text = msg.text,
            .executionContextId = msg.execution_context_id,
            .location = .{
                .url = msg.location_url,
                .lineNumber = msg.location_line,
                .columnNumber = msg.location_column,
            },
        },
        .{ .emit_null_optional_fields = false },
    );
}

const testing = std.testing;

const sample_event =
    \\{"method":"Runtime.console","params":{"executionContextId":"id-3","args":[{"type":"string","value":"hi"},{"type":"number","value":42}],"type":"log","location":{"url":"http://127.0.0.1:8941/page1.html","lineNumber":5,"columnNumber":2}},"sessionId":"s1"}
;

test "console: normalize Runtime.console into message shape" {
    var m = try normalize(testing.allocator, sample_event);
    defer m.deinit(testing.allocator);
    try testing.expectEqualStrings("log", m.level);
    try testing.expectEqualStrings("hi 42", m.text);
    try testing.expectEqualStrings("id-3", m.execution_context_id);
    try testing.expectEqualStrings("http://127.0.0.1:8941/page1.html", m.location_url);
    try testing.expectEqual(@as(i64, 5), m.location_line);
    try testing.expectEqual(@as(i64, 2), m.location_column);
}

test "console: error level and object arg re-serialized as JSON" {
    const ev =
        \\{"method":"Runtime.console","params":{"executionContextId":"id-1","args":[{"type":"object","value":{"a":1}}],"type":"error","location":{"url":"","lineNumber":0,"columnNumber":0}}}
    ;
    var m = try normalize(testing.allocator, ev);
    defer m.deinit(testing.allocator);
    try testing.expectEqualStrings("error", m.level);
    try testing.expectEqualStrings("{\"a\":1}", m.text);
}

test "console: no args yields empty text" {
    const ev =
        \\{"method":"Runtime.console","params":{"executionContextId":"id-1","args":[],"type":"log","location":{"url":"","lineNumber":0,"columnNumber":0}}}
    ;
    var m = try normalize(testing.allocator, ev);
    defer m.deinit(testing.allocator);
    try testing.expectEqualStrings("", m.text);
    try testing.expectEqualStrings("log", m.level);
}

test "console: toJson matches Console.messageAdded params.message shape" {
    var m = try normalize(testing.allocator, sample_event);
    defer m.deinit(testing.allocator);
    const j = try toJson(testing.allocator, &m);
    defer testing.allocator.free(j);
    try testing.expectEqualStrings(
        "{\"level\":\"log\",\"text\":\"hi 42\",\"executionContextId\":\"id-3\",\"location\":{\"url\":\"http://127.0.0.1:8941/page1.html\",\"lineNumber\":5,\"columnNumber\":2}}",
        j,
    );
}

test "console: normalize rejects non-console input" {
    try testing.expectError(
        error.InvalidEvent,
        normalize(testing.allocator, "{\"method\":\"Page.ready\",\"params\":{}}"),
    );
}
