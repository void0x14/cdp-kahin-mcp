//! Juggler Network adapter (Faz 3): passive event passthrough.
//!
//! Schema facts (Network domain events):
//!   requestWillBeSent(frameId?, requestId, redirectedFrom?, postData?,
//!                     headers: Network.HTTPHeader[], isIntercepted, url,
//!                     method, navigationId?, cause, internalCause)
//!   responseReceived(securityDetails?, requestId, fromCache, remoteIPAddress?,
//!                    remotePort?, status, statusText, headers,
//!                    timing: Network.ResourceTiming, fromServiceWorker)
//!   requestFinished(requestId, responseEndTime, transferSize,
//!                   encodedBodySize, protocolVersion?)
//!   requestFailed(requestId, errorCode)
//!
//! The Juggler event names already match their CDP twins verbatim
//! (requestWillBeSent / responseReceived / requestFinished / requestFailed),
//! so Kahin's `Network.*`-prefix collector (oracle.py `_on_network_event`)
//! works unchanged: pure passthrough. No rename, no re-shape.
//!
//! `Network.enable` has no wire equivalent — Juggler emits these events
//! unconditionally (driver.enableNetwork is a documented no-op).
//! INTERCEPTION IS OUT OF SCOPE (Faz 4): setRequestInterception etc. are
//! listed here only to prove they were considered and rejected.

const std = @import("std");

pub const event_request_will_be_sent = "Network.requestWillBeSent";
pub const event_response_received = "Network.responseReceived";
pub const event_request_finished = "Network.requestFinished";
pub const event_request_failed = "Network.requestFailed";

/// Methods that exist in the schema but are deliberately NOT wired (Faz 4).
pub const method_set_request_interception = "Network.setRequestInterception";
pub const method_abort_intercepted_request = "Network.abortInterceptedRequest";
pub const method_resume_intercepted_request = "Network.resumeInterceptedRequest";
pub const method_fulfill_intercepted_request = "Network.fulfillInterceptedRequest";

const testing = std.testing;

test "network: event names match schema (passthrough invariant)" {
    try testing.expectEqualStrings("Network.requestWillBeSent", event_request_will_be_sent);
    try testing.expectEqualStrings("Network.responseReceived", event_response_received);
    try testing.expectEqualStrings("Network.requestFinished", event_request_finished);
    try testing.expectEqualStrings("Network.requestFailed", event_request_failed);
}

test "network: passthrough events keep the Network. prefix (Kahin collector contract)" {
    try testing.expect(std.mem.startsWith(u8, event_request_will_be_sent, "Network."));
    try testing.expect(std.mem.startsWith(u8, event_response_received, "Network."));
    try testing.expect(std.mem.startsWith(u8, event_request_finished, "Network."));
    try testing.expect(std.mem.startsWith(u8, event_request_failed, "Network."));
}
