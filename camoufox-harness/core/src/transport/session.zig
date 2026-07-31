//! Request/response matching + session demux for the Juggler pipe protocol.
//!
//! Wire shapes (CDP-like):
//!   request:  {"id":N,"method":"...","params":{...}}
//!   response: {"id":N,"result":{...}} | {"id":N,"error":{...}}
//!   event:    {"method":"...","params":{...},"sessionId":"..."}   (sessionId optional = root session)

const std = @import("std");
const Allocator = std.mem.Allocator;

pub const Session = struct {
    /// "" is the root session; sub-sessions carry their own id.
    id: []const u8,
    target_type: []const u8,
    name: []const u8,
};

pub const Router = struct {
    allocator: Allocator,
    /// Arena for dispatch results: `method`/`sessionId` strings stay valid
    /// until `deinit` (they do NOT borrow the dispatched input).
    arena: std.heap.ArenaAllocator,
    /// In-flight requests: id -> caller context (resolved on the matching response).
    pending: std.AutoHashMap(u32, *anyopaque),
    /// Registered sessions keyed by session id ("" = root).
    sessions: std.StringHashMap(Session),
    /// Monotonic request id counter. Juggler treats 0 as falsy ("every
    /// message must have an 'id'"), so ids start at 1.
    next_id: std.atomic.Value(u32),

    pub fn init(allocator: Allocator) Router {
        return .{
            .allocator = allocator,
            .arena = std.heap.ArenaAllocator.init(allocator),
            .pending = std.AutoHashMap(u32, *anyopaque).init(allocator),
            .sessions = std.StringHashMap(Session).init(allocator),
            .next_id = std.atomic.Value(u32).init(1),
        };
    }

    pub fn deinit(self: *Router) void {
        var it = self.sessions.iterator();
        while (it.next()) |kv| self.allocator.free(kv.key_ptr.*);
        self.pending.deinit();
        self.sessions.deinit();
        self.arena.deinit();
    }

    /// Next request id (0-based, monotonic; wraps only after 2^32 requests).
    pub fn nextId(self: *Router) u32 {
        return self.next_id.fetchAdd(1, .monotonic);
    }

    /// Register an in-flight request. `context` is handed back on the matching response.
    pub fn registerPending(self: *Router, id: u32, context: *anyopaque) Allocator.Error!void {
        try self.pending.put(id, context);
    }

    /// Register a session; root session uses id "".
    pub fn registerSession(self: *Router, session: Session) Allocator.Error!void {
        const key = try self.allocator.dupe(u8, session.id);
        errdefer self.allocator.free(key);
        try self.sessions.put(key, session);
    }

    pub fn removeSession(self: *Router, id: []const u8) void {
        if (self.sessions.fetchRemove(id)) |kv| self.allocator.free(kv.key);
    }

    pub const Dispatch = union(enum) {
        /// A response matching a pending request (the pending entry is consumed).
        /// All slices borrow from the dispatched `json` input.
        response: struct {
            id: u32,
            context: *anyopaque,
            is_error: bool,
            raw: []const u8,
        },
        /// A server-initiated event. All slices borrow from the dispatched `json` input.
        event: struct {
            method: []const u8,
            /// null = root session.
            session_id: ?[]const u8,
            known_session: bool,
            raw: []const u8,
        },
        /// Message with neither a matching "id" nor a "method" (unknown id, malformed JSON).
        invalid: void,
    };

    /// Route one framed message. `json` must stay alive while the returned
    /// `raw` slice is used (it borrows from it). `method`/`sessionId` are
    /// arena-owned and stay valid until `deinit`.
    pub fn dispatch(self: *Router, json: []const u8) Allocator.Error!Dispatch {
        const parsed = std.json.parseFromSliceLeaky(std.json.Value, self.arena.allocator(), json, .{}) catch {
            return .invalid;
        };
        if (parsed != .object) return .invalid;

        // Response? id wins over method if both present (CDP semantics).
        if (parsed.object.get("id")) |idv| {
            if (idv != .integer) return .invalid;
            const id: u32 = std.math.cast(u32, idv.integer) orelse return .invalid;
            const ctx = self.pending.fetchRemove(id) orelse return .invalid; // stale/unknown id
            return .{ .response = .{
                .id = id,
                .context = ctx.value,
                .is_error = parsed.object.get("error") != null,
                .raw = json,
            } };
        }

        if (parsed.object.get("method")) |mv| {
            if (mv != .string) return .invalid;
            const sid = if (parsed.object.get("sessionId")) |sv| blk: {
                if (sv != .string) return .invalid;
                break :blk sv.string;
            } else "";
            return .{ .event = .{
                .method = mv.string,
                .session_id = if (sid.len == 0) null else sid,
                .known_session = self.sessions.contains(sid),
                .raw = json,
            } };
        }

        return .invalid;
    }
};

const testing = std.testing;

fn initRouter() Router {
    return Router.init(testing.allocator);
}

fn deinitRouter(r: *Router) void {
    r.deinit();
}

test "nextId starts at 1 and is monotonic" {
    var r = initRouter();
    defer deinitRouter(&r);
    try testing.expectEqual(@as(u32, 1), r.nextId());
    try testing.expectEqual(@as(u32, 2), r.nextId());
    try testing.expectEqual(@as(u32, 3), r.nextId());
}

