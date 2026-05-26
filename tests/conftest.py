"""Shared fixtures for CDP-Kahin tests."""

import os
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from kahin.residual_self.fate import FateDB
from kahin.the_source.architect import SchemaEngine


@pytest.fixture(scope="module")
def schema() -> SchemaEngine:
    s = SchemaEngine()
    s.load()
    return s


@pytest.fixture
def fate() -> Generator[FateDB, None, None]:
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    db = FateDB(path=Path(path))
    yield db
    Path(path).unlink(missing_ok=True)
