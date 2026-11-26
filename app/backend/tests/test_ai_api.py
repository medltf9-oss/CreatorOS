"""AI API tests: routes, error envelopes, cancellation, limits, queue integration.

The app boots with CREATOROS_DISABLE_AI=1 (see conftest), so these tests attach a
fake gateway and a real governor/router to `app.state` exactly where production
wires the real ones. Nothing here touches a model runtime.
"""
from __future__ import annotations

import json

import pytest

from creatoros.services.governor import ResourceGovernor, ResourceLimits
from creatoros.services.models import ModelRegistry, ModelRouter
from tests.fakes import FakeGateway


class FakeSmi:
    def __call__(self):
        return (
            {
                "name": "fake GPU",
                "total_mb": 4096,
                "used_mb": 1024,
                "free_mb": 3072,
                "memory_percent": 25.0,
                "utilization_percent": 3,
                "temperature_c": 41,
                "driver": "fake",
            },
            None,
        )


@pytest.fixture()
def ai(client, session):
    """Attach fake AI infrastructure to the running app."""
    from creatoros.main import app

    gateway = FakeGateway()
    governor = ResourceGovernor(gateway, gpu_reader=FakeSmi())
    registry = ModelRegistry(session)
    registry.ensure_seeded()

    app.state.gateway = gateway
    app.state.governor = governor
    app.state.router_factory = None
    app.state.ai_ready = True
    app.state.cancel_registry = {}
    app.state.pulls = {}
    app.state.pull_tasks = {}

    return {"gateway": gateway, "governor": governor, "registry": registry, "session": session, "app": app}


# ------------------------------------------------------------------- health
def test_health_reports_the_runtime_and_pressure(client, ai):
    response = client.get("/ai/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ollama"]["online"] is True
    assert body["models"]["installed"] == 2
    assert body["resources"]["gpu"]["name"] == "fake GPU"
    assert body["resources"]["pressure"] in {"normal", "saturated", "critical"}
    assert body["resources"]["limits"]["max_concurrent_generations"] == 1
    assert body["capabilities"]["inference"] == "available"


def test_health_degrades_honestly_when_the_daemon_is_offline(client, ai):
    ai["gateway"].online = False
    body = client.get("/ai/health").json()
    assert body["ollama"]["online"] is False
    assert body["models"]["installed"] == 0
    assert body["ollama"]["error"]["code"]
    assert body["capabilities"]["inference"] == "unavailable"


# ------------------------------------------------------------------- models
def test_models_lists_real_metadata_and_profile_mapping(client, ai):
    body = client.get("/ai/models").json()
    tags = [model["tag"] for model in body["items"]]
    assert tags == ["qwen3:0.6b", "qwen3:1.7b"]
    specialist = next(model for model in body["items"] if model["tag"] == "qwen3:1.7b")
    assert specialist["profile"] == "specialist"
    assert specialist["loaded"] is False
    assert body["total_size_gb"] > 0
    assert body["ollama_online"] is True


def test_model_detail_includes_context_and_parameters(client, ai):
    body = client.get("/ai/models/qwen3:1.7b").json()
    assert body["context_length"] == 40960
    assert body["parameter_count"] == 2_031_739_904
    assert body["quantization"] == "Q4_K_M"


def test_unknown_model_detail_is_404_with_envelope(client, ai):
    response = client.get("/ai/models/nope:1b")
    assert response.status_code == 404
    assert response.json()["code"] == "MODEL_NOT_FOUND"
    assert response.json()["correlation_id"]


def test_delete_requires_explicit_confirmation(client, ai):
    response = client.delete("/ai/models/qwen3:0.6b")
    assert response.status_code == 428
    assert response.json()["code"] == "CONFIRMATION_REQUIRED"
    assert ai["gateway"].deleted == []


def test_delete_is_refused_while_the_model_is_loaded(client, ai):
    from creatoros.integrations.ollama.client import RunningModel

    ai["gateway"].running = [
        RunningModel(tag="qwen3:0.6b", size_bytes=522_000_000, size_vram_bytes=522_000_000, context_length=4096, expires_at=None)
    ]
    response = client.delete("/ai/models/qwen3:0.6b?confirm=true")
    assert response.status_code == 409
    assert response.json()["code"] == "MODEL_BUSY"
    assert ai["gateway"].deleted == []


def test_force_delete_of_a_loaded_model_unloads_first(client, ai):
    from creatoros.integrations.ollama.client import RunningModel

    ai["gateway"].running = [
        RunningModel(tag="qwen3:0.6b", size_bytes=522_000_000, size_vram_bytes=522_000_000, context_length=4096, expires_at=None)
    ]
    response = client.delete("/ai/models/qwen3:0.6b?confirm=true&force=true")
    assert response.status_code == 200
    assert ai["gateway"].unloaded == ["qwen3:0.6b"]
    assert ai["gateway"].deleted == ["qwen3:0.6b"]


