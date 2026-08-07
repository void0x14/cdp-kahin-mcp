#!/usr/bin/env python3
"""Stealth drift-watcher regression (Faz 3 Task 6).

Runs the real self-audit (kahin_stealth_audit) against a fixed probe page
through the exact tool functions the MCP server exposes, then diffs the
per-check pass/fail results against the pinned baseline
(camoufox-harness/tests/perf/stealth-baseline.json).

Exit codes:
  0 — no new leaks and the audit ratio is at/above the gate (0.8)
  1 — a new leak appeared or the ratio dropped below the gate
  2 — environment/setup problem (KAHIN_REQUIRE_STEALTH unset, baseline
      missing/unreadable, or the audit could not run against the engine)

The baseline is never rewritten by a normal run: a missing baseline is a
setup error (exit 2), not a silent self-pinning pass. Create or regenerate
the baseline explicitly with KAHIN_UPDATE_BASELINE=1; even then a sub-gate
ratio refuses to be pinned. No proxy is involved anywhere in this script,
so proxy credentials can never be logged by it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASELINE = ROOT / "camoufox-harness" / "tests" / "perf" / "stealth-baseline.json"
RATIO_GATE = 0.8
PROBE_PAGE = "about:blank"


async def _run_audit() -> dict:
    """Boot the real Mirage engine and audit a fixed probe page through the
    same tool functions the agent sees."""
    from kahin.tools import pilot, stealth_mirage, trainman_mirage  # noqa: PLC0415

    started = json.loads(await pilot.browser_start(engine="mirage"))
    if started.get("status") not in ("started", "reused"):
        raise RuntimeError(f"browser_start failed: {started}")
    try:
        tab = json.loads(await trainman_mirage.mirage_tab_new())
        if not tab.get("targetId"):
            raise RuntimeError(f"tab_new failed: {tab}")
        await asyncio.sleep(0.5)
        await pilot.navigate(url=PROBE_PAGE)
        await asyncio.sleep(0.5)
        result = json.loads(await stealth_mirage.stealth_audit())
        if not result.get("audited") or not isinstance(result.get("checks"), list):
            raise RuntimeError(f"stealth audit failed: {result}")
        return result
    finally:
        await pilot.browser_stop()


def main() -> int:
    if os.environ.get("KAHIN_REQUIRE_STEALTH") != "1":
        print(
            "error: KAHIN_REQUIRE_STEALTH=1 is required to run the stealth regression",
            file=sys.stderr,
        )
        return 2
    update = os.environ.get("KAHIN_UPDATE_BASELINE") == "1"
    if not BASELINE.exists() and not update:
        print(
            f"error: baseline missing: {BASELINE} — generate it with "
            "KAHIN_UPDATE_BASELINE=1 against a known-good engine, then commit it",
            file=sys.stderr,
        )
        return 2
    try:
        result = asyncio.run(_run_audit())
    except Exception as exc:  # noqa: BLE001 - environment outcome
        print(f"error: audit run failed (environment): {exc}", file=sys.stderr)
        return 2

    current = {c["check"]: bool(c["passed"]) for c in result["checks"]}
    if update:
        # Pin mode: the baseline is (re)created from this audit below; a
        # missing or stale file is not an error here.
        baseline: dict[str, bool] = {}
    else:
        try:
            baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"error: baseline unreadable: {exc}", file=sys.stderr)
            return 2
        if not isinstance(baseline, dict):
            print("error: baseline is not a JSON object", file=sys.stderr)
            return 2

    score = result.get("score") or {}
    ratio = float(score.get("ratio", 0.0))
    failures = sorted(name for name, passed in current.items() if not passed)
    # A check is a NEW leak when it passed in the baseline and fails now;
    # a check absent from the baseline defaults to "was passing" so a new
    # failing probe surfaces as drift until the baseline is re-pinned.
    new_failures = sorted(name for name in failures if baseline.get(name, True) is True)
    print(
        json.dumps(
            {
                "ratio": ratio,
                "passed": score.get("passed"),
                "total": score.get("total"),
                "failures": failures,
                "new_failures": new_failures,
                "baseline": str(BASELINE.relative_to(ROOT)),
            },
            indent=2,
        )
    )

    if update:
        if ratio < RATIO_GATE:
            print(
                f"refusing to pin baseline: audit ratio {ratio} below gate {RATIO_GATE}",
                file=sys.stderr,
            )
            return 1
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"baseline updated: {BASELINE}")
        return 0

    if new_failures:
        print(f"NEW STEALTH LEAKS: {new_failures}", file=sys.stderr)
        return 1
    if ratio < RATIO_GATE:
        print(f"stealth audit ratio {ratio} below gate {RATIO_GATE}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
