"""Shared fixtures for CDP-Kahin tests."""

import pytest

from kahin.the_source.architect import SchemaEngine


@pytest.fixture(scope="module")
def schema() -> SchemaEngine:
    s = SchemaEngine()
    s.load()
    return s