def test_delete_writes_an_audit_row(client, ai, session):
    from creatoros.db.models import AuditEvent

    client.delete("/ai/models/qwen3:0.6b?confirm=true")
    actions = [row.action for row in session.query(AuditEvent).all()]
    assert "ai.model.delete" in actions


def test_unload_endpoint(client, ai):
    response = client.post("/ai/models/qwen3:1.7b/unload")
    assert response.status_code == 200
    assert response.json()["unloaded"] is True
    assert ai["gateway"].unloaded == ["qwen3:1.7b"]


# --------------------------------------------------------------------- pull
def test_pull_returns_a_job_id_and_reports_progress(client, ai):
    started = client.post("/ai/pulls", json={"model": "qwen3:4b"})
    assert started.status_code == 202
    pull_id = started.json()["pull_id"]

    listing = client.get("/ai/pulls").json()
    entry = next(item for item in listing if item["pull_id"] == pull_id)
    assert entry["state"] in {"running", "succeeded", "queued"}
    assert entry["model"] == "qwen3:4b"


def test_cancel_unknown_pull_is_404(client, ai):
    response = client.delete("/ai/pulls/does-not-exist")
    assert response.status_code == 404


# ---------------------------------------------------------------- benchmark
def test_benchmark_refuses_an_embedding_only_model(client, ai):
    ai["gateway"].show_model = _embedding_show  # type: ignore[assignment]
    response = client.post("/ai/models/nomic-embed-text:latest/benchmark", json={"max_tokens": 8})
    assert response.status_code == 409
    assert response.json()["code"] == "MODEL_NOT_GENERATIVE"


async def _embedding_show(tag: str) -> dict:
    return {
        "tag": tag,
        "family": "nomic-bert",
        "families": ["nomic-bert"],
        "parameter_size": "137M",
        "parameter_count": 136_729_088,
        "quantization": "F16",
        "format": "gguf",
        "architecture": "nomic-bert",
        "context_length": 2048,
        "embedding_length": 768,
        "block_count": 12,
        "capabilities": ["embedding"],
        "default_parameters": None,
        "modified_at": None,
        "license_present": True,
    }


def test_models_map_the_implicit_latest_tag_to_its_profile(client, ai):
    from creatoros.integrations.ollama.client import ModelSummary

    ai["gateway"].models = [
        *ai["gateway"].models,
        ModelSummary(tag="nomic-embed-text:latest", size_bytes=274_000_000, digest="c" * 64),
    ]
    body = client.get("/ai/models").json()
    embedding = next(item for item in body["items"] if item["tag"] == "nomic-embed-text:latest")
    assert embedding["profile"] == "embedding"
    assert embedding["context_source"] == "profile"


def test_benchmark_runs_a_real_generation_through_the_gateway(client, ai):
    ai["gateway"].response_text = "benchmark answer"
    ai["gateway"].completion_tokens = 25
    response = client.post("/ai/models/qwen3:0.6b/benchmark", json={"max_tokens": 25})
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "qwen3:0.6b"
    assert body["completion_tokens"] == 25
    assert body["tokens_per_sec"] > 0
    assert body["sample"] == "benchmark answer"
    # the measurement is persisted against the profile
    from creatoros.db.models import ModelBenchmark

    rows = [r for r in ai["session"].query(ModelBenchmark).all() if r.profile_name == "router_light"]
    measured = [r for r in rows if str(r.notes).startswith("live benchmark")]
    assert measured, "the benchmark result was not persisted"
    assert measured[0].tokens_per_sec > 0


# ---------------------------------------------------------------- generate
def test_generate_returns_text_metrics_and_execution_id(client, ai):
    response = client.post("/ai/generate", json={"prompt": "hello", "profile": "specialist"})
    assert response.status_code == 200
    body = response.json()
    assert body["response"] == "fake answer"
    assert body["model"] == "qwen3:1.7b"
    assert body["execution_id"]
    assert body["metrics"]["tokens_per_sec"] > 0


def test_generate_persists_the_execution_row(client, ai, session):
    from creatoros.db.models import AiExecution

    body = client.post("/ai/generate", json={"prompt": "hello"}).json()
    row = session.get(AiExecution, body["execution_id"])
    assert row is not None
    assert row.state == "succeeded"
    assert row.model_tag == "qwen3:1.7b"
    assert row.response_text == "fake answer"
    assert row.completion_tokens == 12


