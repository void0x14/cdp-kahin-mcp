//! Unit test root: pulls in every module's inline tests.

test {
    _ = @import("src/transport/frame.zig");
    _ = @import("src/transport/session.zig");
    _ = @import("src/transport/pipe.zig");
    _ = @import("adapters/browser.zig");
    _ = @import("adapters/page.zig");
    _ = @import("adapters/runtime.zig");
    _ = @import("driver.zig");
}
