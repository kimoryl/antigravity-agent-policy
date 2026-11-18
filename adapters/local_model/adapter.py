"""Local model adapter boundary without starting or installing a provider."""

from __future__ import annotations

from typing import Any


class LocalModelAdapter:
    name = "local_model"
    version = "1.0.0"

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"status": "delegated", "adapter": self.name, "request": request}

