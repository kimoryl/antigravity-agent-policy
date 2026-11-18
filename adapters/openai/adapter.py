"""OpenAI adapter boundary; transport remains supplied by the host application."""

from __future__ import annotations

from typing import Any


class OpenAIAdapter:
    name = "openai"
    version = "1.0.0"

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"status": "delegated", "adapter": self.name, "request": request}

