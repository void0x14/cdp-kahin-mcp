//! Juggler wire framing: JSON payload + `\x00` terminator.
//!
//! Pure functions over byte buffers; no I/O here so the layer is unit-testable
//! without fds or a browser.

const std = @import("std");
const Allocator = std.mem.Allocator;

pub const terminator: u8 = 0x00;

/// `msg` + trailing `\x00`. Owned result.
pub fn encode(allocator: Allocator, msg: []const u8) Allocator.Error![]u8 {
    const out = try allocator.alloc(u8, msg.len + 1);
    @memcpy(out[0..msg.len], msg);
    out[msg.len] = terminator;
    return out;
}

pub const Decoded = struct {
    /// Complete messages (terminator stripped), each an owned copy.
    messages: []const []u8,
    /// Bytes after the last terminator (a partial message), owned copy.
    /// Feed back into the next `decodeStream` call together with new data.
    rest: []u8,
};

/// Drain every `\x00`-terminated message out of `buf`. The tail after the
/// last terminator is returned as `rest` — this is the buffer-drain pattern
/// for streamed input arriving in arbitrary chunk sizes.
pub fn decodeStream(allocator: Allocator, buf: []const u8) Allocator.Error!Decoded {
    var messages: std.array_list.Aligned([]u8, null) = .empty;
    errdefer messages.deinit(allocator);

    var start: usize = 0;
    while (std.mem.findScalar(u8, buf[start..], terminator)) |rel| {
        const sep = start + rel;
        try messages.append(allocator, try allocator.dupe(u8, buf[start..sep]));
        start = sep + 1;
    }
    return .{
        .messages = try messages.toOwnedSlice(allocator),
        .rest = try allocator.dupe(u8, buf[start..]),
    };
}

const testing = std.testing;

fn freeDecoded(d: *Decoded) void {
    for (d.messages) |m| testing.allocator.free(m);
    testing.allocator.free(d.messages);
    testing.allocator.free(d.rest);
}

test "encode appends terminator" {
    const out = try encode(testing.allocator, "{}");
    defer testing.allocator.free(out);
    try testing.expectEqualSlices(u8, "{}\x00", out);
}

test "encode empty message" {
    const out = try encode(testing.allocator, "");
    defer testing.allocator.free(out);
    try testing.expectEqualSlices(u8, "\x00", out);
}

test "decodeStream splits and keeps rest" {
    const d = try decodeStream(testing.allocator, "a\x00b\x00c");
    defer {
        for (d.messages) |m| testing.allocator.free(m);
        testing.allocator.free(d.messages);
        testing.allocator.free(d.rest);
    }
    try testing.expectEqual(@as(usize, 2), d.messages.len);
    try testing.expectEqualSlices(u8, "a", d.messages[0]);
    try testing.expectEqualSlices(u8, "b", d.messages[1]);
    try testing.expectEqualSlices(u8, "c", d.rest);
}

test "decodeStream trailing terminator leaves empty rest" {
    const d = try decodeStream(testing.allocator, "a\x00b\x00");
    defer {
        for (d.messages) |m| testing.allocator.free(m);
        testing.allocator.free(d.messages);
        testing.allocator.free(d.rest);
    }
    try testing.expectEqual(@as(usize, 2), d.messages.len);
    try testing.expectEqual(@as(usize, 0), d.rest.len);
}

test "decodeStream empty input" {
    const d = try decodeStream(testing.allocator, "");
    defer {
        for (d.messages) |m| testing.allocator.free(m);
        testing.allocator.free(d.messages);
        testing.allocator.free(d.rest);
    }
    try testing.expectEqual(@as(usize, 0), d.messages.len);
    try testing.expectEqual(@as(usize, 0), d.rest.len);
}

test "decodeStream partial message is kept whole in rest" {
    const d = try decodeStream(testing.allocator, "{\"id\"");
    defer {
        for (d.messages) |m| testing.allocator.free(m);
        testing.allocator.free(d.messages);
        testing.allocator.free(d.rest);
    }
    try testing.expectEqual(@as(usize, 0), d.messages.len);
    try testing.expectEqualSlices(u8, "{\"id\"", d.rest);
}

test "drain pattern: chunks reassemble across calls" {
    const buf: []const u8 = "{\"id\":1\x00{\"id\":";
    var d = try decodeStream(testing.allocator, buf);
    defer freeDecoded(&d);
    try testing.expectEqual(@as(usize, 1), d.messages.len);
    try testing.expectEqualSlices(u8, "{\"id\":1", d.messages[0]);

    // rest + next chunk completes the second message; old d is consumed.
    const next = try std.mem.concat(testing.allocator, u8, &.{ d.rest, "2}\x00" });
    defer testing.allocator.free(next);
    freeDecoded(&d);
    d = try decodeStream(testing.allocator, next);
    try testing.expectEqual(@as(usize, 1), d.messages.len);
    try testing.expectEqualSlices(u8, "{\"id\":2}", d.messages[0]);
    try testing.expectEqual(@as(usize, 0), d.rest.len);
}

test "encode then decodeStream roundtrip" {
    const framed = try encode(testing.allocator, "{\"a\":1}");
    defer testing.allocator.free(framed);
    const d = try decodeStream(testing.allocator, framed);
    defer {
        for (d.messages) |m| testing.allocator.free(m);
        testing.allocator.free(d.messages);
        testing.allocator.free(d.rest);
    }
    try testing.expectEqual(@as(usize, 1), d.messages.len);
    try testing.expectEqualSlices(u8, "{\"a\":1}", d.messages[0]);
}
