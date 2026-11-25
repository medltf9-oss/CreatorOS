"""Typed contracts for agent communication.

Agents never exchange free-form chat. A unit of work is an `AgentTaskPayload`, its
output is an `AgentResult` whose `output` must satisfy one of the catalogue's
output contracts, and every hop (delegation, review, final answer) has a model
here. Anything that fails validation is rejected before it can be stored.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------- enums
ResultStatus = Literal["succeeded", "failed", "revise", "blocked", "cancelled"]


class Finding(BaseModel):
    """One claim an agent makes, with its own confidence and provenance."""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=3, max_length=2000)
    kind: Literal["fact", "assumption", "speculation"] = "assumption"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source: str | None = Field(default=None, max_length=500)


class ArtifactItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="", max_length=120)
    text: str = Field(min_length=1, max_length=4000)
    notes: str | None = Field(default=None, max_length=1000)


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # One character is enough: a model that labels its steps "1", "2", "3" has produced a usable plan, and
    # refusing an entire objective because a label is short is a worse outcome than showing the label.
    step: str = Field(min_length=1, max_length=500)
    owner: str | None = Field(default=None, max_length=120)
    depends_on: list[str] = Field(default_factory=list, max_length=10)


class ReportSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heading: str = Field(min_length=2, max_length=200)
    body: str = Field(min_length=1, max_length=6000)


class ReviewIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["info", "warning", "error", "critical"] = "warning"
    issue: str = Field(min_length=3, max_length=1000)
    fix: str | None = Field(default=None, max_length=1000)


# ------------------------------------------------------------ agent payloads
class AgentTaskPayload(BaseModel):
    """What a delegated unit of work contains. Produced by the engine, not by a model."""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=8000)
    department: str | None = Field(default=None, max_length=40)
    intent: str = Field(default="general", max_length=60)
    brief: str = Field(default="", max_length=4000)
    context: dict[str, Any] = Field(default_factory=dict)
    upstream: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    allowed_tools: list[str] = Field(default_factory=list, max_length=20)


class AgentArtifact(BaseModel):
    """Structured agent output. One shape per output contract, validated on arrival."""

    model_config = ConfigDict(extra="forbid")

    contract: Literal["analysis", "copy", "plan", "review", "report", "approval"]
    summary: str = Field(min_length=1, max_length=4000)
    findings: list[Finding] = Field(default_factory=list, max_length=20)
    items: list[ArtifactItem] = Field(default_factory=list, max_length=20)
    steps: list[PlanStep] = Field(default_factory=list, max_length=20)
    assignments: list[str] = Field(default_factory=list, max_length=10)
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=20)
    sections: list[ReportSection] = Field(default_factory=list, max_length=12)
    verdict: Literal["pass", "revise", "reject"] | None = None
    notes: str | None = Field(default=None, max_length=2000)
    action: str | None = Field(default=None, max_length=120)
    target: str | None = Field(default=None, max_length=200)
    risks: list[str] = Field(default_factory=list, max_length=10)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _required_for_contract(self) -> AgentArtifact:
        if self.contract == "analysis" and not self.findings:
            raise ValueError("analysis output must include at least one finding")
        if self.contract == "copy" and not self.items:
            raise ValueError("copy output must include at least one item")
        if self.contract == "plan" and not self.steps:
            raise ValueError("plan output must include at least one step")
        if self.contract == "review" and (self.verdict is None or not self.issues):
            raise ValueError("review output must include a verdict and issues")
        if self.contract == "report" and not self.sections:
            raise ValueError("report output must include at least one section")
        if self.contract == "approval" and (not self.action or not self.target):
            raise ValueError("approval output must include action and target")
        return self


class AgentResult(BaseModel):
    """Stored result of one agent task."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    task_id: str
    status: ResultStatus
    summary: str = Field(default="", max_length=4000)
    output: AgentArtifact | None = None
    evidence: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list, max_length=20)
    errors: list[str] = Field(default_factory=list, max_length=20)
    model_tag: str | None = None
    profile: str | None = None
    duration_ms: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    config_version: int = 1
    contribution: float = Field(default=0.0, ge=0.0, le=1.0)


class AgentFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    task_id: str
    code: str = Field(max_length=60)
    message: str = Field(max_length=2000)
    retryable: bool = False
    attempts: int = Field(default=0, ge=0)
    detail: str | None = Field(default=None, max_length=4000)


class DelegationRequest(BaseModel):
    """Chief → department leader, or leader → specialist."""

    model_config = ConfigDict(extra="forbid")

    parent_task_id: str
    agent_id: str
    department: str | None = None
    task_type: str = Field(default="general", max_length=60)
    title: str = Field(default="", max_length=300)
    priority: int = Field(default=5, ge=1, le=10)
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    payload: AgentTaskPayload


class DelegationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    created_task_ids: list[str] = Field(default_factory=list)
    rejected: list[dict[str, str]] = Field(default_factory=list)
    depth: int = 0


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    reviewer_agent_id: str
    subject_task_ids: list[str] = Field(min_length=1, max_length=20)
    criteria: list[str] = Field(default_factory=list, max_length=10)


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer_agent_id: str
    task_id: str
    verdict: Literal["pass", "revise", "reject"]
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=20)
    notes: str | None = Field(default=None, max_length=2000)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class FinalObjectiveResult(BaseModel):
    """What the chief returns to the user. Everything here is derived from stored rows."""

    model_config = ConfigDict(extra="forbid")

    objective_id: str
    state: str
    summary: str
    deliverable: str = ""
    sections: list[ReportSection] = Field(default_factory=list)
    department_results: list[dict[str, Any]] = Field(default_factory=list)
    quality_verdict: Literal["pass", "revise", "reject", "not_run"] = "not_run"
    calls_used: int = 0
    tokens_used: int = 0
    duration_ms: int = 0
    warnings: list[str] = Field(default_factory=list)
    unfinished: list[str] = Field(default_factory=list)
    generated_at: datetime | None = None


# --------------------------------------------------------------- chat helper
class ChatTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=32000)

    @field_validator("content")
    @classmethod
    def _no_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("content must not contain NUL bytes")
        return value
