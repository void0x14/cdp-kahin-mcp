"""Edge case and pattern tool tests for oracle.py."""

import json
import subprocess
import time


def _start():
    proc = subprocess.Popen(
        ["python3", "-c", "from kahin.oracle import main; main()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=".",
    )
    proc.stdin.write('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"0.1.0","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}\n')
    proc.stdin.flush()
    json.loads(proc.stdout.readline())
    proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    proc.stdin.flush()
    time.sleep(0.1)
    return proc


def _call(proc, name, args=None):
    if args is None:
        args = {}
    req = f'{{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{{"name":"{name}","arguments":{json.dumps(args)}}}}}'
    proc.stdin.write(req + "\n")
    proc.stdin.flush()
    resp = json.loads(proc.stdout.readline())
    text = resp.get("result", {}).get("content", [{}])[0].get("text", "")
    return text


# === Phase 3: Pattern Tools ===

def test_pattern_learn_and_query():
    proc = _start()
    _call(proc, "kahin_pattern_learn", {"domain": "Page", "command": "navigate", "context": "test"})
    text = _call(proc, "kahin_pattern_query", {"domain": "Page", "limit": 10})
    data = json.loads(text)
    assert len(data) >= 1
    assert data[0]["domain"] == "Page"
    assert data[0]["command"] == "navigate"
    proc.terminate()


def test_pattern_suggest():
    proc = _start()
    _call(proc, "kahin_pattern_learn", {"domain": "Page", "command": "navigate"})
    _call(proc, "kahin_pattern_learn", {"domain": "Page", "command": "reload"})
    text = _call(proc, "kahin_pattern_suggest", {"partial": "navig"})
    data = json.loads(text)
    names = [d["full_name"] for d in data]
    assert "Page.navigate" in names
    proc.terminate()


def test_pattern_forget():
    proc = _start()
    _call(proc, "kahin_pattern_learn", {"domain": "Page", "command": "navigate"})
    text = _call(proc, "kahin_pattern_forget", {"domain": "Page", "command": "navigate"})
    assert "forgotten" in text
    text = _call(proc, "kahin_pattern_query", {})
    data = json.loads(text)
    assert not any(p.get("domain") == "Page" and p.get("command") == "navigate" for p in data)
    proc.terminate()


def test_pattern_stats():
    proc = _start()
    _call(proc, "kahin_pattern_learn", {"domain": "Page", "command": "navigate"})
    _call(proc, "kahin_pattern_learn", {"domain": "Runtime", "command": "evaluate"})
    text = _call(proc, "kahin_pattern_stats", {})
    data = json.loads(text)
    assert data["total"] >= 2
    assert data["domains"] >= 2
    proc.terminate()


# === Edge Cases ===

def test_validate_empty_domain():
    proc = _start()
    text = _call(proc, "kahin_validate_command", {"domain": "", "command": "", "parameters": {}})
    data = json.loads(text)
    assert data["valid"] is False
    proc.terminate()


def test_list_types_nonexistent():
    proc = _start()
    text = _call(proc, "kahin_list_types", {"domain": "NonExistent"})
    data = json.loads(text)
    assert data == []
    proc.terminate()


def test_find_concept_no_match():
    proc = _start()
    text = _call(proc, "kahin_find_concept", {"query": "zzz_nonexistent_zzz"})
    data = json.loads(text)
    assert data == []
    proc.terminate()


def test_get_domain_not_found():
    proc = _start()
    text = _call(proc, "kahin_get_domain", {"domain": "NoSuchDomain"})
    assert "not found" in text
    proc.terminate()


def test_get_command_not_found():
    proc = _start()
    text = _call(proc, "kahin_get_command", {"domain": "Page", "command": "nonexistent"})
    assert "not found" in text
    proc.terminate()


def test_get_event_not_found():
    proc = _start()
    text = _call(proc, "kahin_get_event", {"domain": "Page", "event": "nonexistent"})
    assert "not found" in text
    proc.terminate()


def test_browser_start_stop_noop_twice():
    proc = _start()
    text1 = _call(proc, "kahin_browser_stop", {})
    assert "No engine" in text1
    text2 = _call(proc, "kahin_browser_start", {"engine": "invalid"})
    assert "Unknown engine" in text2
    proc.terminate()


def test_get_type_not_found():
    proc = _start()
    text = _call(proc, "kahin_get_type", {"domain": "Page", "type_name": "NotAType"})
    assert "not found" in text
    proc.terminate()


def test_error_decode_no_args():
    proc = _start()
    text = _call(proc, "kahin_error_decode", {})
    data = json.loads(text)
    assert data["code"] is None
    proc.terminate()