def test_generate_failure_is_recorded_and_surfaced(client, ai, session):
    from creatoros.db.models import AiExecution
    from creatoros.integrations.ollama.errors import ModelLoadFailed

    ai["gateway"].fail_with = ModelLoadFailed("out of memory")
    response = client.post("/ai/generate", json={"prompt": "hello"})
    assert response.status_code == 507
    payload = response.json()
    assert payload["code"] == "MODEL_LOAD_FAILED"
    row = session.get(AiExecution, payload["detail"]["execution_id"]) if payload.get("detail") else None
    if row is not None:
        assert row.state == "failed"


def test_thinking_only_answer_is_reported_as_failure_not_success(client, ai):
    ai["gateway"].response_text = ""
    ai["gateway"].completion_tokens = 40
    from creatoros.integrations.ollama.client import GenerationResult

    async def thinking_only(model, prompt, **kwargs):
        return GenerationResult(model=model, response="", metrics=ai_result_metrics(), thinking="long monologue")

    from creatoros.integrations.ollama.client import GenerationMetrics

    def ai_result_metrics():
        return GenerationMetrics(completion_tokens=40, eval_duration_ms=1000)

    ai["gateway"].generate = thinking_only
    response = client.post("/ai/generate", json={"prompt": "hello"})
    assert response.status_code == 200
    body = response.json()
    assert body["incomplete"] is True
    assert body["response"] == ""
    assert any("thinking" in warning for warning in body["warnings"])


def test_generate_rejects_an_empty_prompt(client, ai):
    assert client.post("/ai/generate", json={"prompt": ""}).status_code == 422


def test_generate_with_no_models_installed_returns_503(client, ai):
    ai["gateway"].models = []
    response = client.post("/ai/generate", json={"prompt": "hello"})
    assert response.status_code == 503
    assert response.json()["code"] == "NO_MODEL_AVAILABLE"


def test_second_concurrent_generation_is_refused_with_429(client, ai):
    ai["governor"].update_limits(ResourceLimits(max_concurrent_generations=1))

    from creatoros.main import app

    governor = app.state.governor
    governor._active = 1  # simulate an in-flight generation
    response = client.post("/ai/generate", json={"prompt": "hello", "wait_s": 0})
    governor._active = 0
    assert response.status_code == 429
    assert response.json()["code"] == "GOVERNOR_BUSY"


