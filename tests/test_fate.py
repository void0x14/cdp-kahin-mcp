"""Tests for fate.py — Pattern DB."""

from kahin.residual_self.fate import FateDB


def test_learn_and_query(fate: FateDB) -> None:
    fate.learn("Page", "navigate", {"url": "https://x.com"}, "login")
    fate.learn("Runtime", "evaluate", {"expression": "1+1"}, "debug")
    results = fate.query()
    assert len(results) == 2


def test_query_by_domain(fate: FateDB) -> None:
    fate.learn("Page", "navigate", {"url": "https://x.com"}, "login")
    fate.learn("Page", "reload", {}, "refresh")
    results = fate.query(domain="Page")
    assert len(results) == 2
    results = fate.query(domain="Runtime")
    assert len(results) == 0


def test_suggest(fate: FateDB) -> None:
    fate.learn("Page", "navigate", {"url": "https://x.com"})
    fate.learn("Page", "reload", {})
    suggestions = fate.suggest("navig")
    names = [s["full_name"] for s in suggestions]
    assert "Page.navigate" in names
    assert "Page.reload" not in names


def test_frequency(fate: FateDB) -> None:
    fate.learn("Page", "navigate", {"url": "https://x.com"})
    fate.learn("Page", "navigate", {"url": "https://y.com"})
    fate.learn("Page", "reload", {})
    results = fate.query()
    assert results[0]["domain"] == "Page"
    assert results[0]["command"] == "navigate"
    assert results[0]["frequency"] == 2
