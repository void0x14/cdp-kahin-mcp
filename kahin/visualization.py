"""Deterministic, bounded SVG charts for structured crawl and page data."""

from __future__ import annotations

import html
import math
from collections import OrderedDict
from numbers import Real
from typing import Any

MAX_ROWS = 5_000
MAX_FIELD_LENGTH = 128
MAX_TITLE_LENGTH = 256
MAX_LABEL_LENGTH = 120
SVG_WIDTH = 960
SVG_HEIGHT = 540
_PLOT_LEFT = 72
_PLOT_TOP = 56
_PLOT_WIDTH = 840
_PLOT_HEIGHT = 400
_PALETTE = ("#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed", "#0891b2")


def _text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    value = value.strip()
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return value


def _number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must contain finite numbers")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must contain finite numbers")
    return number


def _label(value: Any) -> str:
    return str(value)[:MAX_LABEL_LENGTH]


def _scale(value: float, minimum: float, maximum: float, start: float, length: float) -> float:
    if maximum == minimum:
        return start + length / 2
    return start + ((value - minimum) / (maximum - minimum)) * length


def _svg_text(value: Any, x: float, y: float, *, anchor: str = "start", size: int = 14) -> str:
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" text-anchor="{anchor}" '
        f'font-family="system-ui,sans-serif" font-size="{size}">{html.escape(_label(value))}</text>'
    )


def _axis(title: str, x_field: str, y_field: str, labels: list[str]) -> list[str]:
    out = [
        f'<line x1="{_PLOT_LEFT}" y1="{_PLOT_TOP + _PLOT_HEIGHT}" '
        f'x2="{_PLOT_LEFT + _PLOT_WIDTH}" y2="{_PLOT_TOP + _PLOT_HEIGHT}" stroke="#64748b"/>',
        f'<line x1="{_PLOT_LEFT}" y1="{_PLOT_TOP}" x2="{_PLOT_LEFT}" '
        f'y2="{_PLOT_TOP + _PLOT_HEIGHT}" stroke="#64748b"/>',
        _svg_text(x_field, _PLOT_LEFT + _PLOT_WIDTH / 2, 510, anchor="middle", size=13),
        _svg_text(y_field, 18, _PLOT_TOP + _PLOT_HEIGHT / 2, anchor="middle", size=13),
    ]
    if labels:
        stride = max(1, math.ceil(len(labels) / 12))
        for index in range(0, len(labels), stride):
            x = _PLOT_LEFT + (index / max(1, len(labels) - 1)) * _PLOT_WIDTH
            out.append(_svg_text(labels[index], x, 480, anchor="middle", size=11))
    return out


def _legend(series: list[str]) -> list[str]:
    out: list[str] = []
    for index, label in enumerate(series):
        x = _PLOT_LEFT + index * 140
        if x > _PLOT_LEFT + _PLOT_WIDTH - 120:
            break
        color = _PALETTE[index % len(_PALETTE)]
        out.append(f'<rect x="{x}" y="22" width="12" height="12" fill="{color}"/>')
        out.append(_svg_text(label, x + 18, 33, size=11))
    return out


def _chart_rows(
    rows: list[dict[str, Any]],
    chart_type: str,
    x_field: str,
    y_field: str,
    color_field: str | None,
) -> tuple[list[tuple[str, float, float, str]], list[str]]:
    normalized: list[tuple[str, float, float, str]] = []
    series: OrderedDict[str, None] = OrderedDict()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"rows[{index}] must be an object")
        if x_field not in row or y_field not in row:
            raise ValueError(f"row {index} is missing x_field or y_field")
        x_raw = row[x_field]
        x = _number(x_raw, field=x_field) if chart_type == "scatter" else 0.0
        y = _number(row[y_field], field=y_field)
        group = _label(row.get(color_field, "default")) if color_field else "default"
        series.setdefault(group, None)
        normalized.append((_label(x_raw), x, y, group))
    if chart_type == "pie" and any(y <= 0 for _, _, y, _ in normalized):
        raise ValueError("pie values must be positive")
    return normalized, list(series)


