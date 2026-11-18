from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from adapters.codex.adapter import CodexAdapter  # noqa: E402
from core.event.event_bus import Event, EventBus  # noqa: E402
from modules.agent.interface import AgentProfile  # noqa: E402
from modules.policy.interface import PolicyContext, PolicyDecision  # noqa: E402
from registry.module_registry import ModuleRegistry, default_registry  # noqa: E402


class ModularRuntimeTests(unittest.TestCase):
    def test_default_registry_exposes_replaceable_module_boundaries(self) -> None:
        names = {item["name"] for item in default_registry().snapshot()}
        self.assertTrue({"policy", "capability_pool", "agent_registry", "memory", "codex"} <= names)

    def test_registry_supports_dynamic_enable_disable_and_metadata(self) -> None:
        registry = ModuleRegistry()
        registered = registry.register("local-model", "adapter", version="2.1.0", metadata={"kind": "inference"})
        self.assertEqual(registered.version, "2.1.0")
        self.assertEqual(registry.get("local-model").metadata["kind"], "inference")
        registry.set_enabled("local-model", False)
        self.assertEqual(registry.snapshot()[0]["status"], "disabled")

    def test_event_bus_and_contract_values_are_small_host_boundaries(self) -> None:
        seen: list[str] = []
        bus = EventBus()
        bus.subscribe("capability.loaded", lambda event: seen.append(event.payload["name"]))
        bus.emit(Event("capability.loaded", {"name": "github_reader"}))
        self.assertEqual(seen, ["github_reader"])
        self.assertEqual(PolicyDecision(True, "native host permission").allowed, True)
        self.assertEqual(PolicyContext({"tool": "read"}).request["tool"], "read")
        self.assertEqual(AgentProfile("coder", ("code_generation",)).capabilities, ("code_generation",))

    def test_codex_adapter_keeps_permission_execution_in_the_host(self) -> None:
        result = CodexAdapter().execute({"tool": "Bash"})
        self.assertEqual(result["status"], "delegated")
        self.assertEqual(result["adapter"], "codex")


if __name__ == "__main__":
    unittest.main()
