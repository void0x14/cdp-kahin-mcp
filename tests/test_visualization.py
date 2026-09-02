import pytest

from kahin.visualization import render_chart


def test_line_chart_contains_scaled_polyline_and_escaped_title():
    result = render_chart(
        [{"day": "Mon", "count": 2}, {"day": "Tue", "count": 5}],
        "line",
        "day",
        "count",
        None,
        "<Traffic>",
    )

    assert result["summary"]["rowCount"] == 2
    assert "<Traffic>" not in result["svg"]
    assert "&lt;Traffic&gt;" in result["svg"]
    assert "polyline" in result["svg"]


def test_scatter_chart_reports_numeric_bounds():
    result = render_chart(
        [{"x": 1, "y": 4}, {"x": 3, "y": 8}],
        "scatter",
        "x",
        "y",
        None,
        "Points",
    )

    assert result["summary"]["xMin"] == 1
    assert result["summary"]["xMax"] == 3
    assert result["summary"]["yMin"] == 4
    assert result["summary"]["yMax"] == 8
    assert "circle" in result["svg"]


def test_pie_chart_rejects_non_positive_values():
    with pytest.raises(ValueError, match="positive"):
        render_chart([{"label": "A", "value": 0}], "pie", "label", "value", None, "")


def test_unknown_chart_type_is_rejected():
    with pytest.raises(ValueError, match="chart_type"):
        render_chart([], "radar", "x", "y", None, "")
