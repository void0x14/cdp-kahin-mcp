"""Native WebExtension staging and compatibility reporting."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import orjson

from kahin._mcp import mcp
from kahin.extensions import stage_extension
from kahin.tools._common import _RO, _healer_ref


def _extension_root() -> Path:
    configured = os.environ.get("KAHIN_EXTENSION_DIR", "").strip()
    if configured:
        path = Path(configured).expanduser()
    else:
        data_home = os.environ.get("XDG_DATA_HOME", "").strip()
        root = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
        path = root / "kahin" / "extensions"
    if not path.is_absolute():
        raise ValueError("KAHIN_EXTENSION_DIR must be an absolute path")
    return path


def _error(message: str, code: str = "invalid_argument") -> str:
    return orjson.dumps({
        "error": message,
        "code": code,
        "tool": "kahin_extension_prepare",
    }, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_extension_prepare", annotations=_RO)
async def extension_prepare(source: str) -> str:
    """Stage a real WebExtension and report native Camoufox compatibility."""
    async with _healer_ref.safe("kahin_extension_prepare", source=source[:200] if isinstance(source, str) else None):
        if not isinstance(source, str) or not source.strip():
            return _error("source must be a non-empty absolute directory or .zip path")
        try:
            return orjson.dumps(
                stage_extension(source, _extension_root()),
                option=orjson.OPT_INDENT_2,
            ).decode()
        except (OSError, ValueError) as exc:
            return _error(str(exc))
