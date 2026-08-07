"""Saf tests for the locator engine (no browser needed)."""

from __future__ import annotations

import pytest

from kahin.locators import parse_locator, selector_all_js, selector_js


def test_parse_bare_css() -> None:
    locs = parse_locator("#submit")
    assert [(loc.engine, loc.value, loc.exact) for loc in locs] == [("css", "#submit", False)]


def test_parse_prefixed_engines() -> None:
    locs = parse_locator("text=Save")
    assert locs[0].engine == "text" and locs[0].value == "Save" and locs[0].exact is True
    locs = parse_locator("text^=Save")
    assert locs[0].engine == "text" and locs[0].exact is False
    locs = parse_locator("role=button")
    assert locs[0].engine == "role" and locs[0].value == "button"
    locs = parse_locator("xpath=//button[1]")
    assert locs[0].engine == "xpath" and locs[0].value == "//button[1]"


def test_parse_nth_and_chain() -> None:
    locs = parse_locator("css=.list >> text=Buy >> nth=1")
    assert [loc.engine for loc in locs] == ["css", "text", "index"]
    assert locs[2].value == "1"


def test_selector_js_css_uses_query_selector() -> None:
    js = selector_js("#submit")
    assert "document.querySelector" in js and '"#submit"' in js


def test_selector_js_nth_wraps_all() -> None:
    js = selector_js(".item >> nth=2")
    assert "document.querySelectorAll" in js
    assert "[2]" in js


def test_selector_all_js_returns_array() -> None:
    js = selector_all_js("button")
    assert "querySelectorAll" in js


def test_selector_js_text_exact_and_substring_differ() -> None:
    exact = selector_js("text=Save")
    substring = selector_js("text^=Save")
    assert "===" in exact and "===" not in substring


def test_selector_js_xpath() -> None:
    js = selector_js("xpath=//button[1]")
    assert "document.evaluate" in js


def test_empty_and_unknown_prefix() -> None:
    with pytest.raises(ValueError):
        parse_locator("")
    locs = parse_locator("bogus=foo")
    # unknown prefixes fall back to CSS
    assert locs[0].engine == "css" and locs[0].value == "bogus=foo"


def test_selector_all_rejects_trailing_nth() -> None:
    with pytest.raises(ValueError):
        selector_all_js(".a >> nth=1")
