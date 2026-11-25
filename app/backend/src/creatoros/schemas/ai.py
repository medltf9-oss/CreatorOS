"""Schemas for the AI infrastructure API (Phase 3)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Complexity = Literal["low", "medium", "high"]


class ModelPullRequest(BaseModel):
    model: str = Field(min_length=1, max_length=120)


class PullStatus(BaseModel):
    pull_id: str
    model: str
    state: str
    status: str
    completed: int
    total: int
    percent: float
    error: str | None = None
    cancel_requested: bool = False


class ModelCard(BaseModel):
    tag: str
    size_bytes: int
    size_gb: float
    digest: str
    modified_at: str | None
    family: str | None
    parameter_size: str | None
    quantization: str | None
    capabilities: list[str] = Field(default_factory=list)
    profile: str | None = None
    role: str | None = None
    loaded: bool = False
    vram_gb: float = 0.0
    vram_percent: float = 0.0
    context_length: int | None = None
    context_source: str | None = Field(
        default=None, description="'loaded' when read from the running model, 'profile' when taken from its profile"
    )
    benchmark_tps: float | None = None
    benchmark_at: str | None = None
    benchmark_source: str | None = Field(
        default=None, description="'measured' on this machine, or 'reference' from ADR-09"
    )


class ModelListResponse(BaseModel):
    items: list[ModelCard]
    total: int
    total_size_gb: float
    loaded_count: int
    ollama_online: bool


class ModelDetail(BaseModel):
    tag: str
    family: str | None = None
    families: list[str] = Field(default_factory=list)
    parameter_size: str | None = None
    parameter_count: int | None = None
    quantization: str | None = None
    format: str | None = None
    architecture: str | None = None
    context_length: int | None = None
    embedding_length: int | None = None
    block_count: int | None = None
    capabilities: list[str] = Field(default_factory=list)
    default_parameters: str | None = None
    modified_at: str | None = None
    license_present: bool = False
    profile: str | None = None
    benchmark_tps: float | None = None
    benchmark_source: str | None = None
    loaded: bool = False
    vram_gb: float = 0.0
    vram_percent: float = 0.0


class BenchmarkRequest(BaseModel):
    prompt: str = Field(
        default="Explain in one sentence why a video publishing tool needs a content calendar.",
        max_length=2000,
    )
    max_tokens: int = Field(default=48, ge=8, le=512)
    context_len: int = Field(default=4096, ge=512, le=32768)
    keep_alive: str | int | None = None


class BenchmarkResult(BaseModel):
    model: str
    profile: str | None
    tokens_per_sec: float
    completion_tokens: int
    prompt_tokens: int
    ttft_ms: int | None
    eval_duration_ms: int
    total_duration_ms: int
    context_len: int
    sample: str


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"] = "user"
    content: str = Field(min_length=1, max_length=32000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=50)
    profile: str | None = None
    complexity: Complexity = "medium"
    options: dict[str, Any] | None = None
    context_len: int | None = Field(default=None, ge=512, le=32768)
    wait_s: float = Field(default=60.0, ge=0, le=600)
    think: bool | None = None


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=32000)
    system: str | None = Field(default=None, max_length=8000)
    profile: str | None = None
    complexity: Complexity = "medium"
    options: dict[str, Any] | None = None
    context_len: int | None = Field(default=None, ge=512, le=32768)
    wait_s: float = Field(default=60.0, ge=0, le=600)
    priority: int = Field(default=50, ge=1, le=100)
    idempotency_key: str | None = Field(default=None, max_length=64)
    think: bool | None = None


class LimitsModel(BaseModel):
    max_concurrent_generations: int = Field(default=1, ge=1, le=4)
    max_ram_percent: float = Field(default=85.0, ge=50, le=98)
    min_free_ram_gb: float = Field(default=2.0, ge=0.5, le=32)
    max_vram_percent: float = Field(default=92.0, ge=50, le=100)
    max_queue_depth: int = Field(default=25, ge=1, le=500)
    generation_timeout_s: float = Field(default=300.0, ge=10, le=1800)
    default_profile: str = Field(default="specialist", max_length=60)
    allow_concurrent_models: bool = False


class ProfileModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    ollama_tag: str
    context_len: int
    temperature: float
    roles_json: list[str] = Field(default_factory=list)
    fallback_profile: str | None = None
    enabled: bool = True


class ProfileUpdate(BaseModel):
    ollama_tag: str | None = Field(default=None, max_length=120)
    context_len: int | None = Field(default=None, ge=512, le=131072)
    temperature: float | None = Field(default=None, ge=0, le=2)


class AiExecutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    model_tag: str
    profile_name: str | None
    state: str
    origin: str
    prompt_text: str | None
    response_text: str | None
    prompt_tokens: int
    completion_tokens: int
    ttft_ms: int | None
    duration_ms: int | None
    tokens_per_sec: float | None
    context_len: int | None
    error_code: str | None
    error_detail: str | None
    queue_job_id: str | None
    created_at: datetime
    finished_at: datetime | None
