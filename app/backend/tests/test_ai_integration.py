"""Live integration tests against the real Ollama runtime.

Skipped by default. Run with:

    CREATOROS_RUN_LIVE_AI=1 python -m pytest tests/test_ai_integration.py -v

These are the only tests allowed to load a model; they use the smallest installed
tag and a tiny token budget so the suite stays cheap on a GTX 1050 Ti.
"""
from __future__ import annotations

import os
import time

import pytest

from creatoros.integrations.ollama.client import OllamaGateway
from creatoros.integrations.ollama.errors import ModelNotFound

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CREATOROS_RUN_LIVE_AI") != "1",
        reason="set CREATOROS_RUN_LIVE_AI=1 to run tests against the real model runtime",
    ),
]

BASE_URL = os.environ.get("CREATOROS_OLLAMA_URL", "http://127.0.0.1:11434")
LIGHT_TAG = os.environ.get("CREATOROS_LIVE_LIGHT_MODEL", "qwen3:0.6b")


@pytest.fixture()
async def gateway():
    gw = OllamaGateway(BASE_URL)
    yield gw
    await gw.aclose()


async def test_daemon_is_reachable_and_reports_a_version(gateway):
    health = await gateway.health()
    assert health["online"] is True
    assert health["version"]


async def test_installed_models_include_the_default_profiles(gateway):
    tags = await gateway.installed_tags()
    assert LIGHT_TAG in tags
    assert any(tag.startswith("nomic-embed-text") for tag in tags)


async def test_show_returns_a_real_context_window(gateway):
    info = await gateway.show_model(LIGHT_TAG)
    assert info["context_length"] and info["context_length"] > 0
    assert info["parameter_count"] and info["parameter_count"] > 0
    assert info["quantization"]


async def test_generate_produces_text_and_plausible_throughput(gateway):
    started = time.perf_counter()
    result = await gateway.generate(
        LIGHT_TAG,
        "Reply with the single word: ready",
        options={"num_predict": 24, "temperature": 0.0},
        context_len=2048,
    )
    elapsed = time.perf_counter() - started
    assert result.response.strip()
    assert result.metrics.completion_tokens > 0
    # ADR-09 measured ~75 tok/s for this tag; anything below 5 indicates a CPU-only fallback.
    assert result.metrics.tokens_per_sec > 5.0
    assert elapsed < 120


async def test_model_is_fully_resident_on_the_gpu_after_loading(gateway):
    await gateway.generate(LIGHT_TAG, "hi", options={"num_predict": 4}, keep_alive="30s")
    running = {model.tag: model for model in await gateway.running_models()}
    model = running.get(LIGHT_TAG)
    assert model is not None
    assert model.size_bytes > 0
    assert model.vram_percent > 50, f"only {model.vram_percent}% of the model is on the GPU"


async def test_streaming_yields_incremental_tokens(gateway):
    tokens: list[str] = []
    done = None
    async for event in gateway.stream_generate(
        LIGHT_TAG, "Count: one, two, three", options={"num_predict": 24}, context_len=2048
    ):
        if event["type"] == "token":
            tokens.append(event["text"])
        elif event["type"] == "done":
            done = event
    assert len(tokens) >= 3
    assert done is not None
    assert done["metrics"]["ttft_ms"] is not None


async def test_unload_removes_the_model_from_memory(gateway):
    await gateway.generate(LIGHT_TAG, "hi", options={"num_predict": 4}, keep_alive="30s")
    assert LIGHT_TAG in {model.tag for model in await gateway.running_models()}
    assert await gateway.unload_model(LIGHT_TAG) is True
    assert LIGHT_TAG not in {model.tag for model in await gateway.running_models()}


async def test_unknown_model_is_reported_without_side_effects(gateway):
    with pytest.raises(ModelNotFound):
        await gateway.show_model("creatoros-definitely-missing:1b")
