"""residual_self/fate.py — Pattern DB (Kader)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / ".fate_db.json"


class FateDB:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else DB_PATH
        self._patterns: list[dict[str, Any]] = []
        self._load()

    def clear(self) -> None:
        self._patterns = []
        self._save()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._patterns = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError):
                self._patterns = []

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._patterns, indent=2))

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

    def suggest(self, partial: str) -> list[str]:
        matches = []
        for p in self._patterns:
            full = f"{p['domain']}.{p['command']}"
            if partial.lower() in full.lower():
                matches.append(full)
        return sorted(set(matches))[:5]
