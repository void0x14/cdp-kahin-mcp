"""Fetch CDP protocol schema from Chromium source and bundle as protocol.json.gz."""

import gzip
import json
import urllib.request
from pathlib import Path

BROWSER_PROTOCOL_URL = (
    "https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/"
    "master/json/browser_protocol.json"
)
JS_PROTOCOL_URL = (
    "https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/"
    "master/json/js_protocol.json"
)

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "kahin" / "the_source" / "protocol.json.gz"


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url) as resp:
        return json.loads(resp.read().decode())


def fetch_protocol() -> dict:
    browser = fetch_json(BROWSER_PROTOCOL_URL)
    js = fetch_json(JS_PROTOCOL_URL)
    merged = {"domains": browser.get("domains", []) + js.get("domains", [])}
    return merged


def main() -> None:
    print("Fetching CDP schema...")
    proto = fetch_protocol()
    print(f"Domains: {len(proto['domains'])}")

    data = json.dumps(proto, separators=(",", ":")).encode()
    compressed = gzip.compress(data, compresslevel=9)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(compressed)

    raw_kb = len(data) / 1024
    comp_kb = len(compressed) / 1024
    print(f"Raw: {raw_kb:.0f}KB -> Compressed: {comp_kb:.0f}KB ({comp_kb/raw_kb*100:.0f}%)")
    print(f"Written: {OUT}")


if __name__ == "__main__":
    main()
