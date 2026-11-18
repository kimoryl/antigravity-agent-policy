"""Explicit module registry for optional Policy/Capability/Agent/Memory/Adapter modules."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from threading import RLock
from typing import Any


@dataclass
class ModuleInfo:
    name: str
    module_type: str
    version: str = "1.0.0"
    enabled: bool = True
    status: str = "loaded"
    metadata: dict[str, Any] = field(default_factory=dict)


class ModuleRegistry:
    def __init__(self) -> None:
        self._items: dict[str, ModuleInfo] = {}
        self._lock = RLock()

    def register(self, name: str, module_type: str, *, version: str = "1.0.0", metadata: dict[str, Any] | None = None) -> ModuleInfo:
        if not name or not module_type:
            raise ValueError("module name and type are required")
        info = ModuleInfo(name=name, module_type=module_type, version=version, metadata=dict(metadata or {}))
        with self._lock:
            self._items[name] = info
        return info

    def set_enabled(self, name: str, enabled: bool) -> ModuleInfo:
        with self._lock:
            info = self._items[name]
            info.enabled = bool(enabled)
            info.status = "loaded" if info.enabled else "disabled"
            return info

    def get(self, name: str) -> ModuleInfo | None:
        with self._lock:
            return self._items.get(name)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [asdict(item) for item in sorted(self._items.values(), key=lambda item: item.name)]


_REGISTRY = ModuleRegistry()


def default_registry() -> ModuleRegistry:
    for name, module_type in (
        ("policy", "policy"),
        ("capability_pool", "capability"),
        ("agent_registry", "agent"),
        ("memory", "memory"),
        ("codex", "adapter"),
    ):
        if _REGISTRY.get(name) is None:
            _REGISTRY.register(name, module_type)
    return _REGISTRY


def registry_snapshot() -> list[dict[str, Any]]:
    return default_registry().snapshot()