def render_chart(
    rows: list[dict[str, Any]],
    chart_type: str,
    x_field: str,
    y_field: str,
    color_field: str | None,
    title: str,
) -> dict[str, Any]:
    """Validate rows and render one deterministic bounded SVG chart."""
    chart_type = _text(chart_type, field="chart_type", maximum=32).lower()
    if chart_type not in {"line", "bar", "scatter", "pie"}:
        raise ValueError("chart_type must be line, bar, scatter, or pie")
    if not isinstance(rows, list) or not rows:
        raise ValueError("rows must be a non-empty list")
    if len(rows) > MAX_ROWS:
        raise ValueError(f"rows cannot contain more than {MAX_ROWS} items")
    x_field = _text(x_field, field="x_field", maximum=MAX_FIELD_LENGTH)
    y_field = _text(y_field, field="y_field", maximum=MAX_FIELD_LENGTH)
    title = _text(title or f"{y_field} by {x_field}", field="title", maximum=MAX_TITLE_LENGTH)
    if color_field is not None:
        color_field = _text(color_field, field="color_field", maximum=MAX_FIELD_LENGTH)
    normalized, series = _chart_rows(rows, chart_type, x_field, y_field, color_field)
    y_values = [item[2] for item in normalized]
    x_values = [item[1] for item in normalized] if chart_type == "scatter" else []
    y_min, y_max = min(y_values), max(y_values)
    x_min, x_max = (min(x_values), max(x_values)) if x_values else (None, None)
    labels = [item[0] for item in normalized]
    body = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SVG_WIDTH}" height="{SVG_HEIGHT}" '
        f'viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}" role="img" aria-label="{html.escape(title, quote=True)}">',
        _svg_text(title, SVG_WIDTH / 2, 18, anchor="middle", size=16),
    ]

    if chart_type == "pie":
        total = sum(y_values)
        cx, cy, radius = _PLOT_LEFT + _PLOT_WIDTH / 2, 250, 150
        start = -math.pi / 2
        for index, (label, _, value, group) in enumerate(normalized):
            angle = value / total * math.tau
            end = start + angle
            x1, y1 = cx + radius * math.cos(start), cy + radius * math.sin(start)
            x2, y2 = cx + radius * math.cos(end), cy + radius * math.sin(end)
            large = 1 if angle > math.pi else 0
            color = _PALETTE[index % len(_PALETTE)]
            body.append(
                f'<path d="M {cx:.2f} {cy:.2f} L {x1:.2f} {y1:.2f} '
                f'A {radius} {radius} 0 {large} 1 {x2:.2f} {y2:.2f} Z" '
                f'fill="{color}" data-label="{html.escape(label, quote=True)}"/>'
            )
            start = end
        body.extend(_legend(labels))
    else:
        body.extend(_axis(title, x_field, y_field, labels))
        grouped: dict[str, list[tuple[int, tuple[str, float, float, str]]]] = {}
        for index, item in enumerate(normalized):
            grouped.setdefault(item[3], []).append((index, item))
        if chart_type == "bar":
            slot = _PLOT_WIDTH / len(normalized)
            for index, (label, _, value, group) in enumerate(normalized):
                height = (value - min(0.0, y_min)) / max(1.0, y_max - min(0.0, y_min)) * _PLOT_HEIGHT
                x = _PLOT_LEFT + index * slot + slot * 0.15
                y = _PLOT_TOP + _PLOT_HEIGHT - height
                body.append(
                    f'<rect x="{x:.2f}" y="{y:.2f}" width="{slot * 0.7:.2f}" height="{height:.2f}" '
                    f'fill="{_PALETTE[series.index(group) % len(_PALETTE)]}" '
                    f'data-label="{html.escape(label, quote=True)}"/>'
                )
        elif chart_type == "scatter":
            for _, (_, x, y, group) in enumerate(normalized):
                px = _scale(x, x_min or 0.0, x_max or 0.0, _PLOT_LEFT, _PLOT_WIDTH)
                py = _PLOT_TOP + _PLOT_HEIGHT - _scale(y, y_min, y_max, 0, _PLOT_HEIGHT)
                body.append(
                    f'<circle cx="{px:.2f}" cy="{py:.2f}" r="5" '
                    f'fill="{_PALETTE[series.index(group) % len(_PALETTE)]}"/>'
                )
        else:
            for group, items in grouped.items():
                points = []
                for index, (_, _, value, _) in items:
                    px = _PLOT_LEFT + (index / max(1, len(normalized) - 1)) * _PLOT_WIDTH
                    py = _PLOT_TOP + _PLOT_HEIGHT - _scale(value, y_min, y_max, 0, _PLOT_HEIGHT)
                    points.append(f"{px:.2f},{py:.2f}")
                body.append(
                    f'<polyline points="{" ".join(points)}" fill="none" stroke-width="3" '
                    f'stroke="{_PALETTE[series.index(group) % len(_PALETTE)]}"/>'
                )
        body.extend(_legend(series) if color_field else [])
    body.append("</svg>")
    svg = "".join(body)
    summary: dict[str, Any] = {
        "rowCount": len(normalized),
        "series": series,
        "yMin": y_min,
        "yMax": y_max,
    }
    if x_min is not None and x_max is not None:
        summary.update({"xMin": x_min, "xMax": x_max})
    return {
        "type": chart_type,
        "fields": {"x": x_field, "y": y_field, "color": color_field},
        "summary": summary,
        "svg": svg,
    }
