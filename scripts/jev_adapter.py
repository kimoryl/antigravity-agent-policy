"""Optional loopback Jev judgments; never grants permissions or changes task state."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class JevUnavailable(RuntimeError):
    pass


def _endpoint() -> str:
    url = os.environ.get("CODEX_AGENT_POLICY_JEV_URL", "http://127.0.0.1:8765/v1/systemone")
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.path != "/v1/systemone" or parsed.username or parsed.password:
        raise JevUnavailable("Jev endpoint must be a local /v1/systemone URL")
    return url


def _ask(state: Any, questions: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({"state": state, "questions": questions, "model": "jev-latest"}, ensure_ascii=False).encode("utf-8")
    if len(body) > 12000:
        raise JevUnavailable("Jev request is too large")
    request = Request(_endpoint(), data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=8) as response:
            if response.headers.get("x-local-jev-truncated") == "true":
                raise JevUnavailable("Jev input was truncated")
            return json.load(response)
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as error:
        raise JevUnavailable(str(error)) from error


def rank_skills(goal: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Rerank only a small ambiguous candidate set; abstain on weak margins."""
    if not isinstance(goal, str) or not goal.strip() or not 2 <= len(candidates) <= 8:
        raise JevUnavailable("Rerank requires a goal and 2 to 8 candidates")
    state = {"query": goal[:800], "candidates": {f"c{i}": {"name": item["name"], "description": str(item.get("description", ""))[:350], "provides": item["provides"]} for i, item in enumerate(candidates)}}
    questions = {f"c{i}": {"type": "noul", "instructions": f"Does candidates.c{i} directly help with query? Judge this candidate on its own."} for i in range(len(candidates))}
    response = _ask(state, questions)
    answers = response.get("answers", {})
    scored = []
    for i, item in enumerate(candidates):
        answer = answers.get(f"c{i}", {})
        relevance = answer.get("noul") if answer.get("type") == "noul" else None
        if not isinstance(relevance, (int, float)):
            raise JevUnavailable("Jev returned an incomplete ranking")
        scored.append({"name": item["name"], "relevance": round(float(relevance), 3)})
    scored.sort(key=lambda item: -item["relevance"])
    margin = scored[0]["relevance"] - scored[1]["relevance"]
    chosen = scored[0]["name"] if scored[0]["relevance"] >= .65 and margin >= .15 else None
    return {"chosen": chosen, "ranked": scored, "margin": round(margin, 3), "model": response.get("model"), "usage": response.get("usage")}


def rank_context(query: str, candidates: list[dict[str, str]]) -> list[str]:
    """Return file paths ordered by Jev relevance; only short excerpts leave this process."""
    scored: list[tuple[str, float]] = []
    for offset in range(0, len(candidates), 4):
        batch = candidates[offset:offset + 4]
        state = {"query": query[:500], "candidates": {f"c{i}": {"path": item["path"], "excerpt": item["excerpt"][:220]} for i, item in enumerate(batch)}}
        questions = {f"c{i}": {"type": "noul", "instructions": f"Does candidates.c{i} directly help answer query?"} for i in range(len(batch))}
        answers = _ask(state, questions).get("answers", {})
        for i, item in enumerate(batch):
            answer = answers.get(f"c{i}", {})
            relevance = answer.get("noul") if answer.get("type") == "noul" else None
            if not isinstance(relevance, (int, float)):
                raise JevUnavailable("Jev returned incomplete context ranking")
            scored.append((item["path"], float(relevance)))
    scored.sort(key=lambda pair: -pair[1])
    return [path for path, _score in scored]


def check_verification(goal: str, evidence: str) -> dict[str, Any]:
    """A narrow semantic check; results are advisory, not final verification."""
    if not goal.strip() or not evidence.strip():
        raise JevUnavailable("Goal and evidence are required")
    response = _ask({"goal": goal[:500], "evidence": evidence[:1200]}, {
        "coverage": {"type": "noul", "instructions": "Does the evidence directly support that the goal is met? Answer no when the evidence is unrelated or contradicts the goal."}
    })
    answer = response.get("answers", {}).get("coverage", {})
    probability = answer.get("noul") if answer.get("type") == "noul" else None
    if not isinstance(probability, (int, float)):
        raise JevUnavailable("Jev returned an incomplete verification judgment")
    verdict = "supported" if probability >= .8 else "unsupported" if probability <= .2 else "uncertain"
    return {"verdict": verdict, "probability": round(float(probability), 3), "model": response.get("model")}


def suggest_task_profile(raw_task: str, capabilities: list[str]) -> dict[str, Any]:
    """Return low-risk suggestions. The Codex planner must validate the profile."""
    if not isinstance(raw_task, str) or not raw_task.strip():
        raise JevUnavailable("raw_task is required")
    task = raw_task[:1000]
    questions = {
        "task_type": {"type": "choice", "instructions": "Which kind of work best describes this task?", "criteria": {
            "implementation": "Write or change software", "debugging": "Diagnose or fix a defect", "review": "Inspect quality or correctness", "research": "Find and explain information", "documentation": "Write or revise documentation", "other": "None of these"}},
        "complexity": {"type": "choice", "instructions": "How complex is the requested work?", "criteria": {
            "simple": "One small, clear change", "moderate": "Several related steps", "complex": "Many dependent components or uncertain design"}},
    }
    response = _ask(task, questions)
    answers = response.get("answers", {})

    def choice(name: str) -> tuple[str, dict[str, Any]]:
        answer = answers.get(name, {})
        label = answer.get("choice")
        distribution = answer.get("probabilities", {})
        if not isinstance(label, str) or not isinstance(distribution, dict):
            return "uncertain", {"candidate": label, "probability": 0, "margin": 0}
        probability = distribution.get(label, 0)
        values = sorted((value for value in distribution.values() if isinstance(value, (int, float))), reverse=True)
        margin = values[0] - values[1] if len(values) > 1 else 0
        accepted = isinstance(probability, (int, float)) and probability >= .8 and margin >= .2
        return (label if accepted else "uncertain"), {"candidate": label, "probability": round(float(probability), 3), "margin": round(margin, 3)}

    task_type, task_judgment = choice("task_type")
    complexity, complexity_judgment = choice("complexity")
    matched: list[str] = []
    for offset in range(0, len(capabilities), 4):
        batch = capabilities[offset:offset + 4]
        cap_questions = {f"c{i}": {"type": "noul", "instructions": f"Does the task require capability {cap}?"} for i, cap in enumerate(batch)}
        try:
            cap_answers = _ask(task, cap_questions).get("answers", {})
        except JevUnavailable:
            break
        for i, cap in enumerate(batch):
            answer = cap_answers.get(f"c{i}", {})
            if answer.get("type") == "noul" and isinstance(answer.get("noul"), (int, float)) and answer["noul"] >= .75:
                matched.append(cap)
    return {"task_type": task_type, "complexity_hint": complexity, "required_capabilities": matched, "judgments": {"task_type": task_judgment, "complexity": complexity_judgment}, "source": "jev_suggestion", "requires_codex_review": True, "model": response.get("model")}
