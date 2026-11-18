"""External integration adapter boundary with explicit caller-owned transport."""

from __future__ import annotations

from typing import Any


class ExternalAdapter:
    name = "external"
    version = "1.0.0"

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"status": "delegated", "adapter": self.name, "request": request}

