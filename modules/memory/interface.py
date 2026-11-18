"""Unified memory contract; SQLite/Hindsight adapters implement it."""

from __future__ import annotations

from typing import Any, Protocol


class Memory(Protocol):
    name: str

    def store(self, data: dict[str, Any]) -> dict[str, Any]:
        ...

    def retrieve(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        ...

