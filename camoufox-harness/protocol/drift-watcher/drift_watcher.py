#!/usr/bin/env python3
"""Juggler protocol drift watcher — CI, build-time only, never ships.

Compares the vendored Juggler Protocol.js (camoufox fork pin) against the
upstream Juggler copy and reports schema drift (MASTER-PLAN §2.1.6, Faz 0).

    vendored  : camoufox-harness/vendor/Protocol.js  (daijro/camoufox pin)
    upstream  : microsoft/playwright
                browser_patches/firefox/juggler/protocol/Protocol.js

Exit codes:
    0  no drift (schemas identical)  —  or SKIP: upstream unreachable
       (offline CI), a skip note is printed
    1  drift detected (diff printed to stdout)
    2  usage error (e.g. local --source file missing)

Usage:
    drift_watcher.py                          # compare against upstream URL
    drift_watcher.py --source /path/x.js      # compare against a local file
    drift_watcher.py --url https://...        # override upstream URL
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Make `protocol.extractor` importable when run as a standalone script
# (directory name drift-watcher cannot be a package).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from protocol.extractor.extractor import PROTOCOL_SOURCE, ProtocolParseError, extract_schema

UPSTREAM_URL = "https://raw.githubusercontent.com/microsoft/playwright/main/browser_patches/firefox/juggler/protocol/Protocol.js"
UPSTREAM_LABEL = "microsoft/playwright (main)"

VENDOR_PROTOCOL_PATH = Path(__file__).resolve().parent.parent.parent / "vendor" / "Protocol.js"

_DIFF_LINES_LIMIT = 80


def _fetch(url: str, timeout: float) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _schema_of(path: Path, label: str) -> dict[str, Any]:
    try:
        schema = extract_schema(path.read_text(encoding="utf-8"), label)
    except ProtocolParseError as exc:
        raise ProtocolParseError(f"{path}: {exc}") from exc
    # Comparison is about protocol content, not provenance: pin labels differ
    # between the two files by construction, so neutralize them.
    schema["source"] = PROTOCOL_SOURCE
    return schema


def _diff_report(vendored: dict[str, Any], upstream: dict[str, Any]) -> list[str]:
    a = json.dumps(vendored, indent=2, ensure_ascii=False).splitlines()
    b = json.dumps(upstream, indent=2, ensure_ascii=False).splitlines()
    return list(difflib.unified_diff(a, b, fromfile=f"vendored ({PROTOCOL_SOURCE})",
                                     tofile=f"upstream ({UPSTREAM_LABEL})", lineterm=""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="drift_watcher.py",
        description="Compare the vendored Juggler protocol against upstream (CI tool).",
    )
    parser.add_argument("--vendor", type=Path, default=VENDOR_PROTOCOL_PATH,
                        help="vendored Protocol.js (default: vendor/Protocol.js)")
    parser.add_argument("--source", type=Path, default=None,
                        help="compare against a local Protocol.js file instead of the URL")
    parser.add_argument("--url", default=UPSTREAM_URL,
                        help=f"upstream Protocol.js URL (default: {UPSTREAM_URL})")
    parser.add_argument("--timeout", type=float, default=15.0, help="fetch timeout in seconds")
    args = parser.parse_args(argv)

    if not args.vendor.is_file():
        print(f"[usage] vendored Protocol.js not found: {args.vendor}", file=sys.stderr)
        return 2

    vendored = _schema_of(args.vendor, PROTOCOL_SOURCE)

    if args.source is not None:
        if not args.source.is_file():
            print(f"[usage] --source file not found: {args.source}", file=sys.stderr)
            return 2
        label = f"local {args.source}"
        try:
            upstream = _schema_of(args.source, label)
        except ProtocolParseError as exc:
            print(f"[drift] upstream file failed to extract ({exc}); treat as drift", file=sys.stderr)
            return 1
    else:
        try:
            upstream = extract_schema(_fetch(args.url, args.timeout), UPSTREAM_LABEL)
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError) as exc:
            print(f"[skip] upstream unreachable ({args.url}): {exc}", file=sys.stderr)
            return 0
        except ProtocolParseError as exc:
            print(f"[drift] upstream failed to extract ({exc}); treat as drift", file=sys.stderr)
            return 1
        upstream["source"] = PROTOCOL_SOURCE  # neutralize provenance, see _schema_of

    diff = _diff_report(vendored, upstream)
    if not diff:
        print(f"[no-drift] vendored Juggler schema matches {args.source or UPSTREAM_LABEL}")
        return 0

    print(f"[drift] {len(diff)} differing lines between vendored Juggler protocol "
          f"({PROTOCOL_SOURCE}) and {args.source or UPSTREAM_LABEL}")
    for line in diff[:_DIFF_LINES_LIMIT]:
        print(line)
    if len(diff) > _DIFF_LINES_LIMIT:
        print(f"... ({len(diff) - _DIFF_LINES_LIMIT} more lines)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
