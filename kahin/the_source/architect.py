"""the_source/architect.py — Schema Engine (Matrix'in Mimarı)."""

from __future__ import annotations

import gzip
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import Levenshtein
import orjson

HERE = Path(__file__).resolve().parent
PROTOCOL_PATH = HERE / "protocol.json.gz"


@dataclass
class ParamInfo:
    name: str
    type: str
    optional: bool
    description: str


@dataclass
class CommandInfo:
    domain: str
    name: str
    full_name: str
    description: str
    parameters: list[ParamInfo]
    returns: list[ParamInfo]
    deprecated: bool
    experimental: bool


@dataclass
class EventInfo:
    domain: str
    name: str
    full_name: str
    description: str
    parameters: list[ParamInfo]
    deprecated: bool
    experimental: bool


@dataclass
class DomainInfo:
    name: str
    description: str
    version: str
    commands: list[CommandInfo]
    events: list[EventInfo]
    types: list[TypeInfo]


@dataclass
class TypeProperty:
    name: str
    type: str
    optional: bool
    description: str
    enum_values: list[str] | None


@dataclass
class TypeInfo:
    domain: str
    name: str
    full_name: str
    type: str
    description: str
    enum_values: list[str] | None
    properties: list[TypeProperty]


class SchemaEngine:
    """CDP schema engine. Loads protocol.json.gz, indexes everything."""

    def __init__(self) -> None:
        self.domains: dict[str, DomainInfo] = {}
        self.commands: dict[str, CommandInfo] = {}
        self.events: dict[str, EventInfo] = {}
        self.types: dict[str, TypeInfo] = {}
        self._keyword_index: dict[str, set[str]] = {}
        self.protocol_version = ""
        self.load_time = 0.0

    def load(self, path: str | Path | None = None) -> None:
        t0 = time.time()
        p = Path(path) if path else PROTOCOL_PATH
        raw = gzip.decompress(p.read_bytes())
        data: dict[str, Any] = orjson.loads(raw)
        version = data.get("version", {})
        self.protocol_version = version.get("major", "unknown")

        for domain in data.get("domains", []):
            self._index_domain(domain)

        self._build_keyword_index()
        self.load_time = time.time() - t0

    def _index_domain(self, domain: dict[str, Any]) -> None:
        name = domain["domain"]
        desc = domain.get("description", "")
        cmds_list: list[CommandInfo] = []
        evts_list: list[EventInfo] = []
        typs_list: list[TypeInfo] = []

        for cmd in domain.get("commands", []):
            params = _parse_params(cmd.get("parameters", []))
            returns = _parse_params(cmd.get("returns", []))
            ci = CommandInfo(
                domain=name,
                name=cmd["name"],
                full_name=f"{name}.{cmd['name']}",
                description=cmd.get("description", ""),
                parameters=params,
                returns=returns,
                deprecated=cmd.get("deprecated", False),
                experimental=cmd.get("experimental", False),
            )
            self.commands[ci.full_name] = ci
            cmds_list.append(ci)

        for evt in domain.get("events", []):
            params = _parse_params(evt.get("parameters", []))
            ei = EventInfo(
                domain=name,
                name=evt["name"],
                full_name=f"{name}.{evt['name']}",
                description=evt.get("description", ""),
                parameters=params,
                deprecated=evt.get("deprecated", False),
                experimental=evt.get("experimental", False),
            )
            self.events[ei.full_name] = ei
            evts_list.append(ei)

        for typ in domain.get("types", []):
            ti = _parse_type(name, typ)
            self.types[ti.full_name] = ti
            typs_list.append(ti)

        di = DomainInfo(
            name=name,
            description=desc,
            version=self.protocol_version,
            commands=cmds_list,
            events=evts_list,
            types=typs_list,
        )
        self.domains[name] = di

    def _build_keyword_index(self) -> None:
        index: dict[str, set[str]] = {}
        for full_name, cmd in self.commands.items():
            for w in set(re.findall(r"[a-z]+", f"{cmd.domain} {cmd.name} {cmd.description}".lower())):
                if len(w) > 2:
                    index.setdefault(w, set()).add(full_name)
        for full_name, evt in self.events.items():
            for w in set(re.findall(r"[a-z]+", f"{evt.domain} {evt.name} {evt.description}".lower())):
                if len(w) > 2:
                    index.setdefault(w, set()).add(full_name)
        self._keyword_index = index

    # === Public Query API ===

    def list_domains(self) -> list[dict]:
        result = []
        for name, d in sorted(self.domains.items()):
            result.append({
                "domain": name,
                "description": d.description,
                "version": d.version,
                "deprecated": False,
                "command_count": len(d.commands),
                "event_count": len(d.events),
                "type_count": len(d.types),
            })
        return result

    def get_domain(self, domain: str) -> dict | None:
        d = self.domains.get(domain)
        if not d:
            return None
        return {
            "domain": d.name,
            "description": d.description,
            "version": d.version,
            "commands": [
                {"name": c.name, "description": c.description, "deprecated": c.deprecated}
                for c in d.commands
            ],
            "events": [
                {"name": e.name, "description": e.description, "deprecated": e.deprecated}
                for e in d.events
            ],
            "types": [
                {"name": t.name, "description": t.description}
                for t in d.types
            ],
        }

    def get_command(self, domain: str, command: str) -> dict | None:
        full = f"{domain}.{command}"
        c = self.commands.get(full)
        if not c:
            return None
        return {
            "name": c.name,
            "domain": c.domain,
            "full_name": c.full_name,
            "description": c.description,
            "deprecated": c.deprecated,
            "experimental": c.experimental,
            "parameters": [_param_to_dict(p) for p in c.parameters],
            "returns": [_param_to_dict(p) for p in c.returns],
        }

    def get_event(self, domain: str, event: str) -> dict | None:
        full = f"{domain}.{event}"
        e = self.events.get(full)
        if not e:
            return None
        return {
            "name": e.name,
            "domain": e.domain,
            "full_name": e.full_name,
            "description": e.description,
            "deprecated": e.deprecated,
            "experimental": e.experimental,
            "parameters": [_param_to_dict(p) for p in e.parameters],
        }

    def find_concept(self, query: str, max_results: int = 10) -> list[dict]:
        q = query.lower().strip()
        words = set(re.findall(r"[a-z]+", q))
        if not words:
            return []

        direct = self._keyword_index.get(q, set())
        scored: dict[str, tuple[int, str, str]] = {}

        for name in direct:
            entry = self.commands.get(name) or self.events.get(name)
            if entry:
                typ = "command" if name in self.commands else "event"
                scored[name] = (100, typ, entry.description)

        for w in words:
            for key, names in self._keyword_index.items():
                ratio = Levenshtein.ratio(w, key)
                if ratio >= 0.8:
                    for name in names:
                        entry = self.commands.get(name) or self.events.get(name)
                        if entry:
                            typ = "command" if name in self.commands else "event"
                            new_score = int(ratio * 50)
                            curr = scored.get(name)
                            if not curr or new_score > curr[0]:
                                scored[name] = (new_score, typ, entry.description)

        sorted_items = sorted(scored.items(), key=lambda x: -x[1][0])[:max_results]
        return [
            {
                "domain": name.split(".")[0],
                "type": typ,
                "name": name,
                "description": desc,
            }
            for name, (_, typ, desc) in sorted_items
        ]

    def validate_command(
        self, domain: str, command: str, parameters: dict[str, Any]
    ) -> dict:
        full = f"{domain}.{command}"
        cmd = self.commands.get(full)
        if not cmd:
            return {"valid": False, "errors": [{"message": f"Unknown command: {full}"}]}

        errors: list[dict] = []
        warnings: list[dict] = []
        param_names = {p.name for p in cmd.parameters}
        required = {p.name for p in cmd.parameters if not p.optional}

        for key in parameters:
            if key not in param_names:
                closest = min(param_names, key=lambda x, k=key: Levenshtein.distance(k, x)) if param_names else None
                if closest and Levenshtein.distance(key, closest) <= 3:
                    errors.append({
                        "param": key,
                        "message": f"Unknown parameter '{key}'. Did you mean '{closest}'?",
                    })
                else:
                    errors.append({
                        "param": key,
                        "message": f"Unknown parameter '{key}'",
                    })

        for req in required:
            if req not in parameters:
                errors.append({
                    "param": req,
                    "message": f"Missing required parameter '{req}'",
                })

        return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}

    def error_decode(self, error_code: int | None = None, error_message: str = "") -> dict:
        explanations = {
            -32601: {
                "name": "Method not found",
                "explanation": "The requested CDP method does not exist in this protocol version.",
            },
            -32602: {
                "name": "Invalid params",
                "explanation": "The parameters passed to the method are invalid.",
            },
            -32000: {
                "name": "Internal error",
                "explanation": "An internal error occurred while processing the request.",
            },
        }

        result = {
            "code": error_code,
            "name": "Unknown",
            "explanation": "",
            "common_causes": [],
            "solutions": [],
        }

        if error_code and error_code in explanations:
            info = explanations[error_code]
            result["name"] = info["name"]
            result["explanation"] = info["explanation"]
        elif error_message:
            result["name"] = error_message
            result["explanation"] = "See common causes below."

        if error_message and "." in error_message:
            parts = error_message.split("'")
            if len(parts) >= 3:
                method = parts[1]
                if "." in method:
                    d, c = method.split(".", 1)
                    alt = self._fuzzy_find_command(d, c)
                    if alt:
                        result["common_causes"].append(f"Typo in method name: '{method}' should be '{alt}'")
                        result["solutions"].append(f"Use {alt} instead of {method}")

        if "not found" in result["name"].lower():
            result["common_causes"].append("Method was removed in a newer protocol version")
            result["solutions"].append("Run kahin_list_domains to list available methods")

        if "invalid" in result["name"].lower():
            result["common_causes"].append("Parameter type mismatch or missing required field")
            result["solutions"].append("Use kahin_validate_command before sending")

        return result

    def get_dependencies(self, domain: str, command: str) -> dict:
        full = f"{domain}.{command}"
        cmd = self.commands.get(full)
        if not cmd:
            return {"prerequisites": [], "required_events": [], "common_pattern": ""}

        prereqs = []
        required_events = []

        if cmd.name in ("enable", "start", "capture"):
            prereqs.append({
                "domain": cmd.domain,
                "command": cmd.name,
                "reason": f"Must call {full} to activate the domain",
            })

        for p in cmd.parameters:
            for evt_name, evt in self.events.items():
                if evt.domain == cmd.domain and p.name in evt.name.lower():
                    required_events.append({
                        "domain": evt.domain,
                        "event": evt.name,
                        "reason": f"Emitted when {p.name} is used",
                    })

        return {
            "prerequisites": prereqs,
            "required_events": required_events,
            "common_pattern": "",
            "effects": [],
        }

    def _fuzzy_find_command(self, domain: str, name: str) -> str | None:
        d = self.domains.get(domain)
        if not d:
            return None
        candidates = [(c.name, Levenshtein.distance(name, c.name)) for c in d.commands]
        if not candidates:
            return None
        best = min(candidates, key=lambda x: x[1])
        return f"{domain}.{best[0]}" if best[1] <= 3 else None


def _parse_params(params: list[dict]) -> list[ParamInfo]:
    return [
        ParamInfo(
            name=p["name"],
            type=p.get("type", "any"),
            optional=p.get("optional", False),
            description=p.get("description", ""),
        )
        for p in params
    ]


def _parse_type(domain: str, typ: dict) -> TypeInfo:
    type_name = typ.get("id") or typ.get("name", "")
    return TypeInfo(
        domain=domain,
        name=type_name,
        full_name=f"{domain}.{type_name}",
        type=typ.get("type", "object"),
        description=typ.get("description", ""),
        enum_values=typ.get("enum"),
        properties=[
            TypeProperty(
                name=p["name"],
                type=p.get("type", "any"),
                optional=p.get("optional", False),
                description=p.get("description", ""),
                enum_values=p.get("enum"),
            )
            for p in typ.get("properties", [])
        ],
    )


def _param_to_dict(p: ParamInfo) -> dict:
    return {
        "name": p.name,
        "type": p.type,
        "optional": p.optional,
        "description": p.description,
    }
