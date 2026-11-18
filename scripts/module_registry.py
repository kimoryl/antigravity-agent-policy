"""Compatibility import for the stable module registry package."""

import sys
from pathlib import Path


_PACKAGE_ROOT = str(Path(__file__).resolve().parents[1])
if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)

from registry.module_registry import ModuleInfo, ModuleRegistry, default_registry, registry_snapshot

__all__ = ["ModuleInfo", "ModuleRegistry", "default_registry", "registry_snapshot"]
