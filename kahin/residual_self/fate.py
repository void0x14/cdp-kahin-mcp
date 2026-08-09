"""residual_self/fate.py — Pattern DB (Kader)."""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / ".fate_db.json"  # legacy path, retained for explicit callers


def _default_db_path() -> Path:
    configured = os.environ.get("KAHIN_FATE_PATH")
    if configured:
        return Path(configured).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kahin" / "fate_db.json"


class FateDB:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path).expanduser() if path else _default_db_path()
        self._patterns: list[dict[str, Any]] = []
        self._load()

    def clear(self) -> None:
        self._patterns = []
        self._save()

    def _load(self) -> None:
        if self._path.exists():
            try:
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                self._patterns = loaded if isinstance(loaded, list) else []
            except (json.JSONDecodeError, OSError):
                self._patterns = []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(self._path.name + ".tmp")
        temporary.write_text(json.dumps(self._patterns, indent=2), encoding="utf-8")
        temporary.replace(self._path)

    def learn(self, domain: str, command: str, params: dict[str, Any], context: str = "") -> None:
        for p in self._patterns:
            if p["domain"] == domain and p["command"] == command:
                p["frequency"] += 1
                p["last_used"] = time.time()
                if context and context not in p["contexts"]:
                    p["contexts"].append(context)
                self._save()
                return
        self._patterns.append({
            "domain": domain,
            "command": command,
            "params_pattern": list(params.keys()),
            "contexts": [context] if context else [],
            "frequency": 1,
            "last_used": time.time(),
        })
        self._save()

    def query(self, domain: str | None = None, context: str = "", limit: int = 10) -> list[dict[str, Any]]:
        results = self._patterns
        if domain:
            results = [p for p in results if p["domain"] == domain]
        if context:
            results = [p for p in results if context in p["contexts"]]
        results.sort(key=lambda p: (-p["frequency"], -p["last_used"]))
        return results[:limit]

    def suggest(self, partial: str, limit: int = 5) -> list[dict[str, Any]]:
        matches = []
        for p in self._patterns:
            full = f"{p['domain']}.{p['command']}"
            if partial.lower() in full.lower():
                matches.append({"full_name": full, "frequency": p["frequency"], "domain": p["domain"], "command": p["command"]})
        matches.sort(key=lambda x: -x["frequency"])
        return matches[:limit]

    def forget(self, domain: str, command: str) -> bool:
        for i, p in enumerate(self._patterns):
            if p["domain"] == domain and p["command"] == command:
                self._patterns.pop(i)
                self._save()
                return True
        return False

    def prune(self, valid_commands: set[str]) -> int:
        """Remove records that do not name a command in the loaded schema."""
        before = len(self._patterns)
        self._patterns = [
            pattern for pattern in self._patterns
            if f"{pattern.get('domain')}.{pattern.get('command')}" in valid_commands
        ]
        removed = before - len(self._patterns)
        if removed:
            self._save()
        return removed

    def stats(self) -> dict[str, Any]:
        if not self._patterns:
            return {"total": 0, "domains": 0, "top": []}
        domain_counts = Counter(p["domain"] for p in self._patterns)
        top = sorted(self._patterns, key=lambda p: -p["frequency"])[:5]
        return {
            "total": len(self._patterns),
            "domains": len(domain_counts),
            "top": [f"{p['domain']}.{p['command']} (x{p['frequency']})" for p in top],
        }
