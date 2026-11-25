"""Test doubles for the Ollama gateway.

`FakeGateway` mirrors the async surface of `OllamaGateway` so API and service
tests can run without a model runtime. It never pretends to be real: it is only
constructible inside tests, and live checks live in test_ai_integration.py.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from creatoros.integrations.ollama.client import (
    GenerationMetrics,
    GenerationResult,
    ModelSummary,
    RunningModel,
)
from creatoros.integrations.ollama.errors import ModelNotFound, OllamaUnavailable


@dataclass
class FakeGateway:
    """In-memory stand-in for the Ollama HTTP API."""

    online: bool = True
    base_url: str = "http://fake-ollama:11434"
    models: list[ModelSummary] = field(default_factory=list)
    running: list[RunningModel] = field(default_factory=list)
    response_text: str = "fake answer"
    completion_tokens: int = 12
    fail_with: Exception | None = None
    stream_tokens: list[str] = field(default_factory=lambda: ["one ", "two ", "three"])
    stream_delay: float = 0.0
    pulls: list[dict[str, Any]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unloaded: list[str] = field(default_factory=list)
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.models:
            self.models = [
                ModelSummary(
                    tag="qwen3:0.6b", size_bytes=522_000_000, digest="a" * 64,
                    modified_at="2026-11-22T00:00:00Z", family="qwen3",
                    parameter_size="751.63M", quantization="Q4_K_M", format="gguf",
                    capabilities=["completion"],
                ),
                ModelSummary(
                    tag="qwen3:1.7b", size_bytes=1_400_000_000, digest="b" * 64,
                    modified_at="2026-11-22T00:00:00Z", family="qwen3",
                    parameter_size="2.0B", quantization="Q4_K_M", format="gguf",
                    capabilities=["completion", "tools"],
                ),
            ]

    async def ensure_exclusive(self, model: str, *, wait_s: float = 90.0) -> dict[str, Any]:
        """Mirror the real gateway's exclusive-model policy."""
        others = [entry.tag for entry in self.running if entry.tag != model]
        self.unloaded.extend(others)
        self.running = [entry for entry in self.running if entry.tag == model]
        return {"model": model, "unloaded": others, "waited_ms": 0}

    # ------------------------------------------------------------------ health
    async def health(self) -> dict[str, Any]:
        if not self.online:
            raise OllamaUnavailable("ollama is not reachable on the configured address")
        return {"online": True, "version": "test", "base_url": "http://fake", "latency_ms": 1.0}

    async def is_available(self) -> bool:
        return self.online

    async def aclose(self) -> None:
        return None

    # ------------------------------------------------------------------ models
    async def list_models(self) -> list[ModelSummary]:
        if not self.online:
            raise OllamaUnavailable("offline")
        return list(self.models)

    async def installed_tags(self) -> set[str]:
        return {m.tag for m in await self.list_models()}

    async def show_model(self, tag: str) -> dict[str, Any]:
        self.calls.append(("show", tag))
        if tag not in {m.tag for m in self.models}:
            raise ModelNotFound(f"{tag} is not installed")
        return {
            "tag": tag,
            "family": "qwen3",
            "families": ["qwen3"],
            "parameter_size": "2.0B",
            "parameter_count": 2_031_739_904,
            "quantization": "Q4_K_M",
            "format": "gguf",
            "architecture": "qwen3",
            "context_length": 40960,
            "embedding_length": 2048,
            "block_count": 28,
            "capabilities": ["completion"],
            "default_parameters": None,
            "modified_at": "2026-11-22T00:00:00Z",
            "license_present": True,
        }

    async def running_models(self) -> list[RunningModel]:
        if not self.online:
            raise OllamaUnavailable("offline")
        return list(self.running)

    async def delete_model(self, tag: str) -> None:
        if tag not in {m.tag for m in self.models}:
            raise ModelNotFound(f"{tag} is not installed")
        self.deleted.append(tag)
        self.models = [m for m in self.models if m.tag != tag]

    async def unload_model(self, tag: str) -> bool:
        self.unloaded.append(tag)
        self.running = [m for m in self.running if m.tag != tag]
        return True

    async def pull_stream(self, tag: str) -> AsyncIterator[dict[str, Any]]:  # type: ignore[override]
        self.calls.append(("pull", tag))
        for event in self.pulls or [
            {"status": "pulling manifest", "completed": 0, "total": 100, "percent": 0.0, "digest": None},
            {"status": "downloading", "completed": 50, "total": 100, "percent": 50.0, "digest": "sha"},
            {"status": "success", "completed": 100, "total": 100, "percent": 100.0, "digest": "sha"},
        ]:
            yield event
            await asyncio.sleep(0)

    # --------------------------------------------------------------- inference
    def _result(self, model: str) -> GenerationResult:
        if self.fail_with is not None:
            raise self.fail_with
        metrics = GenerationMetrics(
            prompt_tokens=10,
            completion_tokens=self.completion_tokens,
            total_duration_ms=500,
            eval_duration_ms=400,
        )
        return GenerationResult(model=model, response=self.response_text, metrics=metrics, done_reason="stop")

    async def generate(self, model: str, prompt: str, **kwargs: Any) -> GenerationResult:
        self.calls.append(("generate", {"model": model, "prompt": prompt, **kwargs}))
        if model not in {m.tag for m in self.models}:
            raise ModelNotFound(f"{model} is not installed")
        return self._result(model)

    async def chat(self, model: str, messages: list[dict[str, str]], **kwargs: Any) -> GenerationResult:
        self.calls.append(("chat", {"model": model, "messages": messages}))
        if model not in {m.tag for m in self.models}:
            raise ModelNotFound(f"{model} is not installed")
        return self._result(model)

    async def stream_generate(
        self, model: str, prompt: str, *, cancel: asyncio.Event | None = None, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:  # type: ignore[override]
        for token in self.stream_tokens:
            if cancel is not None and cancel.is_set():
                yield {"type": "cancelled", "partial": True, "metrics": {}}
                return
            if self.stream_delay:
                await asyncio.sleep(self.stream_delay)
            yield {"type": "token", "text": token}
        metrics = GenerationMetrics(completion_tokens=len(self.stream_tokens), eval_duration_ms=300).as_dict()
        metrics["ttft_ms"] = 100
        yield {"type": "done", "metrics": metrics, "done_reason": "stop"}
