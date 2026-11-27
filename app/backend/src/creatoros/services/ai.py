"""AI execution service: real inference with persistence, cancellation and limits.

Every call follows the same lifecycle so nothing is lost or misreported:

    create ai_execution (state=running)  ->  governor.acquire()
    ->  gateway call (with cancel event)  ->  persist text + metrics
    ->  state=succeeded | cancelled | failed  ->  governor.release()

A crash mid-flight leaves a `running` row, which the UI shows as interrupted.
That is deliberate: pretending the work finished is worse than showing a gap.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..core.logging import get_logger, redact_value
from ..db.models import AiExecution
from ..integrations.ollama.client import (
    GenerationCancelled,
    GenerationMetrics,
    OllamaError,
    OllamaGateway,
)
from ..integrations.ollama.errors import OllamaUnavailable
from .governor import GovernorBusy, ResourceGovernor, ResourcePressure
from .models import ModelRouter, RoutingError


def _clamp_context(requested: int | None, profile_context: int) -> int:
    """Never ask a model for more context than its profile can serve.

    A 4B model on 3.3 GiB of VRAM cannot hold a 4096-token KV cache; requesting it kills
    llama-server instead of returning a clean error. The profile is the authority.
    """
    if not requested:
        return profile_context
    return max(1024, min(requested, profile_context))


DEFAULT_OPTIONS: dict[str, Any] = {"temperature": 0.7, "top_p": 0.95, "num_predict": 512}

# Cancellation events must be visible to *other* HTTP requests, so the registry is
# process-wide (the app injects app.state.cancel_registry; this is the fallback).
_CANCEL_REGISTRY: dict[str, asyncio.Event] = {}


class ExecutionNotFound(KeyError):
    code = "EXECUTION_NOT_FOUND"


class ExecutionNotCancellable(RuntimeError):
    """The execution already reached a terminal state; nothing to cancel."""

    code = "EXECUTION_NOT_CANCELLABLE"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AiService:
    def __init__(
        self,
        session: Session,
        gateway: OllamaGateway,
        governor: ResourceGovernor,
        router: ModelRouter,
        cancel_registry: dict[str, asyncio.Event] | None = None,
    ) -> None:
        self.session = session
        self.gateway = gateway
        self.governor = governor
        self.router = router
        self._cancel: dict[str, asyncio.Event] = cancel_registry if cancel_registry is not None else _CANCEL_REGISTRY

    # ------------------------------------------------------------------ helpers
    def _create_execution(
        self,
        *,
        kind: str,
        model: str,
        profile: str | None,
        origin: str,
        prompt: str,
        context_len: int,
        options: dict[str, Any],
        queue_job_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AiExecution:
        execution = AiExecution(
            kind=kind,
            model_tag=model,
            profile_name=profile,
            state="running",
            origin=origin,
            prompt_text=prompt[:8000],
            prompt_hash=_hash(prompt),
            context_len=context_len,
            options_json=redact_value(options),
            queue_job_id=queue_job_id,
            correlation_id=correlation_id,
        )
        self.session.add(execution)
        self.session.commit()
        return execution

    def _finish(
        self,
        execution: AiExecution,
        *,
        state: str,
        text: str | None = None,
        metrics: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> AiExecution:
        execution.state = state
        execution.finished_at = _utcnow()
        if text is not None:
            execution.response_text = text
        if metrics:
            execution.prompt_tokens = int(metrics.get("prompt_tokens") or 0)
            execution.completion_tokens = int(metrics.get("completion_tokens") or 0)
            execution.duration_ms = int(metrics.get("total_duration_ms") or 0)
            execution.ttft_ms = metrics.get("ttft_ms")
            execution.tokens_per_sec = metrics.get("tokens_per_sec")
            execution.usage_json = redact_value(metrics)
        if error_code:
            execution.error_code = error_code
        if error_detail:
            execution.error_detail = redact_value(error_detail)[:4000]
        self.session.commit()
        return execution

    def _register_cancel(self, execution_id: str) -> asyncio.Event:
        event = asyncio.Event()
        self._cancel[execution_id] = event
        return event

    def _drop_cancel(self, execution_id: str) -> None:
        self._cancel.pop(execution_id, None)

    def cancel(self, execution_id: str) -> bool:
        """Request cancellation of an in-flight execution. True if one was live."""
        execution = self.session.get(AiExecution, execution_id)
        if execution is None:
            raise ExecutionNotFound(execution_id)
        if execution.state not in ("running", "queued"):
            raise ExecutionNotCancellable(execution.state)
        event = self._cancel.get(execution_id)
        if event is not None:
            event.set()
        execution.cancel_requested = True
        self.session.commit()
        return event is not None

    # --------------------------------------------------------------- inference
    async def generate(
        self,
        prompt: str,
        *,
        profile: str | None = None,
        complexity: str = "medium",
        options: dict[str, Any] | None = None,
        context_len: int | None = None,
        origin: str = "interactive",
        system: str | None = None,
        queue_job_id: str | None = None,
        correlation_id: str | None = None,
        wait_s: float = 60.0,
        think: bool | None = None,
        response_format: str | None = None,
    ) -> dict[str, Any]:
        decision = await self.router.route(complexity, preferred_profile=profile, needed_context=context_len)
        merged_options = {**DEFAULT_OPTIONS, **(options or {})}
        ctx = _clamp_context(context_len, decision.context_len)

        execution = self._create_execution(
            kind="generate",
            model=decision.model,
            profile=decision.profile,
            origin=origin,
            prompt=prompt,
            context_len=ctx,
            options=merged_options,
            queue_job_id=queue_job_id,
            correlation_id=correlation_id,
        )

        started = time.perf_counter()
        try:
            await self.governor.acquire(wait_s=wait_s, label=f"execution:{execution.id}")
        except (ResourcePressure, GovernorBusy) as exc:
            self._finish(execution, state="refused", error_code=exc.code, error_detail=str(exc))
            raise

        try:
            gpu = await self._prepare_gpu(decision.model, correlation_id=correlation_id)
            if gpu.get("unloaded"):
                if execution.usage_json is None:
                    execution.usage_json = {}
                execution.usage_json = {**execution.usage_json, "gpu_switch": gpu}
                self.session.add(execution)
                self.session.commit()
            result = await self._generate_with_switch_retry(
                decision.model,
                prompt,
                system=system,
                options=merged_options,
                ctx=ctx,
                think=think,
                response_format=response_format,
                switched=bool(gpu.get("unloaded")),
            )
            metrics = {**result.metrics.as_dict(), "wall_ms": int((time.perf_counter() - started) * 1000)}
            if result.thinking_only:
                # Reasoning models can burn the entire token budget inside `thinking`
                # and return an empty answer. Reporting success here would be a lie.
                detail = (
                    "the model produced reasoning but no visible answer; increase num_predict "
                    "or disable thinking (think=false)"
                )
                self._finish(
                    execution,
                    state="failed",
                    text="",
                    metrics=metrics,
                    error_code="THINKING_BUDGET_EXHAUSTED",
                    error_detail=detail,
                )
                return {
                    "execution_id": execution.id,
                    "model": result.model,
                    "profile": decision.profile,
                    "reason": decision.reason,
                    "response": "",
                    "thinking": result.thinking,
                    "metrics": metrics,
                    "warnings": [*decision.warnings, detail],
                    "incomplete": True,
                }
            self._finish(execution, state="succeeded", text=result.response, metrics=metrics)
            return {
                "execution_id": execution.id,
                "model": result.model,
                "profile": decision.profile,
                "reason": decision.reason,
                "response": result.response,
                "thinking": result.thinking,
                "metrics": metrics,
                "warnings": decision.warnings,
            }
        except OllamaError as exc:
            detail = getattr(exc, "detail", None)
            self._finish(
                execution,
                state="failed",
                error_code=exc.code,
                error_detail=(f"{exc} | {detail}" if detail else str(exc))[:800],
            )
            raise
        finally:
            await self.governor.release()



    async def _generate_with_switch_retry(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None,
        options: dict[str, Any],
        ctx: int,
        think: bool | None,
        response_format: str | None,
        switched: bool,
    ) -> Any:
        """Retry exactly once when a model switch destabilises llama-server.

        Swapping a resident 1.7B for a 4B on this GPU makes llama-server drop the socket
        about half the time. One retry after the VRAM settles recovers it; a second
        failure is reported as a real failure, never papered over.
        """
        attempts = 3 if switched else 1
        last_error: Exception | None = None
        for index in range(attempts):
            try:
                return await self.gateway.generate(
                    model,
                    prompt,
                    system=system,
                    options=options,
                    context_len=ctx,
                    timeout_s=self.governor.limits.generation_timeout_s,
                    think=think,
                    response_format=response_format,
                )
            except OllamaUnavailable as exc:
                last_error = exc
                if index + 1 >= attempts:
                    break
                # Swapping the resident model makes llama-server drop the socket often
                # enough that one immediate retry is not enough. Each retry is logged,
                # and exhausting them raises the real provider error.
                delay = 1.5 * (index + 1)
                get_logger(__name__).warning(
                    "retrying %s after a model switch (attempt %d/%d, waiting %.1fs)",
                    model,
                    index + 2,
                    attempts,
                    delay,
                )
                await asyncio.sleep(delay)
                ensure = getattr(self.gateway, "ensure_exclusive", None)
                if ensure is not None:
                    await ensure(model)
        assert last_error is not None
        raise last_error

    async def _prepare_gpu(self, model: str, *, correlation_id: str | None = None) -> dict[str, Any]:
        """Evict other resident models before loading this one (see ADR-17).

        On a 3.3 GiB usable GPU, overlapping a 1.7B and a 4B model kills llama-server.
        The policy is on by default and can be disabled with CREATOROS_EXCLUSIVE_MODEL=0.
        """
        if os.environ.get("CREATOROS_EXCLUSIVE_MODEL", "1") == "0":
            return {"policy": "disabled"}
        ensure = getattr(self.gateway, "ensure_exclusive", None)
        if ensure is None:  # test doubles and future gateways may not implement it
            return {"policy": "unsupported"}
        try:
            info = await ensure(model)
        except Exception as exc:  # noqa: BLE001 - never fail a generation on eviction trouble
            get_logger(__name__).warning("exclusive model policy failed for %s: %s", model, exc)
            return {"policy": "error", "error": str(exc)[:200]}
        info["policy"] = "exclusive"
        if info.get("unloaded"):
            get_logger(__name__).info(
                "unloaded %s before loading %s (%s ms)",
                ", ".join(info["unloaded"]),
                model,
                info.get("waited_ms"),
                extra={"correlation_id": correlation_id},
            )
        return info

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        profile: str | None = None,
        complexity: str = "medium",
        options: dict[str, Any] | None = None,
        context_len: int | None = None,
        origin: str = "interactive",
        wait_s: float = 60.0,
        think: bool | None = None,
        response_format: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        if not messages:
            raise ValueError("at least one message is required")
        prompt = "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages)
        decision = await self.router.route(complexity, preferred_profile=profile, needed_context=context_len)
        merged_options = {**DEFAULT_OPTIONS, **(options or {})}
        ctx = _clamp_context(context_len, decision.context_len)

        execution = self._create_execution(
            kind="chat",
            model=decision.model,
            profile=decision.profile,
            origin=origin,
            prompt=prompt,
            context_len=ctx,
            options=merged_options,
        )
        started = time.perf_counter()
        try:
            await self.governor.acquire(wait_s=wait_s, label=f"chat:{execution.id}")
        except (ResourcePressure, GovernorBusy) as exc:
            self._finish(execution, state="refused", error_code=exc.code, error_detail=str(exc))
            raise

        try:
            await self._prepare_gpu(decision.model, correlation_id=correlation_id)
            result = await self.gateway.chat(
                decision.model,
                messages,
                options=merged_options,
                context_len=ctx,
                timeout_s=self.governor.limits.generation_timeout_s,
                think=think,
                response_format=response_format,
            )
            metrics = {**result.metrics.as_dict(), "wall_ms": int((time.perf_counter() - started) * 1000)}
            self._finish(execution, state="succeeded", text=result.response, metrics=metrics)
            return {
                "execution_id": execution.id,
                "model": result.model,
                "profile": decision.profile,
                "reason": decision.reason,
                "response": result.response,
                "metrics": metrics,
                "warnings": decision.warnings,
            }
        except OllamaError as exc:
            detail = getattr(exc, "detail", None)
            self._finish(
                execution,
                state="failed",
                error_code=exc.code,
                error_detail=(f"{exc} | {detail}" if detail else str(exc))[:800],
            )
            raise
        finally:
            await self.governor.release()

    async def stream(
        self,
        prompt: str,
        *,
        profile: str | None = None,
        complexity: str = "medium",
        options: dict[str, Any] | None = None,
        context_len: int | None = None,
        wait_s: float = 60.0,
        think: bool | None = None,
        response_format: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """NDJSON stream: meta, token*, thinking*, done | cancelled | error."""
        decision = await self.router.route(complexity, preferred_profile=profile, needed_context=context_len)
        merged_options = {**DEFAULT_OPTIONS, **(options or {})}
        ctx = _clamp_context(context_len, decision.context_len)
        execution = self._create_execution(
            kind="generate",
            model=decision.model,
            profile=decision.profile,
            origin="stream",
            prompt=prompt,
            context_len=ctx,
            options=merged_options,
        )
        cancel = self._register_cancel(execution.id)
        yield {
            "type": "meta",
            "execution_id": execution.id,
            "model": decision.model,
            "profile": decision.profile,
            "reason": decision.reason,
            "context_len": ctx,
            "warnings": decision.warnings,
        }
        try:
            await self.governor.acquire(wait_s=wait_s, label=f"stream:{execution.id}")
        except (ResourcePressure, GovernorBusy) as exc:
            self._finish(execution, state="refused", error_code=exc.code, error_detail=str(exc))
            yield {"type": "error", "code": exc.code, "message": str(exc)}
            return

        collected: list[str] = []
        try:
            await self._prepare_gpu(decision.model)
            async for event in self.gateway.stream_generate(
                decision.model,
                prompt,
                options=merged_options,
                context_len=ctx,
                cancel=cancel,
                timeout_s=self.governor.limits.generation_timeout_s,
                think=think,
                response_format=response_format,
            ):
                if event["type"] == "thinking":
                    yield {"type": "thinking", "text": event["text"]}
                elif event["type"] == "token":
                    collected.append(event["text"])
                    yield {"type": "token", "text": event["text"]}
                elif event["type"] == "done":
                    metrics = event["metrics"]
                    self._finish(execution, state="succeeded", text="".join(collected), metrics=metrics)
                    yield {"type": "done", "execution_id": execution.id, "metrics": metrics}
                elif event["type"] == "cancelled":
                    self._finish(
                        execution,
                        state="cancelled",
                        text="".join(collected),
                        metrics=event.get("metrics"),
                        error_code="GENERATION_CANCELLED",
                    )
                    yield {"type": "cancelled", "execution_id": execution.id, "partial": "".join(collected)}
        except GenerationCancelled:
            self._finish(
                execution, state="cancelled", text="".join(collected), error_code="GENERATION_CANCELLED"
            )
            yield {"type": "cancelled", "execution_id": execution.id, "partial": "".join(collected)}
        except OllamaError as exc:
            self._finish(execution, state="failed", text="".join(collected), error_code=exc.code, error_detail=str(exc))
            yield {"type": "error", "code": exc.code, "message": str(exc)}
        finally:
            self._drop_cancel(execution.id)
            await self.governor.release()

    # ------------------------------------------------------------------ history
    def history(self, *, limit: int = 50, state: str | None = None, model: str | None = None) -> list[AiExecution]:
        import sqlalchemy as sa

        # Windows clock resolution is ~15 ms, so two executions created back to back can
        # share a timestamp; the monotonic ULID is the deterministic tiebreaker.
        stmt = (
            sa.select(AiExecution)
            .order_by(AiExecution.created_at.desc(), AiExecution.id.desc())
            .limit(min(limit, 200))
        )
        if state:
            stmt = stmt.where(AiExecution.state == state)
        if model:
            stmt = stmt.where(AiExecution.model_tag == model)
        return list(self.session.scalars(stmt))

    def get(self, execution_id: str) -> AiExecution:
        execution = self.session.get(AiExecution, execution_id)
        if execution is None:
            raise ExecutionNotFound(execution_id)
        return execution

    def mark_interrupted(self) -> int:
        """On boot: rows left `running` by a crash are not silently 'succeeded'."""
        import sqlalchemy as sa

        result = self.session.execute(
            sa.update(AiExecution)
            .where(AiExecution.state == "running")
            .values(
                state="interrupted",
                error_code="APP_RESTARTED",
                error_detail="the application stopped before this generation finished",
                finished_at=_utcnow(),
            )
        )
        self.session.commit()
        return int(result.rowcount or 0)


def execution_history(session: Session, *, limit: int = 50, state: str | None = None, model: str | None = None):
    """Read execution rows without constructing a full service (no gateway needed)."""
    import sqlalchemy as sa

    # Windows clock resolution is ~15 ms, so two executions created back to back can
    # share a timestamp; the monotonic ULID is the deterministic tiebreaker.
    stmt = (
        sa.select(AiExecution)
        .order_by(AiExecution.created_at.desc(), AiExecution.id.desc())
        .limit(min(limit, 200))
    )
    if state:
        stmt = stmt.where(AiExecution.state == state)
    if model:
        stmt = stmt.where(AiExecution.model_tag == model)
    return list(session.scalars(stmt))


# --------------------------------------------------------------------- queue glue
def build_ai_job_handler(
    session_factory,
    gateway: OllamaGateway,
    governor: ResourceGovernor,
    router_factory,
):
    """Handler for the durable `ai.generate` job type.

    Runs the same service the interactive path uses, so queue execution cannot
    drift from interactive behaviour.
    """

    def handler(session: Session, job) -> dict[str, Any]:
        prompt = str(job.payload.get("prompt", "")).strip()
        if not prompt:
            raise ValueError("ai.generate requires a non-empty prompt")

        # The queue worker is synchronous; run the async pipeline in its own loop.
        async def run() -> dict[str, Any]:
            service = AiService(session, gateway, governor, router_factory(session))
            return await service.generate(
                prompt,
                profile=job.payload.get("profile"),
                complexity=str(job.payload.get("complexity", "medium")),
                options=job.payload.get("options"),
                context_len=job.payload.get("context_len"),
                origin="queue",
                queue_job_id=job.id,
                correlation_id=job.payload.get("correlation_id"),
                wait_s=float(job.payload.get("wait_s", 120.0)),
                think=job.payload.get("think"),
            )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(run())

        # Already inside a loop (async worker): run in a dedicated thread loop.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, run()).result()

    return handler


__all__ = [
    "AiService",
    "ExecutionNotCancellable",
    "ExecutionNotFound",
    "GenerationMetrics",
    "GovernorBusy",
    "ResourcePressure",
    "RoutingError",
    "build_ai_job_handler",
    "execution_history",
]
