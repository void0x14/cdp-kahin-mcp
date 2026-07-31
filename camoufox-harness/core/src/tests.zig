//! Unit test root: pulls in every module's inline tests.

test {
    _ = @import("transport/frame.zig");
    _ = @import("transport/session.zig");
    _ = @import("transport/pipe.zig");
}
