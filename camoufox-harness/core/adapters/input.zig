//! Juggler Input adapter (Faz 4).
//!
//! Juggler has NO Input domain — the input methods live in the Page domain
//! (schema facts, juggler-schema.json Page domain):
//!   dispatchKeyEvent(type: string, key: string, keyCode: number,
//!                    location: number, code: string, repeat: boolean,
//!                    text?: string)                          // type: plain string
//!   dispatchMouseEvent(type: enum[mousedown|mousemove|mouseup],
//!                      button: number, x: number, y: number,
//!                      modifiers: number, clickCount?: number,
//!                      buttons: number)                      // buttons REQUIRED
//!   dispatchWheelEvent(x, y, deltaX, deltaY, deltaZ, modifiers: number)
//!   insertText(text: string)
//!
//! This adapter (a) builds the wire params with schema names verbatim, and
//! (b) maps CDP Input.* argument spellings onto the Juggler ones (type
//! strings, button strings -> numbers, buttons bitmask derivation). The
//! CDP modifiers bitmask (Alt=1, Ctrl=2, Meta=4, Shift=8) matches the
//! Juggler one (Playwright convention) and passes through unchanged.
//!
//! CDP -> Juggler mapping table (see task-4 report):
//!   Input.dispatchKeyEvent(type keyDown)   -> Page.dispatchKeyEvent(type "keydown")
//!   Input.dispatchKeyEvent(type keyUp)     -> Page.dispatchKeyEvent(type "keyup")
//!   Input.dispatchKeyEvent(type rawKeyDown)-> Page.dispatchKeyEvent(type "rawkeydown")
//!   Input.dispatchKeyEvent(type char)      -> Page.dispatchKeyEvent(type "char")
//!   windowsVirtualKeyCode                  -> keyCode
//!   autoRepeat                             -> repeat
//!   Input.dispatchMouseEvent(mousePressed) -> Page.dispatchMouseEvent("mousedown")
//!   Input.dispatchMouseEvent(mouseReleased)-> Page.dispatchMouseEvent("mouseup")
//!   Input.dispatchMouseEvent(mouseMoved)   -> Page.dispatchMouseEvent("mousemove")
//!   button "left"|"middle"|"right"|"back"|"forward"|"none" -> 0|1|2|3|4|0
//!   buttons (optional in CDP)              -> computed from type+button when absent
//!   Input.dispatchMouseEvent(mouseWheel)   -> Page.dispatchWheelEvent (separate method)
//!   Input.insertText                       -> Page.insertText (verbatim)

const std = @import("std");
const Allocator = std.mem.Allocator;

pub const method_dispatch_key_event = "Page.dispatchKeyEvent";
pub const method_dispatch_mouse_event = "Page.dispatchMouseEvent";
pub const method_dispatch_wheel_event = "Page.dispatchWheelEvent";
pub const method_insert_text = "Page.insertText";

/// CDP Input.dispatchKeyEvent type -> Juggler Page.dispatchKeyEvent type.
/// Returns null for unknown spellings (callers error out; never invented).
pub fn keyTypeJuggler(cdp_type: []const u8) ?[]const u8 {
    if (std.mem.eql(u8, cdp_type, "keyDown")) return "keydown";
    if (std.mem.eql(u8, cdp_type, "keyUp")) return "keyup";
    if (std.mem.eql(u8, cdp_type, "rawKeyDown")) return "rawkeydown";
    if (std.mem.eql(u8, cdp_type, "char")) return "char";
    return null;
}

/// CDP Input.dispatchMouseEvent type -> Juggler Page.dispatchMouseEvent
/// type. "mouseWheel" is NOT mapped here (it becomes Page.dispatchWheelEvent).
pub fn mouseTypeJuggler(cdp_type: []const u8) ?[]const u8 {
    if (std.mem.eql(u8, cdp_type, "mousePressed")) return "mousedown";
    if (std.mem.eql(u8, cdp_type, "mouseReleased")) return "mouseup";
    if (std.mem.eql(u8, cdp_type, "mouseMoved")) return "mousemove";
    return null;
}

