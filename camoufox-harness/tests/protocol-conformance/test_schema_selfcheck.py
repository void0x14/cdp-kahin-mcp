"""Schema conformance self-checks (MASTER-PLAN Faz 0).

Runs the Juggler protocol extractor against the vendored Protocol.js and
asserts that the committed schema, the pin lock and the drift watcher are
faithful to the source. Method/event names asserted here are taken from the
actual vendored file — nothing is invented (see test_no_invented_methods).
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from protocol.extractor.extractor import (
    PROTOCOL_SOURCE,
    ProtocolParseError,
    extract_schema,
)
from protocol.extractor.extractor import (
    VENDOR_PROTOCOL_PATH as VENDOR,
)

HARNESS = Path(__file__).resolve().parent.parent.parent
SCHEMA_PATH = HARNESS / "protocol" / "schema" / "juggler-schema.json"
PIN_LOCK_PATH = HARNESS / "vendor" / "camoufox-fork-pin.lock"
DRIFT_WATCHER = HARNESS / "protocol" / "drift-watcher" / "drift_watcher.py"

# Critical methods that must be present, straight from vendor/Protocol.js.
CRITICAL_METHODS = [
    ("Browser", "enable"),
    ("Browser", "createBrowserContext"),
    ("Browser", "removeBrowserContext"),
    ("Browser", "newPage"),
    ("Browser", "close"),
    ("Browser", "setExtraHTTPHeaders"),
    ("Browser", "setRequestInterception"),
    ("Page", "navigate"),
    ("Page", "screenshot"),
    ("Page", "goBack"),
    ("Page", "goForward"),
    ("Page", "reload"),
    ("Runtime", "evaluate"),
    ("Runtime", "callFunction"),
    ("Network", "setRequestInterception"),
    ("Heap", "collectGarbage"),
    ("Accessibility", "getFullAXTree"),
]

# CDP-shaped method names the brief listed as candidates; Juggler's real
# Protocol.js has no such methods (frame tree = Page events, lifecycle =
# Page.eventFired). The schema must NOT invent them.
INVENTED_METHODS = [("Page", "getFrameTree"), ("Page", "setLifecycleEventsEnabled")]


@pytest.fixture(scope="module")
def schema() -> dict:
    """Fresh extraction from the vendored Protocol.js."""
    try:
        return extract_schema(VENDOR.read_text(encoding="utf-8"), PROTOCOL_SOURCE)
    except ProtocolParseError as exc:
        pytest.fail(f"extractor failed on vendored Protocol.js: {exc}")


@pytest.fixture(scope="module")
def committed_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- extraction

def test_committed_schema_matches_fresh_extraction(schema: dict, committed_schema: dict) -> None:
    assert schema == committed_schema, (
        "committed protocol/schema/juggler-schema.json is stale — re-run the extractor"
    )


def test_extraction_is_deterministic(schema: dict) -> None:
    assert extract_schema(VENDOR.read_text(encoding="utf-8"), PROTOCOL_SOURCE) == schema


def test_metadata(schema: dict) -> None:
    assert schema["schema_version"] == 1
    assert schema["source"] == PROTOCOL_SOURCE
    assert list(schema["domains"]) == ["Browser", "Heap", "Network", "Runtime", "Page", "Accessibility"]


@pytest.mark.parametrize("domain,method", CRITICAL_METHODS)
def test_critical_methods_present(schema: dict, domain: str, method: str) -> None:
    assert method in schema["domains"][domain]["methods"], f"{domain}.{method} missing from schema"


@pytest.mark.parametrize("domain,method", INVENTED_METHODS)
def test_no_invented_methods(schema: dict, domain: str, method: str) -> None:
    assert method not in schema["domains"][domain]["methods"], (
        f"{domain}.{method} does not exist in Juggler Protocol.js — must not be invented"
    )


# ---------------------------------------------------------------- param fidelity

def test_browser_create_browser_context(schema: dict) -> None:
    m = schema["domains"]["Browser"]["methods"]["createBrowserContext"]
    assert m["params"] == [{"name": "removeOnDetach", "type": "boolean", "optional": True}]
    assert m["returns"] == [{"name": "browserContextId", "type": "string"}]


def test_page_navigate(schema: dict) -> None:
    m = schema["domains"]["Page"]["methods"]["navigate"]
    assert [p["name"] for p in m["params"]] == ["frameId", "url", "referer"]
    assert m["params"][1] == {"name": "url", "type": "string"}
    assert m["params"][2] == {"name": "referer", "type": "string", "optional": True}
    assert m["returns"] == [{"name": "navigationId", "type": "string", "nullable": True}]


def test_runtime_evaluate(schema: dict) -> None:
    m = schema["domains"]["Runtime"]["methods"]["evaluate"]
    assert [p["name"] for p in m["params"]] == ["executionContextId", "expression", "returnByValue"]
    assert [r["name"] for r in m["returns"]] == ["result", "exceptionDetails"]


def test_enum_values(schema: dict) -> None:
    mime = schema["domains"]["Page"]["methods"]["screenshot"]["params"][0]
    assert mime == {"name": "mimeType", "type": "enum", "enum": ["image/png", "image/jpeg"]}
    color_scheme = schema["domains"]["Browser"]["methods"]["setColorScheme"]["params"][1]
    assert color_scheme == {"name": "colorScheme", "type": "enum",
                            "enum": ["dark", "light", "no-preference"], "nullable": True}


def test_enum_with_non_string_value(schema: dict) -> None:
    checked = next(p for p in schema["domains"]["Accessibility"]["types"]["AXTree"]["properties"]
                   if p["name"] == "checked")
    assert checked["type"] == "enum" and checked["enum"] == ["mixed", True]


def test_nullable_and_optional_flags(schema: dict) -> None:
    creds = schema["domains"]["Browser"]["methods"]["setHTTPCredentials"]["params"][1]
    assert creds == {"name": "credentials", "type": "object",
                     "ref": "Network.HTTPCredentials", "nullable": True}


def test_cross_domain_type_refs(schema: dict) -> None:
    # Browser.setDefaultViewport references Page.Viewport (pageTypes pool).
    viewport = schema["domains"]["Browser"]["methods"]["setDefaultViewport"]["params"][1]
    assert viewport["type"] == "object" and viewport["ref"] == "Page.Viewport"
    # Page events reference Runtime types.
    loc = next(p for p in schema["domains"]["Page"]["events"]["uncaughtError"]
               if p["name"] == "location")
    assert loc["ref"] == "Runtime.ScriptLocation"


def test_recursive_type_ref(schema: dict) -> None:
    children = next(p for p in schema["domains"]["Accessibility"]["types"]["AXTree"]["properties"]
                    if p["name"] == "children")
    assert children == {"name": "children", "type": "array", "optional": True,
                        "items": {"type": "object", "ref": "Accessibility.AXTree"}}


def test_anonymous_inline_object(schema: dict) -> None:
    options = schema["domains"]["Browser"]["methods"]["setVideoRecordingOptions"]["params"][1]
    assert options["type"] == "object" and options["optional"] is True
    assert [p["name"] for p in options["properties"]] == ["dir", "width", "height"]


def test_domain_targets(schema: dict) -> None:
    assert schema["domains"]["Browser"]["targets"] == ["browser"]
    assert schema["domains"]["Page"]["targets"] == ["page"]


# ---------------------------------------------------------------- pin lock

def _read_lock() -> dict[str, str]:
    lock: dict[str, str] = {}
    for line in PIN_LOCK_PATH.read_text(encoding="utf-8").splitlines():
        if ": " in line:
            key, _, value = line.partition(": ")
            lock[key] = value.strip()
    return lock


def test_pin_lock_matches_vendor_sha256() -> None:
    lock = _read_lock()
    assert lock["repo"] == "daijro/camoufox"
    assert lock["ref"] == "v152.0.4-beta.28"
    assert lock["date"] == "2026-07-19"
    assert lock["source"] == "additions/juggler/protocol/Protocol.js"
    expected = hashlib.sha256(VENDOR.read_bytes()).hexdigest()
    assert lock["sha256"] == expected, "pin lock sha256 does not match vendor/Protocol.js"


# ---------------------------------------------------------------- drift watcher

def _run_watcher(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DRIFT_WATCHER), *args],
        capture_output=True, text=True, timeout=60, check=False,
    )


def test_drift_watcher_offline_skips_cleanly() -> None:
    # 127.0.0.1:1 refuses connections instantly on any machine with a loopback.
    proc = _run_watcher("--url", "http://127.0.0.1:1/unreachable")
    assert proc.returncode == 0, proc.stderr
    assert "skip" in (proc.stdout + proc.stderr).lower()


def test_drift_watcher_local_no_drift() -> None:
    proc = _run_watcher("--source", str(VENDOR))
    assert proc.returncode == 0, proc.stderr
    assert "no-drift" in proc.stdout


def test_drift_watcher_detects_drift(tmp_path: Path) -> None:
    drifted = tmp_path / "Protocol.js"
    drifted.write_text(VENDOR.read_text(encoding="utf-8").replace("'navigate': {", "'navigat': {"),
                       encoding="utf-8")
    proc = _run_watcher("--source", str(drifted))
    assert proc.returncode == 1
    assert "drift" in proc.stdout
    assert "navigat" in proc.stdout
