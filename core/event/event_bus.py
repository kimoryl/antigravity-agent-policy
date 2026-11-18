"""In-process event boundary for Runtime and optional modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any, Callable


@dataclass(frozen=True)
class Event:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time)


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[[Event], None]]] = {}

    def subscribe(self, name: str, handler: Callable[[Event], None]) -> None:
        self._handlers.setdefault(name, []).append(handler)

    def emit(self, event: Event) -> None:
        for handler in tuple(self._handlers.get(event.name, ())) + tuple(self._handlers.get("*", ())):
            handler(event)

