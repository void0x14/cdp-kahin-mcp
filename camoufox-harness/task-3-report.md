# Task 3 — Camoufox-harness Zig core (Faz 3) — Report

Phase: Target/Console/Emulation adapters + navigate commit gate + lifecycle
hardening. Build root: `camoufox-harness/core/` (pinned toolchain
`~/.local/opt/zig-x86_64-linux-0.16.0/zig`, 0.16.0).

## What Faz 3 shipped (commit 9f405e0)

- `adapters/target.zig`, `adapters/console.zig`, `adapters/emulation.zig`,
  `adapters/network.zig`, `adapters/runtime.zig` — schema-faithful adapters
  (enable\* documented as no-ops, never sent on the wire).
- `driver.navigate` commit gate: `navigationSatisfied` — return gated on the
  started event for OUR navigation id + committed-current (or committed URL
  match), so a stale `eventFired` from the previous document cannot complete
  the lifecycle early.
- Lifecycle state machine (`adapters/page.zig`): idle -> waiting -> done |
  aborted, driven by eventFired/navigationStarted/navigationCommitted/
  navigationAborted, keyed on frame id + navigation id.
- Smoke driver `src/main.zig`: full end-to-end flow against the real
  Camoufox binary (152.0.4-beta.28).

## Review fixes (applied on top of 9f405e0)

Commit: `fix(camoufox-harness): navigationAborted id hardening, ownership fixes (Faz 3 review)`

1. **navigationAborted id hardening** (`driver.zig` onNavigationAborted,
   `page.zig` Lifecycle.onAbort): the abort path had none of the stale-event
   hardening applied to started/committed. `Page.navigationAborted` carries
   `navigationId` (required per schema, protocol/schema/juggler-schema.json)
   but the handler dropped it. Now threaded through and `onAbort` ignores
   non-matching ids exactly like `onCommitted`: aborts for a SUPERSEDED
   navigation (different id than the current one) or arriving before the
   response id is known (previous document) no longer flip the lifecycle to
   aborted — `navigate()` no longer returns `error.NavigationAborted` for a
   navigation that proceeds fine. Matching id still flips to aborted.
2. **`replaceNavigationId` ownership** (`page.zig`): old `navigation_id`
   was freed before duping; on dupe failure the freed pointer stayed
   assigned (the "OOM keeps the previous id" comment was false — it
   dangled). Now dupes to a temp, frees the old allocation after; OOM keeps
   the previous (still valid) id.
3. **Screenshot clip comment** (`driver.zig` screenshot doc): the claim
   "dispatcher rejects undefined clip even though the schema marks it
   optional" was wrong — the schema marks `clip` REQUIRED (no `optional`
   flag). Code always sends clip; behavior was correct, comment fixed.
4. **Docstring** (`page.zig` header): `sameDocumentNavigation` listed as
   `(frameId, navigationId, url)` — schema has only `(frameId, url)`.
   Fixed.

### Test evidence

`~/.local/opt/zig-x86_64-linux-0.16.0/zig build test --summary all`:

```
Build Summary: 3/3 steps succeeded; 119/119 tests passed
test success
+- run test 119 pass (119 total) 76ms MaxRSS:6M
   +- compile test Debug native cached 9ms MaxRSS:41M
```

119/119 (was 116). New tests:
- `lifecycle: abort with a stale navigation id is ignored` — stale id does
  NOT flip state; matching id does.
- `lifecycle: abort before the response id is known is ignored` — previous-
  document abort is a no-op.
- `driver: navigationAborted for a superseded navigation is ignored` —
  end-to-end event-handler level: waiting state + empty abort_text kept.

### Real smoke (Camoufox 152.0.4-beta.28)

`zig build run -- ~/.cache/camoufox/browsers/official/152.0.4-beta.28/camoufox`
— navigate + evaluate + screenshot + frame tree + viewport + clean exit.
Raw tail (full verbose log was streamed to stdout; key lines):

```
Browser.enable OK
setDefaultViewport OK
createBrowserContext -> browserContextId=6147bcd5-f914-4628-acb4-54bffb667c48
newPage -> targetId=fc63cd60-c211-448e-82d8-776bee317088
navigate -> navigationId=nav-23
evaluate("1+1") -> 2
SMOKE PASS: evaluate("1+1") == 2
console messages -> [{"level":"warn",...Quirks Mode...},{"level":"log","text":"smoke-msg-42",...}]
screenshot -> PNG 8143 bytes
screenshot(full_page) -> PNG 1481 bytes
getFrameTree -> {"frameTree":{"frame":{"id":"mainframe-11","url":"data:text/html,<h1>hi</h1>"},"childFrames":[]}}
setViewportSize OK
closeTarget -> {}
browser exited with code 0
EXIT: 0
```

Note: the smoke run itself exercised the fix — the `Page.navigationAborted`
handler now parses `navigationId` and the navigate gate saw real
`navigationStarted`/`navigationCommitted` events with matching ids
(nav-23) while stale events (nav-22 about:blank) were ignored.
