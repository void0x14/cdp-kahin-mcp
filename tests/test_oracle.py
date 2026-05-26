"""Integration tests for oracle.py — MCP Server."""

import json
import subprocess
import time


def _start_server():
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


def test_server_initialize_and_list_tools():
    proc = _start_server()
    proc.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n')
    proc.stdin.flush()
    resp = json.loads(proc.stdout.readline())
    tools = resp["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert "kahin_list_domains" in tool_names
    assert "kahin_get_command" in tool_names
    assert "kahin_validate_command" in tool_names
    assert "kahin_error_decode" in tool_names
    assert "kahin_find_concept" in tool_names
    assert "kahin_browser_start" in tool_names
    assert "kahin_navigate" in tool_names
    assert "kahin_screenshot" in tool_names
    assert "kahin_execute_cdp" in tool_names
    assert "kahin_list_sessions" in tool_names
    assert "kahin_event_history" in tool_names
    assert "kahin_pattern_learn" in tool_names
    assert "kahin_pattern_query" in tool_names
    assert "kahin_pattern_suggest" in tool_names
    assert "kahin_pattern_forget" in tool_names
    assert "kahin_pattern_stats" in tool_names
    assert len(tools) == 31
    proc.terminate()


def test_kahin_list_domains():
    proc = _start_server()
    proc.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"kahin_list_domains","arguments":{}}}\n')
    proc.stdin.flush()
    resp = json.loads(proc.stdout.readline())
    text = resp["result"]["content"][0]["text"]
    data = json.loads(text)
    assert len(data) == 56
    assert data[0]["domain"] == "Accessibility"
    proc.terminate()


def test_kahin_get_command():
    proc = _start_server()
    proc.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"kahin_get_command","arguments":{"domain":"Page","command":"navigate"}}}\n')
    proc.stdin.flush()
    resp = json.loads(proc.stdout.readline())
    cmd = json.loads(resp["result"]["content"][0]["text"])
    assert cmd["name"] == "navigate"
    assert len(cmd["parameters"]) == 5
    proc.terminate()


def test_kahin_validate_command():
    proc = _start_server()
    proc.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"kahin_validate_command","arguments":{"domain":"Page","command":"navigate","parameters":{"urll":"test"}}}}\n')
    proc.stdin.flush()
    resp = json.loads(proc.stdout.readline())
    val = json.loads(resp["result"]["content"][0]["text"])
    assert val["valid"] is False
    proc.terminate()


def test_kahin_error_decode():
    proc = _start_server()
    proc.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"kahin_error_decode","arguments":{"error_code":-32601,"error_message":"Method not found: Page.navigat"}}}\n')
    proc.stdin.flush()
    resp = json.loads(proc.stdout.readline())
    err = json.loads(resp["result"]["content"][0]["text"])
    assert err["code"] == -32601
    assert "Method not found" in err["name"]
    proc.terminate()
