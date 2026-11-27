"""Parsing agent text into validated artifacts.

Small local models drift: they wrap JSON in prose, use a fence, omit the contract
field or return a string where a list belongs. We tolerate *representation* problems
and never tolerate *semantic* ones — a missing finding stays missing, and the engine
records `MALFORMED_AGENT_OUTPUT` rather than inventing content.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..schemas.agents import AgentArtifact

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


class MalformedOutput(ValueError):
    code = "MALFORMED_AGENT_OUTPUT"

    def __init__(self, message: str, *, raw_len: int = 0) -> None:
        super().__init__(message)
        self.raw_len = raw_len


def _balanced_object(text: str) -> str | None:
    """Return the first complete top-level JSON object, ignoring braces in strings."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def extract_json(text: str) -> dict[str, Any]:
    """Pull one JSON object out of possibly messy model text."""
    if not text or not text.strip():
        raise MalformedOutput("agent returned an empty response")
    candidates: list[str] = []
    for match in _FENCE.finditer(text):
        candidates.append(match.group(1).strip())
    candidates.append(text.strip())
    balanced = _balanced_object(text)
    if balanced:
        candidates.append(balanced)

    last_error: Exception | None = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(parsed, dict):
            return parsed
        last_error = MalformedOutput(f"expected a JSON object, got {type(parsed).__name__}")
    raise MalformedOutput(f"no JSON object found in agent response ({last_error})", raw_len=len(text))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [value]


def _assignments(value: Any) -> list[str]:
    """Department ids, whatever shape the model used for them.

    Measured on this machine: qwen3 returns ``assignments`` as a mapping (``{"creative_writing": "why"}``)
    rather than the list the contract asks for. Its keys are precisely the department ids the plan engine
    needs, so reading them is representation tolerance; stringifying the whole mapping - which used to
    happen - silently reduced a two-department plan to the fallback single department.
    """
    if isinstance(value, dict):
        return [str(key) for key in value]
    out: list[str] = []
    for entry in _as_list(value):
        if isinstance(entry, str):
            if entry.strip():
                out.append(entry.strip())
            continue
        if isinstance(entry, dict):
            for key in ("department", "department_id", "id", "name"):
                candidate = entry.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    out.append(candidate.strip())
                    break
            else:
                out.extend(str(key) for key in entry)
            continue
        out.append(str(entry))
    return out


def _aliased(value: Any, canonical: str, aliases: tuple[str, ...], fallback: str) -> dict[str, Any]:
    """Move a recognised alias onto the canonical field and drop the alias.

    Extra keys are forbidden by the contracts, so a mapped alias must not linger.
    """
    if isinstance(value, str):
        return {canonical: value}
    if isinstance(value, dict):
        data = dict(value)
        if not isinstance(data.get(canonical), str):
            for alias in aliases:
                candidate = data.get(alias)
                if isinstance(candidate, str) and candidate.strip():
                    data[canonical] = candidate
                    break
        for alias in aliases:
            data.pop(alias, None)
        if canonical not in data:
            return {canonical: fallback}
        # A present but wrongly-typed value is a real error; let validation reject it.
        return data
    return {canonical: str(value)}


STEP_KEYS = ("step", "owner", "depends_on")
STEP_DEPENDENCY_ALIASES = ("dependencies", "depends", "requires", "after")


def _finding(value: Any) -> dict[str, Any]:
    return _aliased(value, "statement", ("claim", "finding", "text", "detail"), "unspecified finding")


PLACEHOLDER_TEXT = "empty"
ITEM_NOISE_KEYS = ("confidence", "score", "warnings", "id", "index", "n")


def _item(value: Any) -> dict[str, Any]:
    """One copy item, keeping text the model wrote beside the caption instead of discarding it.

    Measured on this machine: qwen3:1.7b answers the copy contract with
    ``{"caption": "Wake up, breathe, thrive.", "hashtag": "#MorningMotivation"}``. Dropping ``hashtag``
    would silently lose the hashtag the objective asked for, and rejecting the item loses the caption
    with it, so the value is kept in ``notes`` under the model's own key name - preserved and
    attributed, never invented. Structural noise (a score, a confidence) is dropped rather than quoted.
    """
    data = _aliased(value, "text", ("content", "copy", "caption", "value", "body"), PLACEHOLDER_TEXT)
    extras: list[str] = []
    for key in list(data):
        if key in ("text", "label", "notes") or key in ITEM_NOISE_KEYS:
            if key in ITEM_NOISE_KEYS:
                data.pop(key)
            continue
        for text in _text_values(data.pop(key)):
            extras.append(f"{key}: {text}")
    if extras and not data.get("notes"):
        data["notes"] = "; ".join(extras)[:1000]
    return data