/// CDP button string -> Juggler button number (Firefox DOM event.button:
/// left=0, middle=1, right=2, back=3, forward=4, none=0). Unknown -> null.
pub fn buttonNumber(cdp_button: []const u8) ?u16 {
    if (std.mem.eql(u8, cdp_button, "left")) return 0;
    if (std.mem.eql(u8, cdp_button, "middle")) return 1;
    if (std.mem.eql(u8, cdp_button, "right")) return 2;
    if (std.mem.eql(u8, cdp_button, "back")) return 3;
    if (std.mem.eql(u8, cdp_button, "forward")) return 4;
    if (std.mem.eql(u8, cdp_button, "none")) return 0;
    return null;
}

/// Firefox buttons bitmask (left=1, right=2, middle=4, back=8, forward=16).
fn buttonsForButton(cdp_button: []const u8) ?u16 {
    if (std.mem.eql(u8, cdp_button, "left")) return 1;
    if (std.mem.eql(u8, cdp_button, "right")) return 2;
    if (std.mem.eql(u8, cdp_button, "middle")) return 4;
    if (std.mem.eql(u8, cdp_button, "back")) return 8;
    if (std.mem.eql(u8, cdp_button, "forward")) return 16;
    return null;
}

/// Juggler requires `buttons` on every dispatchMouseEvent. CDP makes it
/// optional, so when absent we derive it: mousedown presses the button,
/// mouseup/mousemove releases (0). `juggler_type` is the mapped type.
pub fn buttonsFor(cdp_button: []const u8, juggler_type: []const u8, buttons_cdp: ?u16) ?u16 {
    if (buttons_cdp) |b| return b;
    if (std.mem.eql(u8, juggler_type, "mousedown")) return buttonsForButton(cdp_button);
    return 0;
}

/// Page.dispatchKeyEvent params; `text` omitted when null.
pub fn dispatchKeyEventParams(
    allocator: Allocator,
    type_juggler: []const u8,
    key: []const u8,
    key_code: u32,
    location: u32,
    code: []const u8,
    repeat: bool,
    text: ?[]const u8,
) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{
            .type = type_juggler,
            .key = key,
            .keyCode = key_code,
            .location = location,
            .code = code,
            .repeat = repeat,
            .text = text,
        },
        .{ .emit_null_optional_fields = false },
    );
}

/// Page.dispatchMouseEvent params; `clickCount` omitted when null.
pub fn dispatchMouseEventParams(
    allocator: Allocator,
    type_juggler: []const u8,
    button: u16,
    x: f64,
    y: f64,
    modifiers: u16,
    click_count: ?u32,
    buttons: u16,
) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{
            .type = type_juggler,
            .button = button,
            .x = x,
            .y = y,
            .modifiers = modifiers,
            .clickCount = click_count,
            .buttons = buttons,
        },
        .{ .emit_null_optional_fields = false },
    );
}

/// Page.dispatchWheelEvent params (deltaZ fixed to 0 — schema-required).
pub fn dispatchWheelEventParams(
    allocator: Allocator,
    x: f64,
    y: f64,
    delta_x: f64,
    delta_y: f64,
    modifiers: u16,
) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{
            .x = x,
            .y = y,
            .deltaX = delta_x,
            .deltaY = delta_y,
            .deltaZ = 0,
            .modifiers = modifiers,
        },
        .{ .emit_null_optional_fields = false },
    );
}

/// Page.insertText params.
pub fn insertTextParams(allocator: Allocator, text: []const u8) Allocator.Error![]u8 {
    return std.json.Stringify.valueAlloc(
        allocator,
        .{ .text = text },
        .{ .emit_null_optional_fields = false },
    );
}

const testing = std.testing;

test "input: method names match schema" {
    try testing.expectEqualStrings("Page.dispatchKeyEvent", method_dispatch_key_event);
    try testing.expectEqualStrings("Page.dispatchMouseEvent", method_dispatch_mouse_event);
    try testing.expectEqualStrings("Page.dispatchWheelEvent", method_dispatch_wheel_event);
    try testing.expectEqualStrings("Page.insertText", method_insert_text);
}

test "input: key type mapping CDP -> Juggler" {
    try testing.expectEqualStrings("keydown", keyTypeJuggler("keyDown").?);
    try testing.expectEqualStrings("keyup", keyTypeJuggler("keyUp").?);
    try testing.expectEqualStrings("rawkeydown", keyTypeJuggler("rawKeyDown").?);
    try testing.expectEqualStrings("char", keyTypeJuggler("char").?);
    try testing.expect(keyTypeJuggler("keyPress") == null); // not a CDP type
    try testing.expect(keyTypeJuggler("bogus") == null);
}

