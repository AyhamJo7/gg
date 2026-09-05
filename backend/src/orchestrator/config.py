"""Configuration loading: YAML file + settings table overrides."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "orchestrator.yaml"


class Config:
    def __init__(self, data: dict[str, Any]):
        self._data = data

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        p = path or DEFAULT_CONFIG_PATH
        data: dict[str, Any] = yaml.safe_load(p.read_text()) if p.exists() else {}
        return cls(data)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def priority_for(self, role: str) -> list[str]:
        value = self.get(f"priority.{role}", [])
        return list(value) if isinstance(value, list) else []

    def set_priority(self, role: str, providers: list[str]) -> None:
        self._data.setdefault("priority", {})[role] = providers

    def provider_enabled(self, name: str) -> bool:
        return bool(self.get(f"providers.{name}.enabled", False))

    def provider_timeout_s(self, name: str) -> float:
        return float(self.get(f"providers.{name}.timeout_minutes", 60)) * 60.0

    @property
    def raw(self) -> dict[str, Any]:
        return self._data
