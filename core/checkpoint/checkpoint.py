"""Checkpoint contract. The existing Runtime state store remains the source of truth."""

from __future__ import annotations

from typing import Any, Protocol


class CheckpointStore(Protocol):
    def save(self, task_id: str, reason: str) -> dict[str, Any]:
        ...

    def restore(self, task_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
        ...

