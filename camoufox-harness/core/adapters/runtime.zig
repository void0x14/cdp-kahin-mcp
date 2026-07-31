//! Juggler Runtime domain adapter (Faz 2): evaluate.
//!
//! Schema facts (protocol/schema/juggler-schema.json, Runtime domain):
//!   evaluate(executionContextId: string, expression: string,
//!            returnByValue?: boolean) -> result: Runtime.RemoteObject,
//!                                        exceptionDetails: Runtime.ExceptionDetails
//!   — note: `frameId` is NOT an evaluate param; the caller must resolve an
//!     execution context id first (Runtime.executionContextCreated event).
//!
//!   RemoteObject: {type?, subtype?, objectId?, unserializableValue?, value: any}
//!   ExceptionDetails: {text?, stack?, value?}
//!
//!   events: executionContextCreated(executionContextId, auxData{frameId?, name?}),
//!           executionContextsCleared

const std = @import("std");
const Allocator = std.mem.Allocator;

pub const method_evaluate = "Runtime.evaluate";

/// Runtime.evaluate params. `return_by_value` omitted when null.
pub fn evaluateParams(
    allocator: Allocator,
    execution_context_id: []const u8,
    expression: []const u8,
    return_by_value: ?bool,
) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{
            .executionContextId = execution_context_id,
            .expression = expression,
            .returnByValue = return_by_value,
        },
        .{ .emit_null_optional_fields = false },
    );
}

/// Outcome of a successful Runtime.evaluate call.
///
/// `value_json` is the re-serialized `result.result.value` (e.g. "2" for
/// 1+1); empty string when the value is absent. When the evaluation threw,
/// `exception_text`/`exception_stack` carry ExceptionDetails instead.
/// All fields owned; call `deinit`.
pub const EvaluateResult = struct {
    value_json: []u8 = "",
    exception_text: ?[]u8 = null,
    exception_stack: ?[]u8 = null,

    pub fn deinit(self: *EvaluateResult, allocator: Allocator) void {
        if (self.value_json.len > 0) allocator.free(self.value_json);
        if (self.exception_text) |t| allocator.free(t);
        if (self.exception_stack) |s| allocator.free(s);
        self.* = .{};
    }
};

/// Parse a Runtime.evaluate response.
///   {"id":N,"result":{"result":{"type":"number","value":2}}}          -> value_json "2"
///   {"id":N,"result":{"result":{"type":"undefined"},
///                     "exceptionDetails":{"text":"boom","stack":"..."}}} -> exception
pub fn parseEvaluateResult(allocator: Allocator, response_raw: []const u8) !EvaluateResult {
    const parsed = try std.json.parseFromSlice(std.json.Value, allocator, response_raw, .{});
    defer parsed.deinit();
    const root = parsed.value;
    if (root != .object) return error.InvalidResponse;
    if (root.object.get("error")) |_| return error.JugglerError;
    const result = root.object.get("result") orelse return error.InvalidResponse;
    if (result != .object) return error.InvalidResponse;

    var out = EvaluateResult{};
    errdefer out.deinit(allocator);

    if (result.object.get("exceptionDetails")) |ed| {
        if (ed != .object) return error.InvalidResponse;
        if (ed.object.get("text")) |t| {
            if (t != .string) return error.InvalidResponse;
            out.exception_text = try allocator.dupe(u8, t.string);
        }
        if (ed.object.get("stack")) |s| {
            if (s != .string) return error.InvalidResponse;
            out.exception_stack = try allocator.dupe(u8, s.string);
        }
        return out;
    }

    const remote = result.object.get("result") orelse return error.InvalidResponse;
    if (remote != .object) return error.InvalidResponse;
    if (remote.object.get("value")) |v| {
        out.value_json = try std.json.Stringify.valueAlloc(allocator, v, .{ .emit_null_optional_fields = false });
    }
    return out;
}

const testing = std.testing;

test "runtime: evaluate params carry schema names" {
    const p = try evaluateParams(testing.allocator, "ctx-9", "1+1", true);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings(
        "{\"executionContextId\":\"ctx-9\",\"expression\":\"1+1\",\"returnByValue\":true}",
        p,
    );
}

test "runtime: evaluate omits returnByValue when null" {
    const p = try evaluateParams(testing.allocator, "ctx-9", "1+1", null);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings("{\"executionContextId\":\"ctx-9\",\"expression\":\"1+1\"}", p);
}

test "runtime: parse numeric value" {
    var res = try parseEvaluateResult(
        testing.allocator,
        "{\"id\":5,\"result\":{\"result\":{\"type\":\"number\",\"value\":2}}}",
    );
    defer res.deinit(testing.allocator);
    try testing.expectEqualStrings("2", res.value_json);
    try testing.expect(res.exception_text == null);
}

test "runtime: parse string value" {
    var res = try parseEvaluateResult(
        testing.allocator,
        "{\"id\":5,\"result\":{\"result\":{\"type\":\"string\",\"value\":\"hello\"}}}",
    );
    defer res.deinit(testing.allocator);
    try testing.expectEqualStrings("\"hello\"", res.value_json);
}

test "runtime: parse boolean value" {
    var res = try parseEvaluateResult(
        testing.allocator,
        "{\"id\":5,\"result\":{\"result\":{\"type\":\"boolean\",\"value\":false}}}",
    );
    defer res.deinit(testing.allocator);
    try testing.expectEqualStrings("false", res.value_json);
}

test "runtime: missing value yields empty" {
    var res = try parseEvaluateResult(
        testing.allocator,
        "{\"id\":5,\"result\":{\"result\":{\"type\":\"undefined\"}}}",
    );
    defer res.deinit(testing.allocator);
    try testing.expectEqualStrings("", res.value_json);
}

test "runtime: exception details captured" {
    var res = try parseEvaluateResult(
        testing.allocator,
        "{\"id\":5,\"result\":{\"result\":{\"type\":\"undefined\"},\"exceptionDetails\":{\"text\":\"ReferenceError: x is not defined\",\"stack\":\"at <anonymous>\"}}}",
    );
    defer res.deinit(testing.allocator);
    try testing.expectEqualStrings("", res.value_json);
    try testing.expectEqualStrings("ReferenceError: x is not defined", res.exception_text.?);
    try testing.expectEqualStrings("at <anonymous>", res.exception_stack.?);
}

test "runtime: parse rejects error responses" {
    try testing.expectError(
        error.JugglerError,
        parseEvaluateResult(testing.allocator, "{\"id\":5,\"error\":{\"code\":-32601}}"),
    );
}

test "runtime: method name matches schema" {
    try testing.expectEqualStrings("Runtime.evaluate", method_evaluate);
}
