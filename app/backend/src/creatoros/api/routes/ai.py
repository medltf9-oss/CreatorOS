"""AI infrastructure routes: gateway health, model lab, inference, resources.

Everything here talks to the real Ollama daemon through the gateway. No endpoint
fabricates a model list, a benchmark or a completion.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from ...db.models import AiExecution, QueueJob
from ...integrations.ollama.client import OllamaGateway
from ...integrations.ollama.errors import ModelBusy, ModelNotFound, OllamaError, OllamaUnavailable
from ...schemas.ai import (
    AiExecutionOut,
    BenchmarkRequest,
    BenchmarkResult,
    ChatRequest,
    GenerateRequest,
    LimitsModel,
    ModelDetail,
    ModelListResponse,
    ModelPullRequest,
    ProfileModel,
    ProfileUpdate,
    PullStatus,
)
from ...services.governor import GovernorBusy, ResourceGovernor, ResourceLimits, ResourcePressure
from ...services.models import ModelRegistry, ModelRouter, RoutingError, normalise_tag
from ..deps import Audit, DbSession

router = APIRouter(tags=["ai"])


# ------------------------------------------------------------------ app wiring
def _gateway(request: Request) -> OllamaGateway:
    gateway = getattr(request.app.state, "gateway", None)
    if gateway is None:  # pragma: no cover - lifespan always sets it
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OLLAMA_UNAVAILABLE: gateway not initialised")
    return gateway


def _governor(request: Request) -> ResourceGovernor:
    governor = getattr(request.app.state, "governor", None)
    if governor is None:  # pragma: no cover
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GOVERNOR_UNAVAILABLE: not initialised")
    return governor


def _router(request: Request, session: DbSession) -> ModelRouter:
    gateway = _gateway(request)
    governor = _governor(request)
    registry = ModelRegistry(session)
    return ModelRouter(registry, governor, gateway)


def _ai_service(request: Request, session: DbSession):
    from ...services.ai import AiService

    registry = getattr(request.app.state, "cancel_registry", None)
    return AiService(session, _gateway(request), _governor(request), _router(request, session), registry)


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, OllamaUnavailable):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"{exc.code}: {exc}")
    if isinstance(exc, ModelNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, f"{exc.code}: {exc}")
    if isinstance(exc, ModelBusy):
        return HTTPException(status.HTTP_409_CONFLICT, f"{exc.code}: {exc}")
    if isinstance(exc, ResourcePressure):
        return HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"{exc.code}: {exc}")
    if isinstance(exc, GovernorBusy):
        return HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"{exc.code}: {exc}")
    if isinstance(exc, RoutingError):
        # Nothing can serve the request right now; that is "service unavailable",
        # not a client conflict.
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"{exc.code}: {exc}")
    if isinstance(exc, OllamaError):
        return HTTPException(exc.http_status, f"{exc.code}: {exc}")
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"AI_ERROR: {exc}")


# ---------------------------------------------------------------------- health
@router.get("/ai/health")
async def ai_health(request: Request, session: DbSession) -> dict[str, Any]:
    gateway = _gateway(request)
    governor = _governor(request)
    try:
        health = await gateway.health()
        models = await gateway.list_models()
        running = await gateway.running_models()
        ollama_error = None
    except OllamaError as exc:
        health = {"online": False, "base_url": gateway.base_url}
        models, running = [], []
        ollama_error = {"code": exc.code, "message": str(exc)}

    snapshot = governor.snapshot_cached()
    return {
        "ollama": {**health, "error": ollama_error},
        "gateway": {
            "base_url": gateway.base_url,
            "read_timeout_s": governor.limits.generation_timeout_s,
            "requests": governor.stats()["samples"],
        },
        "models": {
            "installed": len(models),
            "loaded": [{"tag": m.tag, "vram_gb": round(m.size_vram_bytes / 1e9, 2)} for m in running],
            "total_size_gb": round(sum(m.size_bytes for m in models) / 1e9, 2),
        },
        "resources": snapshot,
        "queue": {"depth": snapshot.get("queue_depth", 0), "active_generations": snapshot.get("active_generations", 0)},
        "capabilities": {
            "inference": "available" if health.get("online") else "unavailable",
            "streaming": "available" if health.get("online") else "unavailable",
            "model_management": "available" if health.get("online") else "unavailable",
        },
    }


# ---------------------------------------------------------------------- models
@router.get("/ai/models", response_model=ModelListResponse)
async def list_models(request: Request, session: DbSession) -> ModelListResponse:
    gateway = _gateway(request)
    registry = ModelRegistry(session)
    registry.ensure_seeded()
    try:
        installed = await gateway.list_models()
        running = {m.tag: m for m in await gateway.running_models()}
    except OllamaUnavailable as exc:
        raise _translate(exc) from exc

    benchmarks = registry.latest_benchmarks()
    profile_by_tag = {normalise_tag(p.ollama_tag): p for p in registry.profiles()}
    items = []
    for model in installed:
        profile = profile_by_tag.get(normalise_tag(model.tag))
        run = running.get(model.tag)
        bench = benchmarks.get(profile.name) if profile else None
        items.append(
            {
                "tag": model.tag,
                "size_bytes": model.size_bytes,
                "size_gb": model.size_gb,
                "digest": model.digest,
                "modified_at": model.modified_at,
                "family": model.family,
                "parameter_size": model.parameter_size,
                "quantization": model.quantization,
                "capabilities": model.capabilities,
                "profile": profile.name if profile else None,
                "role": (profile.roles_json or [None])[0] if profile else None,
                "loaded": run is not None,
                "vram_gb": round(run.size_vram_bytes / 1e9, 2) if run else 0.0,
                "vram_percent": run.vram_percent if run else 0.0,
                # When the model is resident Ollama reports the context it actually
                # allocated; otherwise fall back to the profile's configured window
                # and say so, rather than showing nothing.
                "context_length": run.context_length if run else (profile.context_len if profile else None),
                "context_source": "loaded" if run and run.context_length else ("profile" if profile else None),
                "benchmark_tps": bench["tokens_per_sec"] if bench else None,
                "benchmark_at": bench["measured_at"] if bench else None,
                "benchmark_source": bench["source"] if bench else None,
            }
        )
    total_size = sum(i["size_bytes"] for i in items)
    return ModelListResponse(
        items=items,
        total=len(items),
        total_size_gb=round(total_size / 1e9, 2),
        loaded_count=len(running),
        ollama_online=True,
    )


@router.get("/ai/models/{tag:path}", response_model=ModelDetail)
async def model_detail(tag: str, request: Request, session: DbSession) -> ModelDetail:
    gateway = _gateway(request)
    try:
        detail = await gateway.show_model(tag)
        running = {m.tag: m for m in await gateway.running_models()}
    except OllamaError as exc:
        raise _translate(exc) from exc

    registry = ModelRegistry(session)
    registry.ensure_seeded()
    profile = next((p for p in registry.profiles() if normalise_tag(p.ollama_tag) == normalise_tag(tag)), None)
    benchmarks = registry.latest_benchmarks()
    run = running.get(tag)

    return ModelDetail(
        **detail,
        profile=profile.name if profile else None,
        benchmark_tps=benchmarks.get(profile.name, {}).get("tokens_per_sec") if profile else None,
        benchmark_source=benchmarks.get(profile.name, {}).get("source") if profile else None,
        loaded=run is not None,
        vram_gb=round(run.size_vram_bytes / 1e9, 2) if run else 0.0,
        vram_percent=run.vram_percent if run else 0.0,
    )


@router.delete("/ai/models/{tag:path}", status_code=status.HTTP_200_OK)
async def delete_model(
    tag: str,
    request: Request,
    session: DbSession,
    audit: Audit,
    confirm: bool = Query(default=False, description="must be true to actually delete"),
    force: bool = Query(default=False, description="delete even if currently loaded"),
) -> dict[str, Any]:
    gateway = _gateway(request)
    if not confirm:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            "CONFIRMATION_REQUIRED: pass confirm=true to delete a model (irreversible)",
        )
    running = {m.tag for m in await gateway.running_models()}
    if tag in running and not force:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"MODEL_BUSY: {tag} is loaded; unload it first or pass force=true",
        )
    if tag in running and force:
        await gateway.unload_model(tag)
    try:
        await gateway.delete_model(tag)
    except OllamaError as exc:
        audit.record("ai.model.delete", resource_type="model", resource_id=tag, outcome="failed")
        raise _translate(exc) from exc
    audit.record("ai.model.delete", resource_type="model", resource_id=tag, metadata={"forced": force})
    return {"deleted": tag, "forced": force}


@router.post("/ai/models/{tag:path}/unload")
async def unload_model(tag: str, request: Request, audit: Audit) -> dict[str, Any]:
    gateway = _gateway(request)
    try:
        unloaded = await gateway.unload_model(tag)
    except OllamaError as exc:
        raise _translate(exc) from exc
    audit.record("ai.model.unload", resource_type="model", resource_id=tag, metadata={"unloaded": unloaded})
    return {"tag": tag, "unloaded": unloaded}


# ------------------------------------------------------------------------ pull
@router.post("/ai/pulls", response_model=PullStatus, status_code=status.HTTP_202_ACCEPTED)
async def start_pull(body: ModelPullRequest, request: Request, audit: Audit) -> PullStatus:
    gateway = _gateway(request)
    pulls: dict[str, dict[str, Any]] = request.app.state.pulls
    tag = body.model.strip()
    if not tag:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "VALIDATION_FAILED: model tag is required")
    for existing in pulls.values():
        if existing["model"] == tag and existing["state"] in ("pulling", "queued"):
            return PullStatus(**existing)

    pull_id = uuid.uuid4().hex[:12]
    record: dict[str, Any] = {
        "pull_id": pull_id,
        "model": tag,
        "state": "pulling",
        "status": "starting",
        "completed": 0,
        "total": 0,
        "percent": 0.0,
        "error": None,
        "cancel_requested": False,
    }
    pulls[pull_id] = record
    audit.record("ai.model.pull.start", resource_type="model", resource_id=tag, metadata={"pull_id": pull_id})

    async def run() -> None:
        try:
            async for event in gateway.pull_stream(tag):
                record.update(
                    status=event.get("status") or "pulling",
                    completed=event.get("completed", 0),
                    total=event.get("total", 0),
                    percent=event.get("percent", 0.0),
                )
                if record["cancel_requested"]:
                    record.update(state="cancelled", status="cancelled")
                    return
            record.update(state="succeeded", status="success", percent=100.0)
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            record.update(state="cancelled", status="cancelled")
            raise
        except OllamaError as exc:
            record.update(state="failed", status="failed", error=f"{exc.code}: {exc}")
        except Exception as exc:  # noqa: BLE001 - surface, never swallow
            record.update(state="failed", status="failed", error=f"{type(exc).__name__}: {exc}")

    task = asyncio.create_task(run(), name=f"pull-{tag}")
    request.app.state.pull_tasks[pull_id] = task
    return PullStatus(**record)


@router.get("/ai/pulls", response_model=list[PullStatus])
async def list_pulls(request: Request) -> list[PullStatus]:
    returns = request.app.state.pulls
    return [PullStatus(**record) for record in returns.values()]


@router.get("/ai/pulls/{pull_id}", response_model=PullStatus)
async def pull_status(pull_id: str, request: Request) -> PullStatus:
    record = request.app.state.pulls.get(pull_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND: unknown pull id")
    return PullStatus(**record)


@router.delete("/ai/pulls/{pull_id}", response_model=PullStatus)
async def cancel_pull(pull_id: str, request: Request, audit: Audit) -> PullStatus:
    record = request.app.state.pulls.get(pull_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND: unknown pull id")
    record["cancel_requested"] = True
    task = request.app.state.pull_tasks.get(pull_id)
    if task is not None and not task.done():
        task.cancel()
    record.update(state="cancelled", status="cancelled")
    audit.record("ai.model.pull.cancel", resource_type="model", resource_id=record["model"])
    return PullStatus(**record)


# ------------------------------------------------------------------- benchmark
@router.post("/ai/models/{tag:path}/benchmark", response_model=BenchmarkResult)
async def benchmark(tag: str, request: Request, session: DbSession, audit: Audit, body: BenchmarkRequest | None = None) -> BenchmarkResult:
    """Run a real, short generation and record the measured throughput."""
    gateway = _gateway(request)
    governor = _governor(request)
    body = body or BenchmarkRequest()
    registry = ModelRegistry(session)
    registry.ensure_seeded()

    try:
        info = await gateway.show_model(tag)
    except OllamaError as exc:
        raise _translate(exc) from exc
    capabilities = info.get("capabilities") or []
    if capabilities and "completion" not in capabilities:
        # An embedding-only model cannot generate; measuring it would be meaningless.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"MODEL_NOT_GENERATIVE: {tag} supports {', '.join(capabilities)} but not text generation",
        )

    profile = next((p for p in registry.profiles() if normalise_tag(p.ollama_tag) == normalise_tag(tag)), None)
    try:
        await governor.acquire(wait_s=30.0, label=f"benchmark:{tag}")
    except (ResourcePressure, GovernorBusy) as exc:
        raise _translate(exc) from exc
    try:
        result = await gateway.generate(
            tag,
            body.prompt,
            options={"temperature": 0.0, "num_predict": body.max_tokens},
            context_len=body.context_len,
            keep_alive=body.keep_alive,
            timeout_s=max(60.0, governor.limits.generation_timeout_s),
        )
    except OllamaError as exc:
        raise _translate(exc) from exc
    finally:
        await governor.release()

    metrics = result.metrics
    if profile is not None:
        registry.set_benchmark(
            profile.name,
            metrics.tokens_per_sec,
            context_len=body.context_len,
            notes=f"live benchmark {metrics.completion_tokens} tokens at num_ctx={body.context_len}",
        )
    audit.record(
        "ai.model.benchmark",
        resource_type="model",
        resource_id=tag,
        metadata={"tokens_per_sec": metrics.tokens_per_sec, "completion_tokens": metrics.completion_tokens},
    )
    return BenchmarkResult(
        model=tag,
        profile=profile.name if profile else None,
        tokens_per_sec=metrics.tokens_per_sec,
        completion_tokens=metrics.completion_tokens,
        prompt_tokens=metrics.prompt_tokens,
        ttft_ms=metrics.ttft_ms,
        eval_duration_ms=metrics.eval_duration_ms,
        total_duration_ms=metrics.total_duration_ms,
        context_len=body.context_len,
        sample=result.response[:400],
    )


# ------------------------------------------------------------------- inference
@router.post("/ai/generate")
async def generate(body: GenerateRequest, request: Request, session: DbSession, audit: Audit) -> dict[str, Any]:
    service = _ai_service(request, session)
    try:
        result = await service.generate(
            body.prompt,
            profile=body.profile,
            complexity=body.complexity,
            options=body.options,
            context_len=body.context_len,
            system=body.system,
            wait_s=body.wait_s,
            think=body.think,
        )
    except (OllamaError, ResourcePressure, GovernorBusy, RoutingError, ValueError) as exc:
        audit.record("ai.generate", resource_type="model", resource_id=body.profile or "auto", outcome="failed")
        raise _translate(exc) from exc
    audit.record(
        "ai.generate",
        resource_type="ai_execution",
        resource_id=result["execution_id"],
        metadata={"model": result["model"], "tokens_per_sec": result["metrics"]["tokens_per_sec"]},
    )
    return result


@router.post("/ai/chat")
async def chat(body: ChatRequest, request: Request, session: DbSession, audit: Audit) -> dict[str, Any]:
    service = _ai_service(request, session)
    try:
        result = await service.chat(
            [m.model_dump() for m in body.messages],
            profile=body.profile,
            complexity=body.complexity,
            options=body.options,
            context_len=body.context_len,
            wait_s=body.wait_s,
            think=body.think,
        )
    except (OllamaError, ResourcePressure, GovernorBusy, RoutingError, ValueError) as exc:
        raise _translate(exc) from exc
    audit.record("ai.chat", resource_type="ai_execution", resource_id=result["execution_id"])
    return result


@router.post("/ai/stream")
async def stream(body: GenerateRequest, request: Request, session: DbSession) -> StreamingResponse:
    service = _ai_service(request, session)

    async def events() -> AsyncIterator[bytes]:
        try:
            async for event in service.stream(
                body.prompt,
                profile=body.profile,
                complexity=body.complexity,
                options=body.options,
                context_len=body.context_len,
                wait_s=body.wait_s,
                think=body.think,
            ):
                yield (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        except (OllamaError, ResourcePressure, GovernorBusy, RoutingError) as exc:
            payload = {"type": "error", "code": getattr(exc, "code", "AI_ERROR"), "message": str(exc)}
            yield (json.dumps(payload) + "\n").encode("utf-8")

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------------------ executions
@router.get("/ai/executions", response_model=list[AiExecutionOut])
def executions(
    session: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
    state: str | None = None,
    model: str | None = None,
) -> list[AiExecutionOut]:
    from ...services.ai import execution_history

    rows = execution_history(session, limit=limit, state=state, model=model)
    return [AiExecutionOut.model_validate(row) for row in rows]


@router.post("/ai/executions/{execution_id}/cancel")
def cancel_execution(execution_id: str, request: Request, session: DbSession, audit: Audit) -> dict[str, Any]:
    from ...services.ai import AiService, ExecutionNotCancellable, ExecutionNotFound

    service = AiService(
        session, _gateway(request), _governor(request), None, getattr(request.app.state, "cancel_registry", None)
    )  # type: ignore[arg-type]
    try:
        live = service.cancel(execution_id)
    except ExecutionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND: execution not found") from exc
    except ExecutionNotCancellable as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"EXECUTION_NOT_CANCELLABLE: already {exc}"
        ) from exc
    audit.record("ai.execution.cancel", resource_type="ai_execution", resource_id=execution_id)
    return {"execution_id": execution_id, "cancelled_in_flight": live}


# ------------------------------------------------------------------- resources
@router.get("/ai/resources")
async def resources(request: Request, session: DbSession) -> dict[str, Any]:
    governor = _governor(request)
    try:
        queue_depth = session.scalar(
            sa.select(sa.func.count()).select_from(QueueJob).where(QueueJob.state == "queued")
        ) or 0
        governor.set_queue_depth(int(queue_depth))
    except sa.exc.SQLAlchemyError:
        pass
    snapshot = await governor.refresh()
    return {**snapshot.as_dict(), "stats": governor.stats(), "history": governor.history(30)}


@router.get("/ai/limits", response_model=LimitsModel)
def get_limits(request: Request) -> LimitsModel:
    return LimitsModel(**(_governor(request).limits.as_dict()))


@router.put("/ai/limits", response_model=LimitsModel)
def set_limits(body: LimitsModel, request: Request, session: DbSession, audit: Audit) -> LimitsModel:
    governor = _governor(request)
    from ...db.models import AppSetting

    limits = ResourceLimits.from_dict(body.model_dump(), base=governor.limits)
    governor.update_limits(limits)
    row = session.get(AppSetting, "resources.limits")
    if row is None:
        session.add(AppSetting(key="resources.limits", value_json=limits.as_dict(), scope="app"))
    else:
        row.value_json = limits.as_dict()
    audit.record("ai.limits.update", resource_type="settings", resource_id="resources.limits", metadata=limits.as_dict())
    return LimitsModel(**governor.limits.as_dict())


# -------------------------------------------------------------------- profiles
@router.get("/ai/profiles", response_model=list[ProfileModel])
def list_profiles(session: DbSession) -> list[ProfileModel]:
    registry = ModelRegistry(session)
    registry.ensure_seeded()
    return [ProfileModel.model_validate(p, from_attributes=True) for p in registry.profiles()]


@router.put("/ai/profiles/{name}", response_model=ProfileModel)
def update_profile(name: str, body: ProfileUpdate, session: DbSession, audit: Audit) -> ProfileModel:
    registry = ModelRegistry(session)
    try:
        profile = registry.update_profile(
            name,
            ollama_tag=body.ollama_tag,
            context_len=body.context_len,
            temperature=body.temperature,
        )
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND: profile not found") from exc
    audit.record("ai.profile.update", resource_type="model_profile", resource_id=name)
    return ProfileModel.model_validate(profile, from_attributes=True)


# --------------------------------------------------------------- durable jobs
@router.post("/ai/jobs", status_code=status.HTTP_202_ACCEPTED)
def enqueue_ai_job(body: GenerateRequest, session: DbSession, audit: Audit) -> dict[str, Any]:
    """Queue a generation through the durable queue (survives restarts)."""
    from ...services.queue import QueueService

    if not body.prompt.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "VALIDATION_FAILED: prompt is required")
    key = None
    if body.idempotency_key:
        from ...core.ids import new_idempotency_key

        key = new_idempotency_key("ai.generate", body.prompt, body.profile or "", body.idempotency_key)

    result = QueueService(session).enqueue(
        "ai.generate",
        {
            "prompt": body.prompt,
            "profile": body.profile,
            "complexity": body.complexity,
            "options": body.options,
            "context_len": body.context_len,
        },
        priority=body.priority,
        idempotency_key=key,
    )
    audit.record(
        "ai.job.enqueue",
        resource_type="queue_job",
        resource_id=result.job_id,
        metadata={"created": result.created},
    )
    return {"job_id": result.job_id, "created": result.created}


@router.get("/ai/jobs/{job_id}")
def ai_job_status(job_id: str, session: DbSession) -> dict[str, Any]:
    job = session.get(QueueJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND: job not found")
    execution = session.scalar(
        sa.select(AiExecution)
        .where(AiExecution.queue_job_id == job_id)
        .order_by(AiExecution.created_at.desc(), AiExecution.id.desc())
    )
    return {
        "job_id": job.id,
        "state": job.state.value if hasattr(job.state, "value") else str(job.state),
        "attempts": job.attempts,
        "last_error": job.last_error,
        "result": job.result_json,
        "execution_id": execution.id if execution else None,
        "execution_state": execution.state if execution else None,
    }