def _text_values(value: Any) -> list[str]:
    """Strings worth keeping from a model-supplied value; anything structural is ignored."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if isinstance(item, (str, int, float)) and str(item).strip()]
    return []


def _step(value: Any) -> dict[str, Any]:
    """A plan step, with the two things small models get wrong about *shape* handled.

    Measured on this machine: qwen3:4b reliably returns ``{"step": "1", "dependencies": []}`` for a plan.
    That is representation, not content - ``dependencies`` is this schema's ``depends_on`` under another
    name, and an unknown key carries nothing the engine reads (task dependencies are computed from the
    graph, never from an artifact). Dropping the extra key and renaming the alias keeps the plan; only a
    step with no text at all stays a real error.
    """
    data = _aliased(value, "step", ("action", "description", "title", "task"), "unspecified step")
    dependencies = next(
        (data[alias] for alias in STEP_DEPENDENCY_ALIASES if alias in data and not data.get("depends_on")),
        None,
    )
    cleaned = {key: data[key] for key in STEP_KEYS if key in data}
    if dependencies is not None and not cleaned.get("depends_on"):
        cleaned["depends_on"] = dependencies
    return cleaned


def _issue(value: Any) -> dict[str, Any]:
    return _aliased(value, "issue", ("problem", "message", "detail", "text"), "unspecified issue")


def _section(value: Any) -> dict[str, Any]:
    data = _aliased(value, "body", ("content", "text", "detail"), "no content")
    if not isinstance(data.get("heading"), str):
        for alias in ("title", "name"):
            candidate = data.get(alias)
            if isinstance(candidate, str) and candidate.strip():
                data["heading"] = candidate
                break
    for alias in ("title", "name"):
        data.pop(alias, None)
    data.setdefault("heading", "Result")
    return data


def _confidence(value: Any, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if 1.0 < number <= 100.0:
        number = number / 100.0
    return max(0.0, min(1.0, number))


def _verdict(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in {"pass", "passed", "approve", "approved", "ok", "accept"}:
        return "pass"
    if lowered in {"revise", "revision", "changes", "needs_work", "rework"}:
        return "revise"
    if lowered in {"reject", "rejected", "fail", "failed", "block"}:
        return "reject"
    return None


def coerce_artifact(data: dict[str, Any], contract: str) -> AgentArtifact:
    """Normalise representation noise, then validate strictly."""
    payload: dict[str, Any] = {
        "contract": contract,
        "summary": str(data.get("summary") or data.get("answer") or data.get("result") or "").strip(),
        "findings": [_finding(value) for value in _as_list(data.get("findings") or data.get("claims"))],
        "items": [_item(value) for value in _as_list(data.get("items") or data.get("variants") or data.get("copy"))],
        "steps": [_step(value) for value in _as_list(data.get("steps") or data.get("plan"))],
        "assignments": _assignments(data.get("assignments") or data.get("departments")),
        "issues": [_issue(value) for value in _as_list(data.get("issues") or data.get("problems"))],
        "sections": [_section(value) for value in _as_list(data.get("sections") or data.get("body"))],
        "verdict": _verdict(data.get("verdict")),
        "notes": data.get("notes") if isinstance(data.get("notes"), str) else None,
        "action": data.get("action") if isinstance(data.get("action"), str) else None,
        "target": data.get("target") if isinstance(data.get("target"), str) else None,
        "risks": [str(value) for value in _as_list(data.get("risks"))],
        "confidence": _confidence(data.get("confidence")),
        "warnings": [str(value) for value in _as_list(data.get("warnings"))],
    }
    if not payload["summary"]:
        payload["summary"] = _fallback_summary(payload, contract)
    if contract == "review" and payload["verdict"] is None:
        # A review without a verdict is not a review; infer only from an explicit issue list.
        severities = {str(issue.get("severity", "warning")) for issue in payload["issues"]}
        payload["verdict"] = "reject" if "critical" in severities else ("revise" if payload["issues"] else "pass")
    if contract == "copy" and not payload["items"] and payload["summary"]:
        payload["items"] = [{"text": payload["summary"], "label": "draft"}]
    if contract == "report" and not payload["sections"] and payload["summary"]:
        payload["sections"] = [{"heading": "Summary", "body": payload["summary"]}]
    if contract == "plan" and not payload["steps"] and payload["summary"]:
        raise MalformedOutput("plan output contained no steps")
    try:
        return AgentArtifact.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError and friends
        raise MalformedOutput(f"agent output failed schema validation: {exc}") from exc


def _fallback_summary(payload: dict[str, Any], contract: str) -> str:
    if payload["findings"]:
        return str(payload["findings"][0].get("statement", ""))[:400]
    if payload["items"]:
        return str(payload["items"][0].get("text", ""))[:400]
    if payload["steps"]:
        return str(payload["steps"][0].get("step", ""))[:400]
    if payload["sections"]:
        return str(payload["sections"][0].get("body", ""))[:400]
    if payload["issues"]:
        return str(payload["issues"][0].get("issue", ""))[:400]
    return f"{contract} output"


def parse_artifact(text: str, contract: str) -> AgentArtifact:
    return coerce_artifact(extract_json(text), contract)


#: Text fields an agent's prose lives in. Only these are scanned for numbers.
PROSE_FIELDS = ("summary", "findings", "sections", "items", "steps", "issues", "notes", "risks")


def artifact_prose(artifact: AgentArtifact) -> str:
    """Every human-readable string in an artifact, joined for the traceability check."""
    payload = artifact.model_dump()
    chunks: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            chunks.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    for field in PROSE_FIELDS:
        if field in payload:
            walk(payload[field])
    return "\n".join(chunks)


def check_artifact_numbers(artifact: AgentArtifact, evidence: list[dict[str, Any]]) -> str | None:
    """Return a warning when the artifact states a number no evidence item supports.

    Analytics tasks only: a task with no measured evidence is not checked, because there is nothing
    to trace to. The caller keeps the artifact, flags the task and never silently drops the claim.
    """
    if not evidence:
        return None
    from .analytics.evidence import UntraceableEvidence, assert_numbers_traceable

    try:
        assert_numbers_traceable(artifact_prose(artifact), evidence)
    except UntraceableEvidence as exc:
        return str(exc)
    return None


def repair_instruction(error: str, contract: str) -> str:
    """Second-chance instruction. States the failure; it does not reveal hidden content."""
    return (
        "Your previous answer could not be parsed.\n"
        f"Problem: {error[:400]}\n"
        f"Return ONLY one JSON object for a '{contract}' result. "
        "No prose before or after it, no code fence, no comments. "
        "Every field must be present and typed as instructed."
    )