test "response matches pending request by id" {
    var r = initRouter();
    defer deinitRouter(&r);
    var ctx: u32 = 42;
    try r.registerPending(7, @ptrCast(&ctx));

    const json = "{\"id\":7,\"result\":{}}";
    switch (try r.dispatch(json)) {
        .response => |resp| {
            try testing.expectEqual(@as(u32, 7), resp.id);
            try testing.expectEqual(@as(*anyopaque, @ptrCast(&ctx)), resp.context);
            try testing.expect(!resp.is_error);
            try testing.expectEqualSlices(u8, json, resp.raw);
        },
        else => return error.TestUnexpectedDispatch,
    }
}

test "response with error flag" {
    var r = initRouter();
    defer deinitRouter(&r);
    var ctx: u32 = 1;
    try r.registerPending(3, @ptrCast(&ctx));
    switch (try r.dispatch("{\"id\":3,\"error\":{\"code\":-32601,\"message\":\"x\"}}")) {
        .response => |resp| {
            try testing.expect(resp.is_error);
            try testing.expectEqual(@as(u32, 3), resp.id);
        },
        else => return error.TestUnexpectedDispatch,
    }
}

test "unknown id is dropped and pending entry consumed" {
    var r = initRouter();
    defer deinitRouter(&r);
    var ctx: u32 = 1;
    try r.registerPending(9, @ptrCast(&ctx));
    // Response for an id we never sent.
    try testing.expectEqual(Router.Dispatch.invalid, (try r.dispatch("{\"id\":99,\"result\":{}}")));
    // The registered pending id is still resolvable afterwards.
    switch (try r.dispatch("{\"id\":9,\"result\":{}}")) {
        .response => |resp| try testing.expectEqual(@as(u32, 9), resp.id),
        else => return error.TestUnexpectedDispatch,
    }
}

test "response consumed only once" {
    var r = initRouter();
    defer deinitRouter(&r);
    var ctx: u32 = 1;
    try r.registerPending(5, @ptrCast(&ctx));
    _ = try r.dispatch("{\"id\":5,\"result\":{}}");
    try testing.expectEqual(Router.Dispatch.invalid, (try r.dispatch("{\"id\":5,\"result\":{}}")));
}

test "event routes by sessionId" {
    var r = initRouter();
    defer deinitRouter(&r);
    try r.registerSession(.{ .id = "s1", .target_type = "page", .name = "page1" });

    const json = "{\"method\":\"Page.loadEventFired\",\"params\":{},\"sessionId\":\"s1\"}";
    switch (try r.dispatch(json)) {
        .event => |ev| {
            try testing.expectEqualSlices(u8, "Page.loadEventFired", ev.method);
            try testing.expectEqualSlices(u8, "s1", ev.session_id.?);
            try testing.expect(ev.known_session);
            try testing.expectEqualSlices(u8, json, ev.raw);
        },
        else => return error.TestUnexpectedDispatch,
    }
}

test "event without sessionId is root session" {
    var r = initRouter();
    defer deinitRouter(&r);
    try r.registerSession(.{ .id = "", .target_type = "browser", .name = "root" });
    switch (try r.dispatch("{\"method\":\"Target.browserContextCreated\",\"params\":{}}")) {
        .event => |ev| {
            try testing.expectEqualSlices(u8, "Target.browserContextCreated", ev.method);
            try testing.expect(ev.session_id == null);
            try testing.expect(ev.known_session); // root registered under ""
        },
        else => return error.TestUnexpectedDispatch,
    }
}

test "event on unknown session reports known_session=false" {
    var r = initRouter();
    defer deinitRouter(&r);
    switch (try r.dispatch("{\"method\":\"Page.navigate\",\"params\":{},\"sessionId\":\"ghost\"}")) {
        .event => |ev| {
            try testing.expectEqualSlices(u8, "ghost", ev.session_id.?);
            try testing.expect(!ev.known_session);
        },
        else => return error.TestUnexpectedDispatch,
    }
}

test "session register/remove" {
    var r = initRouter();
    defer deinitRouter(&r);
    try r.registerSession(.{ .id = "s2", .target_type = "page", .name = "p2" });
    r.removeSession("s2");
    switch (try r.dispatch("{\"method\":\"Page.navigate\",\"params\":{},\"sessionId\":\"s2\"}")) {
        .event => |ev| try testing.expect(!ev.known_session),
        else => return error.TestUnexpectedDispatch,
    }
}

test "root session registers under empty id" {
    var r = initRouter();
    defer deinitRouter(&r);
    try r.registerSession(.{ .id = "", .target_type = "browser", .name = "root" });
    switch (try r.dispatch("{\"method\":\"Browser.attached\",\"params\":{}}")) {
        .event => |ev| {
            try testing.expect(ev.session_id == null);
            try testing.expect(ev.known_session);
        },
        else => return error.TestUnexpectedDispatch,
    }
}

test "malformed json is invalid" {
    var r = initRouter();
    defer deinitRouter(&r);
    try testing.expectEqual(Router.Dispatch.invalid, (try r.dispatch("not json")));
    try testing.expectEqual(Router.Dispatch.invalid, (try r.dispatch("[1,2,3]")));
    try testing.expectEqual(Router.Dispatch.invalid, (try r.dispatch("{\"foo\":1}")));
    try testing.expectEqual(Router.Dispatch.invalid, (try r.dispatch("{\"id\":\"notanumber\"}")));
}
