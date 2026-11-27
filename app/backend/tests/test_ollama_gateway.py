"""Ollama gateway contract tests with a mocked HTTP transport (respx).

These tests pin the *shape* of the Ollama wire protocol we depend on: endpoint
paths, payload keys and the way failures are classified. They never touch a real
model runtime.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from creatoros.integrations.ollama.client import (
    OllamaGateway,
    default_think_for,
    num_ctx_for,
    safe_error_payload,
)
from creatoros.integrations.ollama.errors import (
    GenerationCancelled,
    GenerationTimeout,
    ModelLoadFailed,
    ModelNotFound,
    OllamaBadResponse,
    OllamaUnavailable,
)

BASE = "http://ollama.test:11434"


def gateway(**kwargs) -> OllamaGateway:
    return OllamaGateway(BASE, **kwargs)


@respx.mock
async def test_health_reports_online_and_version():
    respx.get(f"{BASE}/api/version").mock(return_value=httpx.Response(200, json={"version": "0.40.17"}))
    health = await gateway().health()
    assert health["online"] is True
    assert health["version"] == "0.40.17"
    assert health["latency_ms"] is not None


@respx.mock
async def test_health_raises_typed_error_when_daemon_is_down():
    respx.get(f"{BASE}/api/version").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(OllamaUnavailable):
        await gateway().health()


@respx.mock
async def test_is_available_swallows_errors():
    respx.get(f"{BASE}/api/version").mock(side_effect=httpx.ConnectError("refused"))
    assert await gateway().is_available() is False


@respx.mock
async def test_list_models_parses_size_and_metadata():
    respx.get(f"{BASE}/api/tags").mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "qwen3:1.7b",
                        "model": "qwen3:1.7b",
                        "modified_at": "2026-11-20T20:41:41.1+01:00",
                        "size": 1_400_000_000,
                        "digest": "d" * 64,
                        "details": {
                            "family": "qwen3",
                            "parameter_size": "2.0B",
                            "quantization_level": "Q4_K_M",
                            "format": "gguf",
                        },
                    }
                ]
            },
        )
    )
    models = await gateway().list_models()
    assert [m.tag for m in models] == ["qwen3:1.7b"]
    assert models[0].size_bytes == 1_400_000_000
    assert models[0].quantization == "Q4_K_M"


@respx.mock
async def test_list_models_reads_capabilities_from_show_when_absent():
    respx.get(f"{BASE}/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "m:1b", "size": 10, "digest": "x"}]})
    )
    respx.post(f"{BASE}/api/show").mock(
        return_value=httpx.Response(200, json={"capabilities": ["completion", "tools"], "model_info": {}})
    )
    models = await gateway().list_models(enrich=True)
    assert models[0].capabilities == ["completion", "tools"]


@respx.mock
async def test_show_model_extracts_context_length_and_parameter_count():
    respx.post(f"{BASE}/api/show").mock(
        return_value=httpx.Response(
            200,
            json={
                "license": "Apache-2.0",
                "capabilities": ["completion"],
                "details": {"family": "qwen3", "parameter_size": "2.0B", "quantization_level": "Q4_K_M"},
                "model_info": {
                    "general.architecture": "qwen3",
                    "qwen3.context_length": 40960,
                    "qwen3.embedding_length": 2048,
                    "qwen3.block_count": 28,
                    "qwen3.attention.head_count": 16,
                    "general.parameter_count": 2031739904,
                },
            },
        )
    )
    info = await gateway().show_model("qwen3:1.7b")
    assert info["context_length"] == 40960
    assert info["parameter_count"] == 2_031_739_904
    assert info["architecture"] == "qwen3"
    assert info["license_present"] is True


@respx.mock
async def test_show_model_maps_404_to_model_not_found():
    respx.post(f"{BASE}/api/show").mock(return_value=httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(ModelNotFound):
        await gateway().show_model("nope:1b")


@respx.mock
async def test_generate_returns_text_and_metrics():
    respx.post(f"{BASE}/api/generate").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "qwen3:1.7b",
                "response": "hello there",
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 11,
                "eval_count": 20,
                "total_duration": 900_000_000,
                "load_duration": 100_000_000,
                "prompt_eval_duration": 200_000_000,
                "eval_duration": 500_000_000,
            },
        )
    )
    result = await gateway().generate("qwen3:1.7b", "hi")
    assert result.response == "hello there"
    assert result.metrics.completion_tokens == 20
    assert result.metrics.tokens_per_sec == pytest.approx(40.0, rel=0.01)


@respx.mock
async def test_generate_sends_context_window_and_think_flag():
    route = respx.post(f"{BASE}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "ok", "done": True, "eval_count": 1})
    )
    await gateway().generate("qwen3:1.7b", "hi", context_len=8192, think=False, options={"temperature": 0.2})
    payload = json.loads(route.calls[0].request.content)
    assert payload["options"]["num_ctx"] == 8192
    assert payload["options"]["temperature"] == 0.2
    assert payload["think"] is False
    assert payload["stream"] is False


@respx.mock
async def test_generate_maps_500_oom_to_model_load_failed():
    respx.post(f"{BASE}/api/generate").mock(
        return_value=httpx.Response(
            500, json={"error": "llama runner process has terminated: out of memory"}
        )
    )
    with pytest.raises(ModelLoadFailed) as excinfo:
        await gateway().generate("qwen3:4b", "hi")
    assert "memory" in str(excinfo.value).lower()


@respx.mock
async def test_generate_maps_missing_model_message_to_model_not_found():
    respx.post(f"{BASE}/api/generate").mock(
        return_value=httpx.Response(404, json={"error": 'model "ghost:1b" not found, try pulling it first'})
    )
    with pytest.raises(ModelNotFound):
        await gateway().generate("ghost:1b", "hi")


@respx.mock
async def test_generate_rejects_non_json_body():
    respx.post(f"{BASE}/api/generate").mock(return_value=httpx.Response(200, text="<html>proxy</html>"))
    with pytest.raises(OllamaBadResponse):
        await gateway().generate("qwen3:1.7b", "hi")


async def test_generation_timeout_raises_typed_error():
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2.0)
        return httpx.Response(200, json={"response": "late"})

    transport = httpx.MockTransport(slow)
    gw = OllamaGateway(BASE, transport=transport, read_timeout=5.0)
    with pytest.raises(GenerationTimeout):
        await gw.generate("qwen3:1.7b", "hi", timeout_s=0.2)


async def test_cancellation_stops_a_stream_mid_flight():
    async def stream(request: httpx.Request) -> httpx.Response:
        body = "\n".join(
            json.dumps({"response": f"t{i} ", "done": False}) for i in range(200)
        ) + "\n" + json.dumps({"done": True, "eval_count": 200})
        return httpx.Response(200, text=body + "\n")

    gw = OllamaGateway(BASE, transport=httpx.MockTransport(stream))
    cancel = asyncio.Event()
    seen: list[str] = []
    events = gw.stream_generate("qwen3:1.7b", "hi", cancel=cancel)
    async for event in events:
        if event["type"] == "token":
            seen.append(event["text"])
            if len(seen) == 3:
                cancel.set()
    final = event
    assert final["type"] == "cancelled"
    assert len(seen) == 3


@respx.mock
async def test_stream_emits_thinking_separately_from_answer():
    body = "\n".join(
        [
            json.dumps({"thinking": "weighing options", "done": False}),
            json.dumps({"response": "final answer", "done": False}),
            json.dumps({"done": True, "eval_count": 2}),
        ]
    )
    respx.post(f"{BASE}/api/generate").mock(return_value=httpx.Response(200, text=body + "\n"))
    kinds: list[str] = []
    async for event in gateway().stream_generate("qwen3:1.7b", "hi"):
        kinds.append(event["type"])
    assert kinds == ["thinking", "token", "done"]


@respx.mock
async def test_pull_stream_reports_real_percent():
    body = "\n".join(
        [
            json.dumps({"status": "pulling manifest"}),
            json.dumps({"status": "downloading", "completed": 25, "total": 100, "digest": "sha256:aa"}),
            json.dumps({"status": "success"}),
        ]
    )
    respx.post(f"{BASE}/api/pull").mock(return_value=httpx.Response(200, text=body + "\n"))
    progress = [event async for event in gateway().pull_stream("qwen3:4b")]
    assert progress[1]["percent"] == 25.0
    assert progress[-1]["status"] == "success"


@respx.mock
async def test_pull_stream_surfaces_server_errors():
    respx.post(f"{BASE}/api/pull").mock(
        return_value=httpx.Response(200, text=json.dumps({"error": "file does not exist"}) + "\n")
    )
    with pytest.raises(ModelNotFound):
        async for _ in gateway().pull_stream("nope:99b"):
            pass


@respx.mock
async def test_pull_stream_maps_oom_to_load_failure():
    respx.post(f"{BASE}/api/pull").mock(
        return_value=httpx.Response(
            200, text=json.dumps({"error": "failed to allocate memory"}) + chr(10)
        )
    )
    with pytest.raises(ModelLoadFailed):
        async for _ in gateway().pull_stream("qwen3:4b"):
            pass


@respx.mock
async def test_delete_and_unload_hit_the_right_endpoints():
    delete_route = respx.delete(f"{BASE}/api/delete").mock(return_value=httpx.Response(200, json={}))
    generate_route = respx.post(f"{BASE}/api/generate").mock(return_value=httpx.Response(200, json={"done": True}))
    respx.get(f"{BASE}/api/ps").mock(return_value=httpx.Response(200, json={"models": []}))
    gw = gateway()
    await gw.delete_model("qwen3:0.6b")
    assert json.loads(delete_route.calls[0].request.content)["model"] == "qwen3:0.6b"
    assert await gw.unload_model("qwen3:4b") is True
    payload = json.loads(generate_route.calls[0].request.content)
    assert payload["keep_alive"] == 0
    assert payload["model"] == "qwen3:4b"


@respx.mock
async def test_running_models_reports_vram_split():
    respx.get(f"{BASE}/api/ps").mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "qwen3:1.7b",
                        "size": 1_700_000_000,
                        "size_vram": 1_700_000_000,
                        "context_length": 4096,
                        "expires_at": "2026-11-22T03:00:00Z",
                    }
                ]
            },
        )
    )
    running = await gateway().running_models()
    assert running[0].size_vram_bytes == running[0].size_bytes
    assert running[0].fully_on_gpu is True


def test_default_think_is_disabled_only_for_reasoning_tags():
    assert default_think_for("qwen3:1.7b") is False
    assert default_think_for("qwen3") is False
    assert default_think_for("gemma3:4b") is None
    assert default_think_for("nomic-embed-text:latest") is None


def test_num_ctx_for_respects_bounds():
    assert num_ctx_for(None) == 4096
    assert num_ctx_for(1024) == 2048
    assert num_ctx_for(999_999) == 32768
    assert num_ctx_for(8192) == 8192


@respx.mock
async def test_error_payload_is_redacted():
    payload = safe_error_payload(
        RuntimeError("token EAAG1234567890abcdef leaked and Bearer abc.def.ghi.jkl plus igqvXYZ0123456789abcdef")
    )
    text = json.dumps(payload)
    assert "EAAG1234567890" not in text
    assert "abc.def.ghi" not in text
    assert "igqvXYZ0123456789abcdef" not in text


async def test_already_cancelled_stream_never_touches_the_network():
    gw = gateway()  # unreachable address on purpose: cancellation must short-circuit
    cancel = asyncio.Event()
    cancel.set()
    events = [event async for event in gw.stream_generate("qwen3:1.7b", "hi", cancel=cancel)]
    assert events == [{"type": "cancelled", "partial": False, "metrics": events[0]["metrics"]}]
    assert isinstance(GenerationCancelled("x"), Exception)
