"""Codex host adapter. Permission prompts stay in Codex's native flow."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# The adapter is also importable directly by the small test harness, so keep
# the policy module lookup independent of the plugin's working directory.
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from policy_engine import MODEL_CAPACITY_FAILURES, PolicyError, load_policy, resolve  # noqa: E402


class CodexAdapter:
    name = "codex"
    version = "1.0.0"

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"status": "delegated", "adapter": self.name, "request": request}

    def execute_with_policy(
        self,
        request: dict[str, Any],
        *,
        policy: dict[str, Any] | None = None,
        runner: Any | None = None,
        audit: Any | None = None,
    ) -> dict[str, Any]:
        """Run a bounded, policy-selected request through an injected host runner.

        Codex remains the authority that supplies the real host runner.  The
        injected callable makes the selection, fallback and audit contract
        testable without pretending this repository can execute a Codex model.
        The legacy ``execute`` method is intentionally unchanged.
        """
        if not isinstance(request, dict):
            raise TypeError("request must be an object")
        if audit is not None and not callable(audit):
            raise TypeError("audit must be callable")
        if runner is None:
            return {
                "status": "stopped",
                "reason": "host_runner_required",
                "selected_model": None,
                "executed_model": None,
                "fallback_count": 0,
                "attempts": [],
            }
        active_policy = policy or load_policy()
        retry_limit = int(active_policy["escalation"]["retry_limit"])
        retries = 0
        failure_kind: str | None = None
        previous_model: str | None = None
        fallback_count = 0
        attempts: list[dict[str, Any]] = []
        selected_model: str | None = None
        attempted_models: list[str] = []

        while True:
            decision_request = dict(request)
            decision_request["retries"] = retries
            if failure_kind is None:
                decision_request.pop("failure_kind", None)
            else:
                decision_request["failure_kind"] = failure_kind
            decision_request["attempted_models"] = list(attempted_models)
            try:
                decision = resolve(decision_request, active_policy)
            except PolicyError as exc:
                reason = "fallback_unavailable" if failure_kind in MODEL_CAPACITY_FAILURES else "policy_error"
                return self._stopped(reason, selected_model, fallback_count, attempts, retries=retries, error=str(exc))
            if decision.get("status") != "ready":
                return self._stopped(
                    str(decision.get("reason", "policy_stop")), selected_model, fallback_count,
                    attempts, retries=retries, retry_later=decision.get("retry_later", False),
                    failure_kind=decision.get("failure_kind"), attempted_models=decision.get("attempted_models", attempted_models),
                )

            selected_model = decision.get("model_override")
            if selected_model is None and decision_request.get("user_model") is not None:
                selected_model = decision_request["user_model"]
            if previous_model is not None and selected_model != previous_model:
                fallback_count += 1
            previous_model = selected_model
            host_request = {
                **request,
                "reasoning_effort": decision.get("reasoning_effort"),
                "context_budget_tokens": decision.get("context_budget_tokens"),
            }
            if selected_model is None:
                host_request.pop("model", None)
            else:
                host_request["model"] = selected_model
            try:
                response = runner(host_request)
            except Exception as exc:  # pragma: no cover - defensive host boundary
                attempt = {"selected_model": selected_model, "executed_model": None, "status": "error", "failure_kind": "host_error"}
                attempts.append(attempt)
                self._emit_audit(audit, attempt)
                return self._stopped("host_error", selected_model, fallback_count, attempts, retries=retries, error=str(exc))
            if not isinstance(response, dict):
                attempt = {"selected_model": selected_model, "executed_model": None, "status": "error", "failure_kind": "host_invalid_response"}
                attempts.append(attempt)
                self._emit_audit(audit, attempt)
                return self._stopped("host_invalid_response", selected_model, fallback_count, attempts, retries=retries)

            response_status = response.get("status")
            executed_model = response.get("model")
            if response_status == "ok":
                if not isinstance(executed_model, str) or not executed_model:
                    attempt = {"selected_model": selected_model, "executed_model": None, "status": "error", "failure_kind": "host_model_unreported"}
                    attempts.append(attempt)
                    self._emit_audit(audit, attempt)
                    return self._stopped("host_model_unreported", selected_model, fallback_count, attempts, retries=retries)
                if selected_model is not None and executed_model != selected_model:
                    attempt = {"selected_model": selected_model, "executed_model": executed_model, "status": "error", "failure_kind": "host_model_mismatch"}
                    attempts.append(attempt)
                    self._emit_audit(audit, attempt)
                    return self._stopped("host_model_mismatch", selected_model, fallback_count, attempts, retries=retries)
                attempt = {"selected_model": selected_model, "executed_model": executed_model, "status": "ok"}
                attempts.append(attempt)
                self._emit_audit(audit, attempt)
                return {
                    "status": "completed",
                    "selected_model": selected_model,
                    "executed_model": executed_model,
                    "fallback_count": fallback_count,
                    "retries": retries,
                    "attempts": attempts,
                    "response": response,
                }

            failure_kind = response.get("failure_kind")
            attempt = {"selected_model": selected_model, "executed_model": None, "status": "error", "failure_kind": failure_kind}
            attempts.append(attempt)
            self._emit_audit(audit, attempt)
            if failure_kind not in MODEL_CAPACITY_FAILURES:
                return self._stopped(str(failure_kind or "host_execution_failed"), selected_model, fallback_count, attempts, retries=retries)
            if isinstance(selected_model, str) and selected_model not in attempted_models:
                attempted_models.append(selected_model)
            if retries >= retry_limit:
                return self._stopped("retry_limit_exceeded", selected_model, fallback_count, attempts, retries=retries)
            retries += 1

    @staticmethod
    def _emit_audit(audit: Any | None, attempt: dict[str, Any]) -> None:
        if audit is not None:
            audit(dict(attempt))

    @staticmethod
    def _stopped(reason: str, selected_model: str | None, fallback_count: int, attempts: list[dict[str, Any]], retries: int | None = None, **extra: Any) -> dict[str, Any]:
        return {
            "status": "stopped",
            "reason": reason,
            "selected_model": selected_model,
            "executed_model": None,
            "fallback_count": fallback_count,
            "retries": max(0, retries if retries is not None else len(attempts) - 1),
            "attempts": attempts,
            **extra,
        }

