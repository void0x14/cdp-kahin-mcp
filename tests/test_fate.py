"""Tests for fate.py — Pattern DB."""

import tempfile
from pathlib import Path

from kahin.residual_self.fate import FateDB


def make_fate() -> FateDB:
    return FateDB(path=Path(tempfile.mktemp(suffix=".json")))


def test_learn_and_query():
    f = make_fate()
    f.learn("Page", "navigate", {"url": "https://x.com"}, "login")
    f.learn("Runtime", "evaluate", {"expression": "1+1"}, "debug")
    results = f.query()
    assert len(results) == 2


def test_query_by_domain():
    f = make_fate()
    f.learn("Page", "navigate", {"url": "https://x.com"}, "login")
    f.learn("Page", "reload", {}, "refresh")
    results = f.query(domain="Page")
    assert len(results) == 2
    results = f.query(domain="Runtime")
    assert len(results) == 0


def test_suggest():
    f = make_fate()
    f.learn("Page", "navigate", {"url": "https://x.com"})
    f.learn("Page", "reload", {})
    suggestions = f.suggest("navig")
    names = [s["full_name"] for s in suggestions]
    assert "Page.navigate" in names
    assert "Page.reload" not in names


def test_frequency():
    f = make_fate()
    f.learn("Page", "navigate", {"url": "https://x.com"})
    f.learn("Page", "navigate", {"url": "https://y.com"})
    f.learn("Page", "reload", {})
    results = f.query()
    assert results[0]["domain"] == "Page"
    assert results[0]["command"] == "navigate"
    assert results[0]["frequency"] == 2
