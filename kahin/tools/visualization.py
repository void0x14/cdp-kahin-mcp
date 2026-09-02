"""Data visualization tools built from real, caller-provided rows."""

from __future__ import annotations

from typing import Any

import orjson

from kahin._mcp import mcp
from kahin.tools._common import _RO, _healer_ref
from kahin.visualization import render_chart


def _error(message: str, *, field: str | None = None) -> str:
    payload: dict[str, Any] = {
        "error": message,
        "code": "invalid_argument",
        "tool": "kahin_visualize_data",
    }
    if field is not None:
        payload["field"] = field
    return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()


@mcp.tool(name="kahin_visualize_data", annotations=_RO)
async def visualize_data(
    rows: list[dict[str, Any]],
    chartType: str = "line",
    xField: str = "",
    yField: str = "",
    colorField: str | None = None,
    title: str = "",
) -> str:
    """Render bounded caller-provided rows as a deterministic SVG chart."""
    async with _healer_ref.safe(
        "kahin_visualize_data",
        chartType=chartType,
        xField=xField,
        yField=yField,
        rowCount=len(rows) if isinstance(rows, list) else None,
    ):
        try:
            payload = render_chart(rows, chartType, xField, yField, colorField, title)
        except ValueError as exc:
            return _error(str(exc))
        return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()
