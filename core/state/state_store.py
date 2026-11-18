"""State storage contract. Concrete SQLite/Hindsight implementations stay outside Core."""

from __future__ import annotations

from typing import Any, Protocol


class StateStore(Protocol):
    def get(self, key: str, default: Any = None) -> Any:
        ...

    def put(self, key: str, value: Any) -> None:
        ...

