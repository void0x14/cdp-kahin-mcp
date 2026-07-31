#!/usr/bin/env python3
"""Juggler protocol extractor — build-time only, never ships.

Parses the vendored Firefox Juggler protocol definition
(``vendor/Protocol.js`` — a single object-literal JS file) with tree-sitter
and emits a deterministic JSON schema consumed by Zig compile-time type
generation (MASTER-PLAN §2.4, Faz 0).

The file imports the ``t`` helper namespace from PrimitiveTypes.js via
``ChromeUtils.importESModule``. The ``t.*`` expressions used in
Protocol.js map to schema descriptors as follows:

    t.String          -> "string"          t.Boolean -> "boolean"
    t.Number          -> "number"          t.Any     -> "any"
    t.Null            -> "null"
    t.Enum([...])     -> {"type": "enum", "enum": [values]}
    t.Optional(x)     -> x plus "optional": true   (key may be absent)
    t.Nullable(x)     -> x plus "nullable": true   (value may be null)
    t.Array(x)        -> {"type": "array", "items": x}
    t.Recursive(p, n) -> {"type": "object", "ref": "<Domain>.<Type>"}
    t.Object(x)       -> {"type": "object", "properties": ...} (or bare "object")
    t.Dict(x)         -> {"type": "object"}  (string-keyed map, not enumerated)
    <pool>.<Type>     -> {"type": "object", "ref": "<Domain>.<Type>"}
    {...} inline      -> {"type": "object", "properties": ...} (anonymous)

``t.Nullable`` does not appear in the brief's helper list but is used
extensively in the vendored file (e.g. ``setHTTPCredentials``,
``Page.navigate`` returns), so it is extracted faithfully.

Schema format (version 1):

    {
      "schema_version": 1,
      "source": "<pin label>",
      "domains": {
        "<Domain>": {
          "targets": ["browser" | "page", ...],
          "methods": {"<name>": {"params": [...], "returns": [...]}},
          "events":  {"<name>": [field descriptors]},
          "types":   {"<Type>": {"properties": [field descriptors]}}
        }
      }
    }

Field descriptors: {"name": ..., "type": ..., "optional"/"nullable": bool,
plus a payload for non-primitives: "enum" (values), "items" (array element
type), "ref" ("<Domain>.<Type>"), "properties" (anonymous object). Arrays
are structured ({"type": "array", "items": ...}) instead of the "string[]"
shorthand so nested item types (enum / object refs) stay lossless for
codegen. Ordering is source order throughout — output is deterministic
(no timestamps).

The extractor is deliberately strict: any expression shape it cannot
interpret raises ProtocolParseError with the offending line instead of
silently emitting a guess — a Juggler protocol change must fail loudly,
never produce a quietly wrong schema.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any, NoReturn

from tree_sitter import Language, Node, Parser

# Imported via importlib so type checkers that run outside the project venv
# (which lacks the grammar package) don't report a missing import.
tree_sitter_javascript = importlib.import_module("tree_sitter_javascript")

PROTOCOL_SOURCE = "daijro/camoufox@v152.0.4-beta.28"
SCHEMA_VERSION = 1

# Default input: the vendored copy pinned in vendor/camoufox-fork-pin.lock.
VENDOR_PROTOCOL_PATH = Path(__file__).resolve().parent.parent.parent / "vendor" / "Protocol.js"

# t.* helpers that denote a primitive type.
_PRIMITIVE_HELPERS = {
    "String": "string",
    "Boolean": "boolean",
    "Number": "number",
    "Any": "any",
    "Null": "null",
}


class ProtocolParseError(ValueError):
    """Raised when the extractor meets a JS shape it cannot interpret."""


def _iter_named(node: Node, node_type: str) -> Any:
    """Yield all descendants (incl. self) of ``node`` with the given type."""
    if node.type == node_type:
        yield node
    for child in node.named_children:
        yield from _iter_named(child, node_type)


def _field(node: Node, name: str) -> Node:
    """Field access that asserts presence (tree-sitter schema guarantees it)."""
    child = node.child_by_field_name(name)
    if child is None:
        raise ProtocolParseError(f"{node.type} node has no field {name!r} at line {node.start_point[0] + 1}")
    return child


def _pairs(obj: Node) -> list[Node]:
    return [c for c in obj.named_children if c.type == "pair"]


def _node_text(node: Node) -> str:
    """``node.text`` as str (never None per tree-sitter runtime semantics)."""
    return (node.text or b"").decode()


def _string_value(node: Node) -> str:
    """Unquoted text of a ``string`` node; ``""`` when absent.

    The JS grammar has no ``string_fragment`` field — the fragment is the
    first named child (or we fall back to unquoting the raw text).
    """
    frag = node.named_children[0] if node.named_children else None
    if frag is not None and frag.type == "string_fragment":
        return _node_text(frag)
    raw = _node_text(node).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return raw


def _pair_key(node: Node) -> str:
    key = _field(node, "key")
    if key.type == "string":
        return _string_value(key)
    return _node_text(key)


class _Extractor:
    """Tree-sitter based reader for the object-literal Protocol.js subset."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.tree = Parser(Language(tree_sitter_javascript.language())).parse(source.encode("utf-8"))
        self.root = self.tree.root_node
        # pool variable name -> {TypeName: object node}; populated from
        # `browserTypes.TargetInfo = {...}` style assignments.
        self.pools: dict[str, dict[str, Any]] = {}
        # domain name -> {"node": ..., "types_pool": str | None}
        self.domains: dict[str, dict[str, Any]] = {}
        self._collect()

    # ------------------------------------------------------------------ helpers

    def _text(self, node: Node) -> str:
        return self.source[node.start_byte : node.end_byte]

    def _fail(self, node: Node, why: str) -> NoReturn:
        snippet = self._text(node).replace("\n", " ")[:80]
        raise ProtocolParseError(f"{why} at line {self._line(node)}: {snippet!r}")

    @staticmethod
    def _line(node: Node) -> int:
        return node.start_point[0] + 1

    def _warn(self, message: str) -> None:
        print(f"[extractor] warning: {message}", file=sys.stderr)

    # ------------------------------------------------------------------ collection

    def _collect(self) -> None:
        """Find type pools, their members and the domain objects."""
        const_objects: dict[str, Node] = {}
        for decl in _iter_named(self.root, "lexical_declaration"):
            # tree-sitter-javascript grammar: lexical_declaration has no
            # `declarator` field; the declarator is its first named child.
            declarator = decl.named_children[0] if decl.named_children and decl.named_children[0].type == "variable_declarator" else None
            name = declarator.child_by_field_name("name") if declarator else None
            value = declarator.child_by_field_name("value") if declarator else None
            if name is None or value is None or name.type != "identifier" or value.type != "object":
                continue
            const_objects[self._text(name)] = value

        for stmt in _iter_named(self.root, "expression_statement"):
            expr = stmt.named_children[0] if stmt.named_children else None
            if expr is None or expr.type != "assignment_expression":
                continue
            left = expr.child_by_field_name("left")
            right = expr.child_by_field_name("right")
            if left is None or right is None or left.type != "member_expression":
                continue
            pool_name = self._text(_field(left, "object"))
            member = self._text(_field(left, "property"))
            if pool_name not in const_objects:
                continue  # not a protocol pool (e.g. a const mutated later)
            if right.type != "object":
                self._fail(right, f"pool member {pool_name}.{member} is not an object literal")
            self.pools.setdefault(pool_name, {})[member] = right

        # A domain is a const object literal carrying a `targets` key.
        for name, node in const_objects.items():
            keys = {_pair_key(p) for p in _pairs(node)}
            if "targets" in keys:
                types_pool = None
                for pair in _pairs(node):
                    if _pair_key(pair) == "types":
                        value = _field(pair, "value")
                        if value.type == "identifier":
                            types_pool = self._text(value)
                self.domains[name] = {"node": node, "types_pool": types_pool}

        # Resolve which domain owns each pool (for cross-domain type refs).
        for pool_name, members in self.pools.items():
            owner = next((d for d, info in self.domains.items() if info["types_pool"] == pool_name), None)
            members["__domain"] = owner  # type: ignore[assignment]

    def _domain_order(self) -> list[str]:
        """Canonical domain order from `export const protocol = {domains: {...}}`."""
        for decl in _iter_named(self.root, "lexical_declaration"):
            declarator = decl.named_children[0] if decl.named_children and decl.named_children[0].type == "variable_declarator" else None
            name = declarator.child_by_field_name("name") if declarator else None
            value = declarator.child_by_field_name("value") if declarator else None
            if name is None or value is None or self._text(name) != "protocol" or value.type != "object":
                continue
            for pair in _pairs(value):
                if _pair_key(pair) == "domains":
                    order: list[str] = []
                    for p in _pairs(_field(pair, "value")):
                        pv = _field(p, "value")
                        if pv.type == "identifier":
                            order.append(self._text(pv))
                    if order:
                        return order
        return list(self.domains)  # fallback: declaration order

    # ------------------------------------------------------------------ type expressions

    def _parse_type(self, node: Node) -> dict[str, Any]:
        node_type = node.type
        if node_type == "identifier":
            name = self._text(node)
            if name == "t":
                self._fail(node, "bare `t` reference used as a type")
            self._fail(node, f"unknown identifier {name!r} used as a type")
        if node_type == "member_expression":
            return self._parse_member(node)
        if node_type == "call_expression":
            fn = _field(node, "function")
            if fn.type == "member_expression" and self._text(_field(fn, "object")) == "t":
                return self._parse_t_call(self._text(_field(fn, "property")), _field(node, "arguments"), node)
            self._fail(node, "unsupported call expression as a type")
        if node_type == "object":
            return {"type": "object", "properties": self._parse_fields(node)}
        self._fail(node, f"unsupported type expression ({node_type})")

    def _parse_member(self, node: Node) -> dict[str, Any]:
        obj = _field(node, "object")
        prop = _field(node, "property")
        obj_name, prop_name = self._text(obj), self._text(prop)
        if obj_name == "t":
            if prop_name in _PRIMITIVE_HELPERS:
                return {"type": _PRIMITIVE_HELPERS[prop_name]}
            self._fail(node, f"unknown t helper t.{prop_name}")
        if obj_name in self.pools:
            return {"type": "object", "ref": self._ref(obj_name, prop_name, node)}
        self._fail(node, f"member access on unknown object {obj_name!r}")

    def _ref(self, pool_name: str, type_name: str, node: Node) -> str:
        owner = self.pools[pool_name].get("__domain")
        if owner is None:
            self._fail(node, f"type pool {pool_name!r} is not owned by any domain")
        return f"{owner}.{type_name}"

    def _parse_t_call(self, helper: str, args: Node, node: Node) -> dict[str, Any]:
        argv = list(args.named_children)
        if helper == "Enum":
            if len(argv) != 1 or argv[0].type != "array":
                self._fail(node, "t.Enum expects a single array argument")
            return {"type": "enum", "enum": [self._literal(a) for a in argv[0].named_children]}
        if helper in ("Optional", "Nullable"):
            if len(argv) != 1:
                self._fail(node, f"t.{helper} expects a single argument")
            desc = self._parse_type(argv[0])
            desc["optional" if helper == "Optional" else "nullable"] = True
            return desc
        if helper == "Array":
            if len(argv) != 1:
                self._fail(node, "t.Array expects a single argument")
            return {"type": "array", "items": self._parse_type(argv[0])}
        if helper == "Recursive":
            if len(argv) != 2 or argv[0].type != "identifier" or argv[1].type != "string":
                self._fail(node, "t.Recursive expects (pool, 'TypeName')")
            pool_name = self._text(argv[0])
            type_name = _string_value(argv[1])
            return {"type": "object", "ref": self._ref(pool_name, type_name, node)}
        if helper == "Object":
            if len(argv) == 1 and argv[0].type == "object":
                return {"type": "object", "properties": self._parse_fields(argv[0])}
            return {"type": "object"}
        if helper == "Dict":
            return {"type": "object"}
        self._fail(node, f"unknown t helper t.{helper}")

    @staticmethod
    def _literal(node: Node) -> Any:
        if node.type == "string":
            return _string_value(node)
        if node.type == "true":
            return True
        if node.type == "false":
            return False
        if node.type == "null":
            return None
        if node.type == "number":
            text = _node_text(node)
            return float(text) if ("." in text or "e" in text.lower()) else int(text)
        raise ProtocolParseError(f"unsupported enum literal: {node.type}")

    # ------------------------------------------------------------------ field maps

    def _parse_fields(self, obj: Node) -> list[dict[str, Any]]:
        """Parse `{name: typeExpr, ...}` into ordered field descriptors."""
        fields: list[dict[str, Any]] = []
        for pair in _pairs(obj):
            value = _field(pair, "value")
            desc = self._parse_type(value)
            fields.append({"name": _pair_key(pair), **desc})
        return fields

    # ------------------------------------------------------------------ domains

    def _parse_method(self, name: str, node: Node) -> dict[str, Any]:
        params: list[dict[str, Any]] = []
        returns: list[dict[str, Any]] = []
        for pair in _pairs(node):
            key = _pair_key(pair)
            value = _field(pair, "value")
            if key == "params":
                params = self._parse_fields(value)
            elif key == "returns":
                returns = self._parse_fields(value)
            else:
                self._warn(f"method {name}: unknown key {key!r} ignored")
        return {"params": params, "returns": returns}

    def _domain_schema(self, name: str) -> dict[str, Any]:
        info = self.domains[name]
        out: dict[str, Any] = {"methods": {}, "events": {}, "types": {}}
        for pair in _pairs(info["node"]):
            key = _pair_key(pair)
            value = _field(pair, "value")
            if key == "targets":
                out["targets"] = [_string_value(e) if e.type == "string" else self._text(e) for e in value.named_children]
            elif key == "types":
                if value.type == "identifier":  # pool reference
                    pool = self.pools.get(self._text(value), {})
                    for type_name, type_node in pool.items():
                        if type_name == "__domain":
                            continue
                        out["types"][type_name] = {"properties": self._parse_fields(type_node)}
                elif value.type == "object":  # inline (e.g. Heap's `types: {}`)
                    for pair2 in _pairs(value):
                        out["types"][_pair_key(pair2)] = {
                            "properties": self._parse_fields(_field(pair2, "value"))
                        }
            elif key == "events":
                for pair2 in _pairs(value):
                    out["events"][_pair_key(pair2)] = self._parse_fields(_field(pair2, "value"))
            elif key == "methods":
                for pair2 in _pairs(value):
                    mname = _pair_key(pair2)
                    out["methods"][mname] = self._parse_method(mname, _field(pair2, "value"))
            else:
                self._warn(f"domain {name}: unknown key {key!r} ignored")
        return out

    def extract(self) -> dict[str, Any]:
        return {name: self._domain_schema(name) for name in self._domain_order()}


