"""Agent snapshot formatting tests (pure Python)."""

from __future__ import annotations

from kahin.agent_snapshot import format_snapshot


def _node(node_id: str, **overrides: object) -> dict:
    base = {
        "nodeId": node_id, "tag": "button", "role": "button", "name": "",
        "text": "", "visible": True, "rect": {"x": 0, "y": 0, "width": 10, "height": 10},
        "attributes": {}, "actions": [], "children": [],
    }
    base.update(overrides)
    return base


def test_format_basic_tree() -> None:
    tree = _node("n1", tag="main", role=None, children=[
        _node("n2", tag="h1", role="heading", name="Kahin", text="Kahin MCP"),
        _node("n3", tag="input", role="textbox", name="Search", actions=["focus", "type"]),
        _node("n4", tag="button", role="button", name="Go", actions=["click"]),
        _node("n5", tag="a", role="link", name="Docs", actions=["click"]),
    ])
    result = format_snapshot(tree, max_tokens=5000)
    lines = result["lines"]
    assert any("heading" in line and "Kahin" in line and "[ref=n2]" in line for line in lines)
    assert any('textbox "Search" [ref=n3] [focus, type]' in line for line in lines)
    assert any('button "Go" [ref=n4] [click]' in line for line in lines)
    assert result["truncated"] is False
    assert result["nodeCount"] == 5 and result["shownCount"] == 4


def test_hidden_nodes_skipped() -> None:
    tree = _node("n1", tag="div", role=None, children=[
        _node("n2", tag="button", role="button", name="Shown", actions=["click"]),
        _node("n3", tag="button", role="button", name="Hidden", visible=False, actions=["click"]),
    ])
    result = format_snapshot(tree)
    assert "Shown" in "\n".join(result["lines"])
    assert "Hidden" not in "\n".join(result["lines"])


def test_token_budget_truncates() -> None:
    children = [_node(f"n{i}", tag="button", role="button", name=f"Item {i}", actions=["click"]) for i in range(50)]
    tree = _node("n1", tag="div", role=None, children=children)
    result = format_snapshot(tree, max_tokens=30)
    assert result["truncated"] is True
    assert result["shownCount"] < 50
    assert result["tokens_estimate"] <= result["max_tokens"]


def test_text_is_capped_and_indented() -> None:
    tree = _node("n1", tag="div", role=None, children=[
        _node("n2", tag="button", role="button", name="X" * 200, actions=["click"]),
    ])
    result = format_snapshot(tree)
    lines = result["lines"]
    assert len(lines[0]) < 120
    assert lines[0].startswith("- ") or lines[0].startswith("  ")
