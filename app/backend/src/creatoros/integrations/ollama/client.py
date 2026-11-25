"""Ollama HTTP gateway - the only path from CreatorOS to a model.

Design rules
------------
* One reusable async client, one place that knows Ollama's wire format.
* Every call is bounded by a timeout; streaming calls are cancellable.
* Model output is data, never instructions: nothing here executes, evaluates or
  shells out. There is no code path from a completion to a process or a secret.
* Metrics (TTFT, duration, tokens/second) are computed from Ollama's own
  counters rather than guessed, and are returned with every result.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Self

import httpx

from ...core.logging import get_logger, redact_value
from .errors import (
    GenerationCancelled,
    GenerationTimeout,
    ModelNotFound,
    OllamaBadResponse,
    OllamaError,
    OllamaUnavailable,
    classify_error_message,
    classify_http_error,
)

log = get_logger(__name__)


def _norm_tag(tag: str) -> str:
    value = (tag or "").strip().lower()
    if ":" not in value:
        return f"{value}:latest"
    return value


DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 300.0
PULL_READ_TIMEOUT = 120.0


@dataclass(slots=True)
class ModelSummary:
    tag: str
    size_bytes: int = 0
    digest: str = ""
    modified_at: str | None = None
    family: str | None = None
    parameter_size: str | None = None
    quantization: str | None = None
    format: str | None = None
    capabilities: list[str] = field(default_factory=list)

    @property
    def size_gb(self) -> float:
        return round(self.size_bytes / 1e9, 2)


@dataclass(slots=True)
class RunningModel:
    tag: str
    size_bytes: int
    size_vram_bytes: int
    context_length: int | None
    expires_at: str | None

    @property
    def vram_percent(self) -> float:
        if not self.size_bytes:
            return 0.0
        return round(self.size_vram_bytes / self.size_bytes * 100, 1)

    @property
    def fully_on_gpu(self) -> bool:
        """True when every layer is VRAM-resident (no CPU spill)."""
        return self.size_bytes > 0 and self.size_vram_bytes >= self.size_bytes


@dataclass(slots=True)
class GenerationMetrics:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_duration_ms: int = 0
    load_duration_ms: int = 0
    prompt_eval_duration_ms: int = 0
    eval_duration_ms: int = 0
    ttft_ms: int | None = None

    @property
    def tokens_per_sec(self) -> float:
        if not self.eval_duration_ms or not self.completion_tokens:
            return 0.0
        return round(self.completion_tokens / (self.eval_duration_ms / 1000), 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_duration_ms": self.total_duration_ms,
            "load_duration_ms": self.load_duration_ms,
            "prompt_eval_duration_ms": self.prompt_eval_duration_ms,
            "eval_duration_ms": self.eval_duration_ms,
            "ttft_ms": self.ttft_ms,
            "tokens_per_sec": self.tokens_per_sec,
        }


@dataclass(slots=True)
class GenerationResult:
    model: str
    response: str
    metrics: GenerationMetrics
    done_reason: str | None = None
    thinking: str = ""

    @property
    def thinking_only(self) -> bool:
        """True when the model produced reasoning but no visible answer."""
        return not self.response.strip() and bool(self.thinking.strip())


def default_think_for(model_tag: str) -> bool | None:
    """Qwen3 models reason by default, which can consume the whole token budget.

    For an interactive caption/drafting tool we want the answer, not the internal
    monologue, so thinking is off by default for qwen3 tags. Callers can override.
    """
    lowered = model_tag.lower()
    if lowered.startswith("qwen3"):
        return False
    return None


MIN_CONTEXT = 2048
MAX_CONTEXT = 32768
DEFAULT_CONTEXT = 4096


def num_ctx_for(requested: int | None) -> int:
    """Clamp a requested context window to what this class of GPU can hold.

    Above ~32k the KV cache alone exceeds the 4 GB card, and below 2k the models
    start dropping instructions, so both ends are bounded.
    """
    if not requested:
        return DEFAULT_CONTEXT
    return max(MIN_CONTEXT, min(MAX_CONTEXT, int(requested)))


def _ns_to_ms(value: Any) -> int:
    try:
        return int(int(value) / 1_000_000)
    except (TypeError, ValueError):
        return 0


class OllamaGateway:
    """Async client for the local Ollama daemon."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        *,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._read_timeout = read_timeout
        self._transport = transport
        # A client is bound to the loop that created it. The gateway is shared by
        # the API loop and the worker thread's loop, so keep one per loop.
        self._clients: dict[int, httpx.AsyncClient] = {}
        self._capabilities: dict[str, list[str]] = {}
        self._lock = None
        # Number of generate/chat/stream requests currently in flight on any loop.
        self._inflight = 0

    # ------------------------------------------------------------------ client
    async def _get_client(self) -> httpx.AsyncClient:
        loop_key = id(asyncio.get_running_loop())
        client = self._clients.get(loop_key)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self._read_timeout, connect=DEFAULT_CONNECT_TIMEOUT),
                transport=self._transport,
                headers={"Content-Type": "application/json"},
            )
            self._clients[loop_key] = client
        return client

    async def aclose(self) -> None:
        for key, client in list(self._clients.items()):
            if not client.is_closed:
                try:
                    await client.aclose()
                except RuntimeError:  # client belonged to a loop that is already gone
                    pass
            self._clients.pop(key, None)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ health
    async def health(self) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            client = await self._get_client()
            response = await client.get("/api/version", timeout=DEFAULT_CONNECT_TIMEOUT)
            if response.status_code != 200:
                raise classify_http_error(response.status_code, response.text)
            payload = response.json()
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise OllamaUnavailable("ollama is not reachable on the configured address", detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise OllamaUnavailable("ollama request failed", detail=str(exc)) from exc
        except json.JSONDecodeError as exc:
            raise OllamaBadResponse("ollama returned a non-JSON version response", detail=str(exc)) from exc

        return {
            "online": True,
            "version": payload.get("version"),
            "base_url": self.base_url,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    async def is_available(self) -> bool:
        try:
            await self.health()
        except OllamaError:
            return False
        return True

    # ------------------------------------------------------------------ models
    async def capabilities_for(self, tag: str) -> list[str]:
        """Capabilities need /api/show; cached for the process lifetime."""
        cached = self._capabilities.get(tag)
        if cached is not None:
            return cached
        try:
            info = await self.show_model(tag)
        except OllamaError:
            return []
        caps = list(info.get("capabilities") or [])
        self._capabilities[tag] = caps
        return caps

    async def list_models(self, *, enrich: bool = False) -> list[ModelSummary]:
        payload = await self._post_or_get("/api/tags", method="GET")
        models: list[ModelSummary] = []
        for item in payload.get("models", []):
            details = item.get("details") or {}
            models.append(
                ModelSummary(
                    tag=item.get("name") or item.get("model") or "",
                    size_bytes=int(item.get("size") or 0),
                    digest=item.get("digest") or "",
                    modified_at=item.get("modified_at"),
                    family=details.get("family"),
                    parameter_size=details.get("parameter_size"),
                    quantization=details.get("quantization_level"),
                    format=details.get("format"),
                    capabilities=list(item.get("capabilities") or []),
                )
            )
        if enrich:
            for model in models:
                if not model.capabilities:
                    model.capabilities = await self.capabilities_for(model.tag)
        return sorted(models, key=lambda m: m.tag)

    async def installed_tags(self) -> set[str]:
        return {model.tag for model in await self.list_models()}

    async def show_model(self, tag: str) -> dict[str, Any]:
        payload = await self._post_or_get("/api/show", method="POST", json_body={"model": tag})
        details = payload.get("details") or {}
        info = payload.get("model_info") or {}
        context_length = None
        parameter_count = None
        architecture = info.get("general.architecture")
        if architecture:
            context_length = info.get(f"{architecture}.context_length")
        for key, value in info.items():
            if key.endswith(".context_length") and context_length is None:
                context_length = value
        parameter_count = info.get("general.parameter_count")

        return {
            "tag": tag,
            "family": details.get("family"),
            "families": details.get("families") or [],
            "parameter_size": details.get("parameter_size"),
            "parameter_count": parameter_count,
            "quantization": details.get("quantization_level"),
            "format": details.get("format"),
            "architecture": architecture,
            "context_length": context_length,
            "embedding_length": info.get(f"{architecture}.embedding_length") if architecture else None,
            "block_count": info.get(f"{architecture}.block_count") if architecture else None,
            "capabilities": payload.get("capabilities") or [],
            "default_parameters": payload.get("parameters"),
            "modified_at": payload.get("modified_at"),
            "license_present": bool(payload.get("license")),
        }

    async def running_models(self) -> list[RunningModel]:
        payload = await self._post_or_get("/api/ps", method="GET")
        running: list[RunningModel] = []
        for item in payload.get("models", []):
            running.append(
                RunningModel(
                    tag=item.get("name") or item.get("model") or "",
                    size_bytes=int(item.get("size") or 0),
                    size_vram_bytes=int(item.get("size_vram") or 0),
                    context_length=item.get("context_length"),
                    expires_at=item.get("expires_at"),
                )
            )
        return running

    async def ensure_exclusive(self, model: str, *, wait_s: float = 90.0) -> dict[str, Any]:
        """Make `model` the only resident model before generating.

        On a 4 GiB Pascal GPU, having qwen3:1.7b resident while qwen3:4b loads kills
        llama-server outright (GPU OOM surfaces as a broken socket, not a clean error).
        Ollama's own eviction is not reliable enough here, so the gateway unloads the
        other models explicitly and waits until /api/ps agrees before the caller runs.
        """
        if self._inflight > 0:
            # Someone is generating right now; evicting underneath them is what causes
            # the llama-server drop we are trying to prevent.
            return {"model": model, "unloaded": [], "policy": "skipped_inflight", "waited_ms": 0}
        wanted = model.strip()
        unloaded: list[str] = []
        try:
            running = await self.running_models()
        except OllamaError:
            running = []
        for entry in running:
            if entry.tag and entry.tag != wanted and _norm_tag(entry.tag) != _norm_tag(wanted):
                try:
                    await self.unload_model(entry.tag)
                    unloaded.append(entry.tag)
                except OllamaError as exc:
                    log.warning("could not unload %s before loading %s: %s", entry.tag, wanted, exc)
        started = time.perf_counter()
        while (time.perf_counter() - started) < wait_s:
            try:
                still_running = await self.running_models()
            except OllamaError:
                break
            if not any(
                entry.tag and _norm_tag(entry.tag) != _norm_tag(wanted) for entry in still_running
            ):
                # /api/ps clearing does not mean the driver has released the VRAM yet;
                # loading the next model immediately succeeds roughly half the time.
                if unloaded:
                    await asyncio.sleep(1.5)
                break
            await asyncio.sleep(0.5)
        return {
            "model": wanted,
            "unloaded": unloaded,
            "waited_ms": int((time.perf_counter() - started) * 1000),
        }

    async def delete_model(self, tag: str) -> None:
        client = await self._get_client()
        try:
            response = await client.request("DELETE", "/api/delete", json={"model": tag})
        except httpx.HTTPError as exc:
            raise OllamaUnavailable("cannot reach ollama to delete the model", detail=str(exc)) from exc
        if response.status_code == 404:
            raise ModelNotFound(f"{tag} is not installed")
        if response.status_code >= 400:
            raise classify_http_error(response.status_code, response.text)

    async def unload_model(self, tag: str) -> bool:
        """Ask Ollama to evict a model from memory (keep_alive=0, no prompt)."""
        client = await self._get_client()
        try:
            response = await client.post(
                "/api/generate",
                json={"model": tag, "prompt": "", "keep_alive": 0, "stream": False},
            )
        except httpx.HTTPError as exc:
            raise OllamaUnavailable("cannot reach ollama to unload the model", detail=str(exc)) from exc
        if response.status_code >= 400:
            raise classify_http_error(response.status_code, response.text)
        remaining = {model.tag for model in await self.running_models()}
        return tag not in remaining

    # -------------------------------------------------------------------- pull
    async def pull_stream(self, tag: str) -> AsyncIterator[dict[str, Any]]:
        """Yield real pull progress events from Ollama.

        Yields dicts: {"status", "completed", "total", "percent", "digest"}.
        """
        client = await self._get_client()
        try:
            async with client.stream(
                "POST", "/api/pull", json={"model": tag, "stream": True}, timeout=PULL_READ_TIMEOUT
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise classify_http_error(response.status_code, body)
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "error" in event:
                        raise classify_error_message(str(event["error"]))
                    completed = int(event.get("completed") or 0)
                    total = int(event.get("total") or 0)
                    yield {
                        "status": event.get("status") or "pulling",
                        "completed": completed,
                        "total": total,
                        "percent": round(completed / total * 100, 1) if total else 0.0,
                        "digest": event.get("digest"),
                    }
        except httpx.HTTPError as exc:
            raise OllamaUnavailable("pull stream failed", detail=str(exc)) from exc

    # --------------------------------------------------------------- inference
    async def generate(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        options: dict[str, Any] | None = None,
        context_len: int | None = None,
        keep_alive: str | int | None = None,
        timeout_s: float | None = None,
        think: bool | None = None,
        response_format: str | None = None,
    ) -> GenerationResult:
        body: dict[str, Any] = {"model": model, "prompt": prompt, "stream": False}
        think = default_think_for(model) if think is None else think
        if think is not None:
            body["think"] = think
        if response_format:
            # Constrain decoding: without this a small model can loop until Ollama's
            # repeat guard aborts the prediction ("token repeat limit reached").
            body["format"] = response_format
        if system:
            body["system"] = system
        if options:
            body["options"] = dict(options)
        if context_len:
            body.setdefault("options", {})["num_ctx"] = num_ctx_for(context_len)
        if keep_alive is not None:
            body["keep_alive"] = keep_alive

        self._inflight += 1
        try:
            payload = await self._request("/api/generate", body, timeout_s=timeout_s)
        finally:
            self._inflight -= 1
        return GenerationResult(
            model=payload.get("model", model),
            response=payload.get("response", ""),
            metrics=self._metrics_from(payload),
            done_reason=payload.get("done_reason"),
            thinking=payload.get("thinking") or "",
        )

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        options: dict[str, Any] | None = None,
        context_len: int | None = None,
        timeout_s: float | None = None,
        think: bool | None = None,
        response_format: str | None = None,
    ) -> GenerationResult:
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        think = default_think_for(model) if think is None else think
        if think is not None:
            body["think"] = think
        if response_format:
            body["format"] = response_format
        if options:
            body["options"] = dict(options)
        if context_len:
            body.setdefault("options", {})["num_ctx"] = num_ctx_for(context_len)
        payload = await self._request("/api/chat", body, timeout_s=timeout_s)
        message = payload.get("message") or {}
        return GenerationResult(
            model=payload.get("model", model),
            response=message.get("content", ""),
            metrics=self._metrics_from(payload),
            done_reason=payload.get("done_reason"),
            thinking=message.get("thinking") or "",
        )

    async def stream_generate(
        self,
        model: str,
        prompt: str,
        *,
        options: dict[str, Any] | None = None,
        context_len: int | None = None,
        cancel: asyncio.Event | None = None,
        timeout_s: float | None = None,
        think: bool | None = None,
        response_format: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream tokens. Yields {"type":"token"|"thinking"|"done"|"cancelled", ...}."""
        body: dict[str, Any] = {"model": model, "prompt": prompt, "stream": True}
        think = default_think_for(model) if think is None else think
        if response_format:
            body["format"] = response_format
        if think is not None:
            body["think"] = think
        if options:
            body["options"] = dict(options)
        if context_len:
            body.setdefault("options", {})["num_ctx"] = num_ctx_for(context_len)
        self._inflight += 1
        try:
            async for event in self._stream("/api/generate", body, cancel=cancel, timeout_s=timeout_s, key="response"):
                yield event
        finally:
            self._inflight -= 1

    async def stream_chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        options: dict[str, Any] | None = None,
        context_len: int | None = None,
        cancel: asyncio.Event | None = None,
        timeout_s: float | None = None,
        response_format: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        if response_format:
            body["format"] = response_format
        if options:
            body["options"] = dict(options)
        if context_len:
            body.setdefault("options", {})["num_ctx"] = num_ctx_for(context_len)
        self._inflight += 1
        try:
            async for event in self._stream("/api/chat", body, cancel=cancel, timeout_s=timeout_s, key="message"):
                yield event
        finally:
            self._inflight -= 1

    # ------------------------------------------------------------------ helpers
    async def _post_or_get(
        self, path: str, *, method: str = "POST", json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        client = await self._get_client()
        try:
            if method == "GET":
                response = await client.get(path)
            else:
                response = await client.post(path, json=json_body or {})
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise OllamaUnavailable("ollama is not reachable", detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise OllamaUnavailable("ollama request failed", detail=str(exc)) from exc
        if response.status_code >= 400:
            raise classify_http_error(response.status_code, response.text)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise OllamaBadResponse(f"{path} returned invalid JSON", detail=str(exc)) from exc
        if not isinstance(payload, dict):
            raise OllamaBadResponse(f"{path} returned unexpected payload")
        return payload

    async def _request(self, path: str, body: dict[str, Any], *, timeout_s: float | None = None) -> dict[str, Any]:
        client = await self._get_client()
        timeout = timeout_s or self._read_timeout
        try:
            response = await asyncio.wait_for(client.post(path, json=body, timeout=timeout), timeout=timeout + 1.0)
        except TimeoutError as exc:
            raise GenerationTimeout(f"model did not answer within {timeout:.0f}s") from exc
        except httpx.ReadTimeout as exc:
            raise GenerationTimeout(f"model did not answer within {timeout:.0f}s", detail=str(exc)) from exc
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise OllamaUnavailable("ollama is not reachable", detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise OllamaUnavailable("ollama request failed", detail=str(exc)) from exc
        if response.status_code >= 400:
            raise classify_http_error(response.status_code, response.text)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise OllamaBadResponse(f"{path} returned invalid JSON", detail=str(exc)) from exc
        # Ollama reports some failures with HTTP 200 and an "error" field. Returning that
        # payload as a successful (empty) generation would silently fabricate a result.
        if isinstance(payload, dict) and payload.get("error"):
            raise classify_error_message(str(payload["error"]))
        return payload

    async def _stream(
        self,
        path: str,
        body: dict[str, Any],
        *,
        cancel: asyncio.Event | None,
        timeout_s: float | None,
        key: str,
    ) -> AsyncIterator[dict[str, Any]]:
        if cancel is not None and cancel.is_set():
            yield {"type": "cancelled", "partial": False, "metrics": GenerationMetrics().as_dict()}
            return
        client = await self._get_client()
        started = time.perf_counter()
        chunks = 0
        first_token_ms: int | None = None
        metrics = GenerationMetrics()

        try:
            async with client.stream(
                "POST", path, json=body, timeout=timeout_s or self._read_timeout
            ) as response:
                if response.status_code >= 400:
                    raw = (await response.aread()).decode("utf-8", "replace")
                    raise classify_http_error(response.status_code, raw)
                async for line in response.aiter_lines():
                    if cancel is not None and cancel.is_set():
                        yield {"type": "cancelled", "partial": chunks > 0, "metrics": metrics.as_dict()}
                        return
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("error"):
                        raise classify_error_message(str(event["error"]))
                    if key == "message":
                        text = (event.get("message") or {}).get("content", "")
                        thinking = (event.get("message") or {}).get("thinking", "")
                    else:
                        text = event.get("response", "")
                        thinking = event.get("thinking", "")
                    if thinking:
                        yield {"type": "thinking", "text": thinking}
                    if text:
                        if first_token_ms is None:
                            first_token_ms = int((time.perf_counter() - started) * 1000)
                        chunks += 1
                        yield {"type": "token", "text": text}
                    if event.get("done"):
                        metrics = self._metrics_from(event)
                        metrics.ttft_ms = first_token_ms
                        yield {"type": "done", "metrics": metrics.as_dict(), "done_reason": event.get("done_reason")}
                        return
        except httpx.ReadTimeout as exc:
            raise GenerationTimeout("stream stalled beyond the read timeout", detail=str(exc)) from exc
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise OllamaUnavailable("ollama is not reachable", detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            if cancel is not None and cancel.is_set():
                raise GenerationCancelled("generation cancelled by the user") from exc
            raise OllamaUnavailable("stream failed", detail=str(exc)) from exc

    @staticmethod
    def _metrics_from(payload: dict[str, Any]) -> GenerationMetrics:
        return GenerationMetrics(
            prompt_tokens=int(payload.get("prompt_eval_count") or 0),
            completion_tokens=int(payload.get("eval_count") or 0),
            total_duration_ms=_ns_to_ms(payload.get("total_duration")),
            load_duration_ms=_ns_to_ms(payload.get("load_duration")),
            prompt_eval_duration_ms=_ns_to_ms(payload.get("prompt_eval_duration")),
            eval_duration_ms=_ns_to_ms(payload.get("eval_duration")),
        )


def safe_error_payload(exc: Exception) -> dict[str, Any]:
    """Redacted, UI-safe description of a gateway failure."""
    if isinstance(exc, OllamaError):
        return redact_value(exc.as_dict())
    return {"code": "OLLAMA_ERROR", "message": redact_value(str(exc))}