def extract_schema(source: str, source_label: str = PROTOCOL_SOURCE) -> dict[str, Any]:
    """Parse Juggler Protocol.js source and return the schema dict (v1)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "source": source_label,
        "domains": _Extractor(source).extract(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="extractor.py",
        description="Extract the Juggler protocol schema from Protocol.js (build-time tool).",
    )
    parser.add_argument("source", nargs="?", type=Path, default=VENDOR_PROTOCOL_PATH,
                        help="path to Protocol.js (default: vendor/Protocol.js)")
    parser.add_argument("--output", type=Path, default=None,
                        help="write schema to this file instead of stdout")
    parser.add_argument("--source-label", default=PROTOCOL_SOURCE,
                        help="pin label recorded in the schema (default: %(default)s)")
    args = parser.parse_args(argv)

    source = args.source.read_text(encoding="utf-8")
    schema = extract_schema(source, args.source_label)
    payload = json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)

    n_methods = sum(len(d["methods"]) for d in schema["domains"].values())
    n_events = sum(len(d["events"]) for d in schema["domains"].values())
    n_types = sum(len(d["types"]) for d in schema["domains"].values())
    print(f"[extractor] {len(schema['domains'])} domains, {n_methods} methods, "
          f"{n_events} events, {n_types} types from {args.source}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
