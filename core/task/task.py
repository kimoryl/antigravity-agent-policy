"""Task lifecycle contract used by adapters and modules."""

from __future__ import annotations

from typing import Any, Protocol


class Task(Protocol):
    task_id: str
    project_id: str
    state: dict[str, Any]

