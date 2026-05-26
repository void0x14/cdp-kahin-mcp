"""Tests for the_source/architect.py — Schema Engine."""


def test_load(schema) -> None:
    assert len(schema.domains) == 56
    assert len(schema.commands) == 667
    assert len(schema.events) == 237
    assert len(schema.types) >= 600


def test_list_domains(schema) -> None:
    doms = schema.list_domains()
    assert len(doms) == 56
    assert doms[0]["domain"] == "Accessibility"
    assert doms[0]["command_count"] > 0


def test_get_domain(schema) -> None:
    d = schema.get_domain("Page")
    assert d is not None
    assert d["domain"] == "Page"
    assert len(d["commands"]) > 30
    assert len(d["events"]) > 10


def test_get_domain_not_found(schema) -> None:
    assert schema.get_domain("NonExistent") is None


def test_get_command(schema) -> None:
    c = schema.get_command("Page", "navigate")
    assert c is not None
    assert c["name"] == "navigate"
    assert len(c["parameters"]) == 5
    assert c["parameters"][0]["name"] == "url"
    assert c["parameters"][0]["type"] == "string"
    assert c["parameters"][0]["optional"] is False


def test_get_command_not_found(schema) -> None:
    assert schema.get_command("Page", "nonexistent") is None


def test_get_event(schema) -> None:
    e = schema.get_event("Page", "loadEventFired")
    assert e is not None
    assert e["name"] == "loadEventFired"


def test_find_concept(schema) -> None:
    results = schema.find_concept("screenshot", 5)
    assert len(results) >= 1
    names = [r["name"] for r in results]
    assert "Page.captureScreenshot" in names


def test_validate_command_valid(schema) -> None:
    res = schema.validate_command("Page", "navigate", {"url": "https://example.com"})
    assert res["valid"] is True
    assert len(res["errors"]) == 0


def test_validate_command_typo(schema) -> None:
    res = schema.validate_command("Page", "navigate", {"url": 123})
    res2 = schema.validate_command("Page", "navigate", {"urll": "https://x.com", "referrerr": "bad"})
    assert res2["valid"] is False
    messages = [e["message"] for e in res2["errors"]]
    assert any("Did you mean" in m or "Unknown" in m for m in messages)


def test_validate_command_missing_required(schema) -> None:
    res = schema.validate_command("Page", "navigate", {})
    assert res["valid"] is False
    assert any("Missing required" in e["message"] for e in res["errors"])


def test_validate_command_unknown_command(schema) -> None:
    res = schema.validate_command("Page", "nonexistent", {})
    assert res["valid"] is False
    assert "Unknown command" in res["errors"][0]["message"]


def test_error_decode(schema) -> None:
    res = schema.error_decode(-32601, "Method not found: Page.navigat")
    assert res["code"] == -32601
    assert res["name"] == "Method not found"


def test_error_decode_no_code(schema) -> None:
    res = schema.error_decode(error_message="Some error occurred")
    assert res["code"] is None


def test_get_type(schema) -> None:
    t = schema.types.get("Page.Frame")
    assert t is not None
    assert t.name == "Frame"
    assert len(t.properties) > 0


def test_list_types(schema) -> None:
    domain_types = [t for t in schema.types.values() if t.domain == "Page"]
    assert len(domain_types) > 5
