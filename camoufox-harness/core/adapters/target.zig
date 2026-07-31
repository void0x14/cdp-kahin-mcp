//! Juggler Target adapter (Faz 3).
//!
//! Schema fact: there is NO Target domain in Juggler. The CDP Target tools
//! map onto driver state + existing Browser/Page methods:
//!   Target.getTargets     -> generated from the driver's page map
//!                            (Browser.attachedToTarget registrations +
//!                            navigationCommitted URLs). No wire call.
//!   Target.createTarget   -> Browser.newPage (default context; Juggler's
//!                            newPage has no url param, so the url is a
//!                            separate Page.navigate — driver.createTarget
//!                            does both)
//!   Target.closeTarget    -> Page.close (schema: runBeforeUnload?: boolean)
//!                            on the target's session, then wait for
//!                            Browser.detachedFromTarget cleanup
//!
//! Output shapes follow CDP so Kahin's consumers (oracle.py get_session /
//! create_session / kill_session) parse them unchanged.

const std = @import("std");
const Allocator = std.mem.Allocator;

/// One entry of a CDP Target.getTargets `targetInfos` array. `url`/`type`
/// are always present ("" / "page"); `browser_context_id` omitted when null
/// (Stringify with emit_null_optional_fields=false).
pub const TargetInfo = struct {
    targetId: []const u8,
    type: []const u8 = "page",
    browserContextId: ?[]const u8 = null,
    url: []const u8 = "",
};

/// Build the CDP `Target.getTargets` result:
///   {"targetInfos":[{"targetId":..,"type":"page","browserContextId":..,"url":..}]}
pub fn buildTargetInfos(allocator: Allocator, infos: []const TargetInfo) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{ .targetInfos = infos },
        .{ .emit_null_optional_fields = false },
    );
}

/// CDP Target.createTarget result: {"targetId": "..."}.
pub fn buildCreateTargetResult(allocator: Allocator, target_id: []const u8) Allocator.Error![]u8 {
    return std.fmt.allocPrint(allocator, "{{\"targetId\":\"{s}\"}}", .{target_id});
}

/// CDP Target.closeTarget result: {}.
pub fn closeTargetResult() []const u8 {
    return "{}";
}

const testing = std.testing;

test "target: buildTargetInfos shapes CDP targetInfos with context and url" {
    const infos = [_]TargetInfo{
        .{ .targetId = "t-1", .url = "http://x/", .browserContextId = "ctx-1" },
        .{ .targetId = "t-2" },
    };
    const j = try buildTargetInfos(testing.allocator, &infos);
    defer testing.allocator.free(j);
    try testing.expectEqualStrings(
        "{\"targetInfos\":[{\"targetId\":\"t-1\",\"type\":\"page\",\"browserContextId\":\"ctx-1\",\"url\":\"http://x/\"},{\"targetId\":\"t-2\",\"type\":\"page\",\"url\":\"\"}]}",
        j,
    );
}

test "target: buildCreateTargetResult matches CDP result shape" {
    const j = try buildCreateTargetResult(testing.allocator, "t-9");
    defer testing.allocator.free(j);
    try testing.expectEqualStrings("{\"targetId\":\"t-9\"}", j);
}

test "target: closeTargetResult is empty object" {
    try testing.expectEqualStrings("{}", closeTargetResult());
}