test "input: mouse type mapping CDP -> Juggler" {
    try testing.expectEqualStrings("mousedown", mouseTypeJuggler("mousePressed").?);
    try testing.expectEqualStrings("mouseup", mouseTypeJuggler("mouseReleased").?);
    try testing.expectEqualStrings("mousemove", mouseTypeJuggler("mouseMoved").?);
    try testing.expect(mouseTypeJuggler("mouseWheel") == null); // -> dispatchWheelEvent
}

test "input: button string -> number (Firefox event.button)" {
    try testing.expectEqual(@as(u16, 0), buttonNumber("left").?);
    try testing.expectEqual(@as(u16, 1), buttonNumber("middle").?);
    try testing.expectEqual(@as(u16, 2), buttonNumber("right").?);
    try testing.expectEqual(@as(u16, 3), buttonNumber("back").?);
    try testing.expectEqual(@as(u16, 4), buttonNumber("forward").?);
    try testing.expectEqual(@as(u16, 0), buttonNumber("none").?);
    try testing.expect(buttonNumber("bogus") == null);
}

test "input: buttons bitmask derivation (Juggler requires buttons)" {
    // mousedown presses the button.
    try testing.expectEqual(@as(u16, 1), buttonsFor("left", "mousedown", null).?);
    try testing.expectEqual(@as(u16, 2), buttonsFor("right", "mousedown", null).?);
    try testing.expectEqual(@as(u16, 4), buttonsFor("middle", "mousedown", null).?);
    try testing.expectEqual(@as(u16, 8), buttonsFor("back", "mousedown", null).?);
    try testing.expectEqual(@as(u16, 16), buttonsFor("forward", "mousedown", null).?);
    // mouseup/mousemove release.
    try testing.expectEqual(@as(u16, 0), buttonsFor("left", "mouseup", null).?);
    try testing.expectEqual(@as(u16, 0), buttonsFor("left", "mousemove", null).?);
    // explicit CDP buttons wins.
    try testing.expectEqual(@as(u16, 3), buttonsFor("left", "mousedown", 3).?);
    // unknown button with derivation needed -> null.
    try testing.expect(buttonsFor("bogus", "mousedown", null) == null);
}

test "input: dispatchKeyEvent params full (schema names, mapped type)" {
    const p = try dispatchKeyEventParams(testing.allocator, "keydown", "a", 65, 0, "KeyA", false, "a");
    defer testing.allocator.free(p);
    try testing.expectEqualStrings(
        "{\"type\":\"keydown\",\"key\":\"a\",\"keyCode\":65,\"location\":0,\"code\":\"KeyA\",\"repeat\":false,\"text\":\"a\"}",
        p,
    );
}

test "input: dispatchKeyEvent omits text when null" {
    const p = try dispatchKeyEventParams(testing.allocator, "keyup", "Enter", 13, 0, "Enter", false, null);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings(
        "{\"type\":\"keyup\",\"key\":\"Enter\",\"keyCode\":13,\"location\":0,\"code\":\"Enter\",\"repeat\":false}",
        p,
    );
}

test "input: dispatchMouseEvent params full" {
    const p = try dispatchMouseEventParams(testing.allocator, "mousedown", 0, 100, 20, 8, 1, 1);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings(
        "{\"type\":\"mousedown\",\"button\":0,\"x\":100,\"y\":20,\"modifiers\":8,\"clickCount\":1,\"buttons\":1}",
        p,
    );
}

test "input: dispatchMouseEvent omits clickCount when null" {
    const p = try dispatchMouseEventParams(testing.allocator, "mousemove", 0, 5, 5, 0, null, 0);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings("{\"type\":\"mousemove\",\"button\":0,\"x\":5,\"y\":5,\"modifiers\":0,\"buttons\":0}", p);
}

test "input: dispatchWheelEvent params (deltaZ fixed 0)" {
    const p = try dispatchWheelEventParams(testing.allocator, 10, 20, 0, 120, 0);
    defer testing.allocator.free(p);
    try testing.expectEqualStrings(
        "{\"x\":10,\"y\":20,\"deltaX\":0,\"deltaY\":120,\"deltaZ\":0,\"modifiers\":0}",
        p,
    );
}

test "input: insertText params" {
    const p = try insertTextParams(testing.allocator, "hello");
    defer testing.allocator.free(p);
    try testing.expectEqualStrings("{\"text\":\"hello\"}", p);
}
