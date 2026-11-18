"""Small Runtime boundary that keeps orchestration independent of modules."""

from __future__ import annotations

from typing import Any, Protocol


class Runtime(Protocol):
    """The minimum contract implemented by a host Runtime."""

    def execute(self, task: dict[str, Any]) -> dict[str, Any]:
        ...

    def emit(self, event: dict[str, Any]) -> None:
        ...

    def checkpoint(self, reason: str = "manual") -> dict[str, Any]:
        ...

