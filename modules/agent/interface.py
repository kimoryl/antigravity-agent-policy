"""Agent profile boundary for registered roles and lifecycle metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class AgentProfile:
    name: str
    capabilities: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


class Agent(Protocol):
    profile: AgentProfile

    def run(self, task: dict[str, Any]) -> dict[str, Any]:
        ...

