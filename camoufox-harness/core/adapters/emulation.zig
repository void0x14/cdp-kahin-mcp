//! Juggler viewport/emulation adapter (Faz 3).
//!
//! CDP `Emulation.setDeviceMetricsOverride` has no direct Juggler twin, but
//! two schema methods cover it:
//!   Browser.setDefaultViewport(browserContextId?, viewport: Page.Viewport?)
//!       -> Page.Viewport = {viewportSize: Page.Size, deviceScaleFactor?}
//!   Page.setViewportSize(viewportSize: Page.Size|null)
//!
//! Mapping (CDP -> Juggler, schema-verified):
//!   width, height               -> viewport.viewportSize.{width,height}
//!   deviceScaleFactor           -> viewport.deviceScaleFactor
//!   mobile                      -> NOT in schema (no isMobile anywhere;
//!                                 closest is Browser.setTouchOverride,
//!                                 separate concern, not mapped)
//!   screenWidth, screenHeight   -> NOT in schema (not mapped)
//!   scale, screenOrientation, ... -> NOT in schema (not mapped)
//!
//! setDefaultViewport is context-wide (browserContextId scopes it);
//! setViewportSize is per-page. Both are used: the former for device-metric
//! emulation, the latter for the full-page screenshot size (Faz 3 brief).

const std = @import("std");
const Allocator = std.mem.Allocator;

pub const method_set_default_viewport = "Browser.setDefaultViewport";
pub const method_set_viewport_size = "Page.setViewportSize";

/// Page.Size per schema: {width: number, height: number}.
pub const Size = struct {
    width: f64,
    height: f64,
};

/// Page.Viewport per schema: {viewportSize: Page.Size, deviceScaleFactor?}.
pub const Viewport = struct {
    viewportSize: Size,
    deviceScaleFactor: ?f64 = null,
};

/// Browser.setDefaultViewport params. `browser_context_id` and the scale
/// factor are omitted when null (schema marks both optional).
pub fn setDefaultViewportParams(
    allocator: Allocator,
    browser_context_id: ?[]const u8,
    viewport: Viewport,
) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{ .browserContextId = browser_context_id, .viewport = viewport },
        .{ .emit_null_optional_fields = false },
    );
}

/// Page.setViewportSize params: {"viewportSize": Page.Size}.
pub fn setViewportSizeParams(allocator: Allocator, size: Size) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{ .viewportSize = size },
        .{ .emit_null_optional_fields = false },
    );
}

const testing = std.testing;

test "emulation: setDefaultViewport params carry schema names" {
    const p = try setDefaultViewportParams(
        testing.allocator,
        "ctx-1",
        .{ .viewportSize = .{ .width = 390, .height = 844 }, .deviceScaleFactor = 3 },
    );
    defer testing.allocator.free(p);
    try testing.expectEqualStrings(
        "{\"browserContextId\":\"ctx-1\",\"viewport\":{\"viewportSize\":{\"width\":390,\"height\":844},\"deviceScaleFactor\":3}}",
        p,
    );
}

test "emulation: setDefaultViewport omits deviceScaleFactor when null" {
    const p = try setDefaultViewportParams(
        testing.allocator,
        null,
        .{ .viewportSize = .{ .width = 1280, .height = 720 } },
    );
    defer testing.allocator.free(p);
    try testing.expectEqualStrings(
        "{\"viewport\":{\"viewportSize\":{\"width\":1280,\"height\":720}}}",
        p,
    );
}

test "emulation: setViewportSize params carry schema names" {
    const p = try setViewportSizeParams(testing.allocator, .{ .width = 100, .height = 50 });
    defer testing.allocator.free(p);
    try testing.expectEqualStrings("{\"viewportSize\":{\"width\":100,\"height\":50}}", p);
}

test "emulation: float sizes serialize without trailing zeros" {
    const p = try setViewportSizeParams(testing.allocator, .{ .width = 1280, .height = 720 });
    defer testing.allocator.free(p);
    try testing.expectEqualStrings("{\"viewportSize\":{\"width\":1280,\"height\":720}}", p);
}

test "emulation: method names match schema" {
    try testing.expectEqualStrings("Browser.setDefaultViewport", method_set_default_viewport);
    try testing.expectEqualStrings("Page.setViewportSize", method_set_viewport_size);
}
