"""Typed errors for the Ollama gateway.

Every failure mode the UI must distinguish gets its own type, so the shell can
show an actionable message instead of "something went wrong".
"""
from __future__ import annotations


class OllamaError(RuntimeError):
    """Base class. `code` is stable and safe to show in the API envelope."""

    code = "OLLAMA_ERROR"
    http_status = 502

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail

    def as_dict(self) -> dict[str, str]:
        payload = {"code": self.code, "message": str(self)}
        if self.detail:
            payload["detail"] = self.detail
        return payload


class OllamaUnavailable(OllamaError):
    """Ollama is not running, or the socket refused / timed out."""

    code = "OLLAMA_UNAVAILABLE"
    http_status = 503


class ModelNotFound(OllamaError):
    """The requested tag is not installed locally."""

    code = "MODEL_NOT_FOUND"
    http_status = 404


class ModelLoadFailed(OllamaError):
    """The model could not be loaded (usually VRAM/RAM exhaustion)."""

    code = "MODEL_LOAD_FAILED"
    http_status = 507


class GenerationTimeout(OllamaError):
    code = "GENERATION_TIMEOUT"
    http_status = 504


class GenerationCancelled(OllamaError):
    code = "GENERATION_CANCELLED"
    http_status = 409


class OllamaBadResponse(OllamaError):
    """Ollama answered with something we cannot parse."""

    code = "OLLAMA_BAD_RESPONSE"
    http_status = 502


class ModelBusy(OllamaError):
    """The model is currently loaded/executing and the action was refused."""

    code = "MODEL_BUSY"
    http_status = 409


_OOM_MARKERS = (
    "out of memory",
    "out of vram",
    "insufficient memory",
    "memory required",
    "failed to allocate",
    "cannot allocate memory",
)


def classify_http_error(status: int, body: str) -> OllamaError:
    """Map an Ollama HTTP failure onto our error taxonomy."""
    lowered = (body or "").lower()
    if "not found" in lowered and "model" in lowered:
        return ModelNotFound("model is not installed", detail=body[:500])
    if any(marker in lowered for marker in _OOM_MARKERS):
        return ModelLoadFailed("not enough memory to load the model", detail=body[:500])
    if status == 404:
        return ModelNotFound("model is not installed", detail=body[:500])
    if status in (500, 503):
        return OllamaUnavailable("ollama reported a server error", detail=body[:500])
    return OllamaBadResponse(f"ollama returned HTTP {status}", detail=body[:500])


NOT_FOUND_MARKERS = ("not found", "does not exist", "no such model", "pull model manifest")
MEMORY_MARKERS = ("out of memory", "oom", "failed to allocate", "insufficient memory", "cuda error")
CANCEL_MARKERS = ("context canceled", "cancelled", "canceled")


def classify_error_message(message: str) -> OllamaError:
    """Classify an error *string* returned inside a 200 OK stream body.

    Ollama reports pull/load failures as `{"error": "..."}` events with HTTP 200,
    so status-code mapping alone would mislabel them.
    """
    lowered = message.lower()
    if any(marker in lowered for marker in NOT_FOUND_MARKERS):
        return ModelNotFound(message)
    if any(marker in lowered for marker in MEMORY_MARKERS):
        return ModelLoadFailed(message)
    if any(marker in lowered for marker in CANCEL_MARKERS):
        return GenerationCancelled(message)
    return OllamaBadResponse(message)
