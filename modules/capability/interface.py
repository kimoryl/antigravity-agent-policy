"""Capability contract shared by Skill, MCP, Tool and external providers."""

from __future__ import annotations

from typing import Any, Protocol


class Capability(Protocol):
    name: str
    type: str
    version: str

    def health(self) -> dict[str, Any]:
        ...

    def invoke(self, request: dict[str, Any]) -> dict[str, Any]:
        ...