# ------------------------------------------------------------------- stream
def test_stream_emits_meta_tokens_and_done(client, ai):
    with client.stream("POST", "/ai/stream", json={"prompt": "hello"}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        events = [json.loads(line) for line in response.iter_lines() if line.strip()]
    assert events[0]["type"] == "meta"
    assert events[0]["execution_id"]
    assert [event["text"] for event in events if event["type"] == "token"] == ["one ", "two ", "three"]
    assert events[-1]["type"] == "done"
    assert events[-1]["metrics"]["ttft_ms"] == 100


def test_stream_can_be_cancelled_from_another_request(client, ai):
    ai["gateway"].stream_delay = 0.01
    ai["gateway"].stream_tokens = [f"t{i} " for i in range(50)]

    with client.stream("POST", "/ai/stream", json={"prompt": "long"}) as response:
        execution_id = None
        saw_cancel = False
        for line in response.iter_lines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event["type"] == "meta":
                execution_id = event["execution_id"]
                assert client.post(f"/ai/executions/{execution_id}/cancel").status_code == 200
            elif event["type"] == "cancelled":
                saw_cancel = True
                break
    assert saw_cancel is True
    assert execution_id


def test_stream_reports_gateway_failure_as_an_error_event(client, ai):
    from creatoros.integrations.ollama.errors import ModelLoadFailed

    ai["gateway"].fail_with = ModelLoadFailed("cannot load")

    async def boom(*args, **kwargs):
        raise ModelLoadFailed("cannot load")
        yield  # pragma: no cover

    ai["gateway"].stream_generate = boom
    with client.stream("POST", "/ai/stream", json={"prompt": "hello"}) as response:
        events = [json.loads(line) for line in response.iter_lines() if line.strip()]
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "MODEL_LOAD_FAILED"


# ---------------------------------------------------------------- executions
def test_execution_history_is_newest_first_and_filterable(client, ai):
    for prompt in ("one", "two"):
        client.post("/ai/generate", json={"prompt": prompt})
    rows = client.get("/ai/executions?limit=10").json()
    assert len(rows) == 2
    assert rows[0]["prompt_text"] == "two"
    filtered = client.get("/ai/executions?state=succeeded").json()
    assert all(row["state"] == "succeeded" for row in filtered)
    assert client.get("/ai/executions?model=qwen3:0.6b").json() == []


def test_cancel_unknown_execution_is_404(client, ai):
    assert client.post("/ai/executions/nope/cancel").status_code == 404


def test_cancelling_a_finished_execution_is_conflict(client, ai):
    body = client.post("/ai/generate", json={"prompt": "hello"}).json()
    response = client.post(f"/ai/executions/{body['execution_id']}/cancel")
    assert response.status_code == 409


# ------------------------------------------------------------- resources
def test_resources_endpoint_reports_telemetry(client, ai):
    body = client.get("/ai/resources").json()
    assert body["cpu_percent"] >= 0
    assert body["ram_total_gb"] > 0
    assert body["gpu"]["total_mb"] == 4096
    assert body["active_generations"] == 0


def test_history_endpoint_returns_samples(client, ai):
    client.get("/ai/resources")
    body = client.get("/ai/resources").json()
    assert body["history"]


# ------------------------------------------------------------------ limits
def test_limits_roundtrip(client, ai):
    original = client.get("/ai/limits").json()
    assert original["max_concurrent_generations"] == 1
    updated = client.put("/ai/limits", json={"max_concurrent_generations": 2, "max_ram_percent": 80})
    assert updated.status_code == 200
    assert updated.json()["max_concurrent_generations"] == 2
    assert client.get("/ai/limits").json()["max_concurrent_generations"] == 2
    ai["governor"].update_limits(ResourceLimits())


def test_limits_are_persisted_to_settings(client, ai, session):
    from creatoros.db.models import AppSetting

    client.put("/ai/limits", json={"max_queue_depth": 40})
    row = session.get(AppSetting, "resources.limits")
    assert row is not None
    assert row.value_json["max_queue_depth"] == 40
    ai["governor"].update_limits(ResourceLimits())


def test_invalid_limits_are_rejected_with_422(client, ai):
    response = client.put("/ai/limits", json={"max_concurrent_generations": 12})
    assert response.status_code == 422
    body = response.json()
    # One envelope for every failure: code + human message + correlation id.
    assert body["code"] == "VALIDATION_ERROR"
    assert "max_concurrent_generations" in body["message"]
    assert body["correlation_id"]


# ---------------------------------------------------------------- profiles
def test_profiles_expose_tag_context_and_benchmarks(client, ai):
    body = client.get("/ai/profiles").json()
    names = {profile["name"] for profile in body}
    assert {"router_light", "specialist", "planner", "coding", "vision", "embedding"} <= names
    specialist = next(p for p in body if p["name"] == "specialist")
    assert specialist["ollama_tag"] == "qwen3:1.7b"


def test_profile_can_be_repointed_with_validation(client, ai):
    response = client.put("/ai/profiles/specialist", json={"ollama_tag": "qwen3:1.7b", "context_len": 8192})
    assert response.status_code == 200
    assert response.json()["context_len"] == 8192
    assert client.put("/ai/profiles/nope", json={"context_len": 4096}).status_code == 404


# --------------------------------------------------------------- queue glue
def test_ai_job_is_enqueued_and_stored(client, ai, session):
    created = client.post(
        "/ai/jobs",
        json={"prompt": "queued work", "profile": "specialist", "idempotency_key": "job-test-1"},
    )
    assert created.status_code == 202
    job_id = created.json()["job_id"]
    duplicate = client.post(
        "/ai/jobs",
        json={"prompt": "queued work", "profile": "specialist", "idempotency_key": "job-test-1"},
    )
    assert duplicate.json()["job_id"] == job_id

    status = client.get(f"/ai/jobs/{job_id}").json()
    assert status["state"] in {"queued", "running", "succeeded", "failed"}


def test_ai_job_handler_persists_result_before_marking_success(session, ai):
    """The queue handler must write the result row before the job is 'succeeded'."""
    from creatoros.db.models import AiExecution
    from creatoros.services.ai import build_ai_job_handler

    handler = build_ai_job_handler(
        lambda: session,
        ai["gateway"],
        ai["governor"],
        lambda s: ModelRouter(ModelRegistry(s), ai["governor"], ai["gateway"]),
    )
    result = handler(
        session,
        type("Job", (), {"id": "job-1", "payload": {"prompt": "hello", "profile": "specialist"}})(),
    )
    assert result["response"] == "fake answer"
    row = session.get(AiExecution, result["execution_id"])
    assert row.state == "succeeded"
    assert row.queue_job_id == "job-1"


def test_queue_job_detail_is_404_for_unknown_job(client, ai):
    assert client.get("/ai/jobs/nope").status_code == 404


# ----------------------------------------------------------------- security
def test_ai_routes_require_authentication(unauth_client):
    for method, path in [
        ("get", "/ai/health"),
        ("get", "/ai/models"),
        ("get", "/ai/resources"),
        ("get", "/ai/limits"),
        ("get", "/ai/executions"),
        ("post", "/ai/generate"),
        ("delete", "/ai/models/qwen3:0.6b?confirm=true"),
    ]:
        response = getattr(unauth_client, method)(path)
        assert response.status_code == 401, f"{method} {path} was not protected"
