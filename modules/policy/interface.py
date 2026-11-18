"""Policy module interface; host permissions remain controlled by Codex."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class PolicyContext:
    request: dict[str, Any]
    task: dict[str, Any] = field(default_factory=dict)
    agent: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


class Policy(Protocol):
    name: str
    version: str

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        ...

    def decide(self, context: PolicyContext) -> PolicyDecision:
        ...

