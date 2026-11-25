"""Resource Governor - the component that keeps this laptop usable.

Why it exists: a 4 GB Pascal GPU running a 4B model at 4K context is already at
the edge of what the hardware can do. Two concurrent generations do not make the
machine faster; they make it swap, thermal-throttle and look broken. So:

* concurrency defaults to **1** and only rises if benchmarks justify it;
* admission is refused (not queued forever) when memory pressure is high;
* every decision is recorded with a reason so the UI can explain itself;
* telemetry degrades gracefully: no NVIDIA GPU means `gpu: null`, not a crash.

All measurements come from real sources: `psutil` for CPU/RAM and `nvidia-smi`
for GPU utilisation, memory and temperature. Nothing is estimated.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import psutil

from ..core.logging import get_logger, redact_value
from ..integrations.ollama.client import OllamaError, OllamaGateway

log = get_logger(__name__)

NVIDIA_SMI_TIMEOUT_S = 4.0
CPU_SAMPLE_TIMEOUT_S = 2.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """User-editable safety envelope. Defaults follow ADR-11."""

    max_concurrent_generations: int = 1
    max_ram_percent: float = 85.0
    min_free_ram_gb: float = 2.0
    max_vram_percent: float = 92.0
    max_queue_depth: int = 25
    generation_timeout_s: float = 300.0
    default_profile: str = "specialist"
    allow_concurrent_models: bool = False

    def validate(self) -> ResourceLimits:
        """Strict validation for user input: out-of-range values are rejected.

        Silently clamping a limit the user asked for would misrepresent what the
        system is actually doing, so the API surfaces these as 422s instead.
        """
        problems: list[str] = []
        if not 1 <= int(self.max_concurrent_generations) <= 4:
            problems.append("max_concurrent_generations must be between 1 and 4")
        if not 50.0 <= float(self.max_ram_percent) <= 98.0:
            problems.append("max_ram_percent must be between 50 and 98")
        if not 0.5 <= float(self.min_free_ram_gb) <= 32.0:
            problems.append("min_free_ram_gb must be between 0.5 and 32")
        if not 50.0 <= float(self.max_vram_percent) <= 100.0:
            problems.append("max_vram_percent must be between 50 and 100")
        if not 1 <= int(self.max_queue_depth) <= 500:
            problems.append("max_queue_depth must be between 1 and 500")
        if not 10.0 <= float(self.generation_timeout_s) <= 1800.0:
            problems.append("generation_timeout_s must be between 10 and 1800")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def clamped(self) -> ResourceLimits:
        """Tolerant variant for values coming from storage or defaults."""
        return replace(
            self,
            max_concurrent_generations=max(1, min(4, int(self.max_concurrent_generations))),
            max_ram_percent=max(50.0, min(98.0, float(self.max_ram_percent))),
            min_free_ram_gb=max(0.5, min(32.0, float(self.min_free_ram_gb))),
            max_vram_percent=max(50.0, min(100.0, float(self.max_vram_percent))),
            max_queue_depth=max(1, min(500, int(self.max_queue_depth))),
            generation_timeout_s=max(10.0, min(1800.0, float(self.generation_timeout_s))),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_concurrent_generations": self.max_concurrent_generations,
            "max_ram_percent": self.max_ram_percent,
            "min_free_ram_gb": self.min_free_ram_gb,
            "max_vram_percent": self.max_vram_percent,
            "max_queue_depth": self.max_queue_depth,
            "generation_timeout_s": self.generation_timeout_s,
            "default_profile": self.default_profile,
            "allow_concurrent_models": self.allow_concurrent_models,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, base: ResourceLimits | None = None) -> ResourceLimits:
        base = base or cls()
        return replace(
            base,
            max_concurrent_generations=int(data.get("max_concurrent_generations", base.max_concurrent_generations)),
            max_ram_percent=float(data.get("max_ram_percent", base.max_ram_percent)),
            min_free_ram_gb=float(data.get("min_free_ram_gb", base.min_free_ram_gb)),
            max_vram_percent=float(data.get("max_vram_percent", base.max_vram_percent)),
            max_queue_depth=int(data.get("max_queue_depth", base.max_queue_depth)),
            generation_timeout_s=float(data.get("generation_timeout_s", base.generation_timeout_s)),
            default_profile=str(data.get("default_profile", base.default_profile)),
            allow_concurrent_models=bool(data.get("allow_concurrent_models", base.allow_concurrent_models)),
        ).clamped()


class GovernorBusy(RuntimeError):
    """No inference slot available within the caller's patience."""

    code = "GOVERNOR_BUSY"


class ResourcePressure(RuntimeError):
    """Admission refused because memory headroom is below the configured floor."""

    code = "RESOURCE_PRESSURE"

    def __init__(self, message: str, *, snapshot: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.snapshot = snapshot or {}


@dataclass(slots=True)
class GovernorSnapshot:
    captured_at: datetime
    cpu_percent: float
    ram_total_gb: float
    ram_available_gb: float
    ram_percent: float
    gpu: dict[str, Any] | None
    loaded_models: list[dict[str, Any]]
    active_generations: int
    max_concurrent: int
    queue_depth: int
    limits: dict[str, Any]
    pressure: str
    reasons: list[str] = field(default_factory=list)
    telemetry_errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return redact_value(
            {
                "captured_at": self.captured_at.isoformat(),
                "cpu_percent": self.cpu_percent,
                "ram_total_gb": self.ram_total_gb,
                "ram_available_gb": self.ram_available_gb,
                "ram_percent": self.ram_percent,
                "gpu": self.gpu,
                "loaded_models": self.loaded_models,
                "active_generations": self.active_generations,
                "max_concurrent": self.max_concurrent,
                "queue_depth": self.queue_depth,
                "limits": self.limits,
                "pressure": self.pressure,
                "reasons": self.reasons,
                "telemetry_errors": self.telemetry_errors,
            }
        )


def read_nvidia_smi() -> tuple[dict[str, Any] | None, str | None]:
    """Query nvidia-smi once. Returns (gpu_dict, error_message)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None, "nvidia-smi not found on PATH"
    query = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"
    try:
        completed = subprocess.run(
            [exe, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=NVIDIA_SMI_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"nvidia-smi failed: {exc}"
    if completed.returncode != 0:
        return None, f"nvidia-smi exit {completed.returncode}"
    line = (completed.stdout or "").strip().splitlines()
    if not line:
        return None, "nvidia-smi returned no rows"
    parts = [part.strip() for part in line[0].split(",")]
    if len(parts) < 5:
        return None, "nvidia-smi returned an unexpected row"

    def _num(value: str) -> float | None:
        try:
            return float(value)
        except ValueError:
            return None

    used_mb = _num(parts[2]) or 0.0
    total_mb = _num(parts[3]) or 0.0
    return (
        {
            "name": parts[0],
            "utilization_percent": _num(parts[1]),
            "memory_used_mb": round(used_mb, 1),
            "memory_total_mb": round(total_mb, 1),
            "memory_free_mb": round(max(0.0, total_mb - used_mb), 1),
            "memory_percent": round(used_mb / total_mb * 100, 1) if total_mb else None,
            "temperature_c": _num(parts[4]) if len(parts) > 4 else None,
            "power_w": _num(parts[5]) if len(parts) > 5 else None,
        },
        None,
    )


class ResourceGovernor:
    """Samples the machine, tracks active generations and admits work.

    Concurrency is enforced with an explicit counter + condition (not an
    asyncio.Semaphore) so the limit can change at runtime without recreating the
    object, which is what "change limits in Settings" requires.
    """

    def __init__(
        self,
        gateway: OllamaGateway,
        limits: ResourceLimits | None = None,
        *,
        sample_callback=None,
        gpu_reader: Callable[[], tuple[dict[str, Any] | None, str | None]] | None = None,
    ) -> None:
        self._gateway = gateway
        self._limits = (limits or ResourceLimits()).clamped()
        self._active = 0
        # No asyncio primitives are stored here: admission is polled, and the
        # governor is shared between the API loop and the worker thread's loop.
        # Awaiting a Condition created in one loop from another raises
        # "bound to a different event loop" - which is exactly what it did.
        self._history: deque[dict[str, Any]] = deque(maxlen=120)
        self._last_gpu: dict[str, Any] | None = None
        self._last_gpu_error: str | None = None
        self._loaded: list[dict[str, Any]] = []
        self._queue_depth = 0
        self._sample_task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._sample_callback = sample_callback
        self._gpu_reader = gpu_reader or read_nvidia_smi
        self._sample_interval = 3.0
        self._stats = {"samples": 0, "refusals": 0, "waits": 0, "timeouts": 0}
        self.peak_ram_percent = 0.0
        self.peak_vram_percent = 0.0

    # ------------------------------------------------------------------ limits
    @property
    def limits(self) -> ResourceLimits:
        return self._limits

    def update_limits(self, limits: ResourceLimits) -> ResourceLimits:
        self._limits = limits.clamped()
        return self._limits

    # ------------------------------------------------------------------ sample
    def _sync_sample(self) -> dict[str, Any]:
        memory = psutil.virtual_memory()
        gpu, gpu_error = self._gpu_reader()
        self._last_gpu = gpu
        self._last_gpu_error = gpu_error
        sample = {
            "captured_at": _utcnow().isoformat(),
            "cpu_percent": psutil.cpu_percent(interval=None),
            "ram_total_gb": round(memory.total / 1e9, 2),
            "ram_available_gb": round(memory.available / 1e9, 2),
            "ram_percent": round(memory.percent, 1),
            "gpu": gpu,
            "gpu_error": gpu_error,
        }
        self._history.append(sample)
        self._stats["samples"] += 1
        self.peak_ram_percent = max(self.peak_ram_percent, sample["ram_percent"])
        if gpu and gpu.get("memory_percent"):
            self.peak_vram_percent = max(self.peak_vram_percent, gpu["memory_percent"])
        if self._sample_callback:
            try:
                self._sample_callback(sample)
            except Exception as exc:  # noqa: BLE001 - telemetry must never break inference
                log.warning("governor sample callback failed", extra={"error": redact_value(str(exc))})
        return sample

    async def refresh(self) -> GovernorSnapshot:
        """Take a fresh sample, including loaded models from Ollama."""
        psutil.cpu_percent(interval=None)  # prime the counters
        sample = await asyncio.to_thread(self._sync_sample)
        loaded: list[dict[str, Any]] = []
        try:
            for model in await self._gateway.running_models():
                loaded.append(
                    {
                        "tag": model.tag,
                        "size_gb": round(model.size_bytes / 1e9, 2),
                        "vram_gb": round(model.size_vram_bytes / 1e9, 2),
                        "vram_percent": model.vram_percent,
                        "context_length": model.context_length,
                        "expires_at": model.expires_at,
                    }
                )
        except OllamaError:
            loaded = []
        self._loaded = loaded
        return self._snapshot(sample)

    def _snapshot(self, sample: dict[str, Any]) -> GovernorSnapshot:
        reasons: list[str] = []
        pressure = "normal"
        if sample["ram_percent"] >= self._limits.max_ram_percent:
            pressure = "critical"
            reasons.append(
                f"RAM {sample['ram_percent']:.0f}% exceeds limit {self._limits.max_ram_percent:.0f}%"
            )
        elif sample["ram_available_gb"] <= self._limits.min_free_ram_gb:
            pressure = "critical"
            reasons.append(
                f"only {sample['ram_available_gb']:.1f} GB RAM free (floor {self._limits.min_free_ram_gb:.1f} GB)"
            )
        gpu = sample.get("gpu")
        if gpu and gpu.get("memory_percent") and gpu["memory_percent"] >= self._limits.max_vram_percent:
            pressure = "critical"
            reasons.append(
                f"VRAM {gpu['memory_percent']:.0f}% exceeds limit {self._limits.max_vram_percent:.0f}%"
            )
        if pressure == "normal" and self._active >= self._limits.max_concurrent_generations:
            pressure = "saturated"
            reasons.append(
                f"{self._active} active generation(s) at the configured limit "
                f"{self._limits.max_concurrent_generations}"
            )
        return GovernorSnapshot(
            captured_at=datetime.now(UTC),
            cpu_percent=sample["cpu_percent"],
            ram_total_gb=sample["ram_total_gb"],
            ram_available_gb=sample["ram_available_gb"],
            ram_percent=sample["ram_percent"],
            gpu=gpu,
            loaded_models=self._loaded,
            active_generations=self._active,
            max_concurrent=self._limits.max_concurrent_generations,
            queue_depth=self._queue_depth,
            limits=self._limits.as_dict(),
            pressure=pressure,
            reasons=reasons,
            telemetry_errors=[sample["gpu_error"]] if sample.get("gpu_error") else [],
        )

    def snapshot_cached(self) -> dict[str, Any]:
        sample = self._history[-1] if self._history else None
        if sample is None:
            sample = self._sync_sample()
        return self._snapshot(
            {
                "captured_at": sample["captured_at"],
                "cpu_percent": sample["cpu_percent"],
                "ram_total_gb": sample["ram_total_gb"],
                "ram_available_gb": sample["ram_available_gb"],
                "ram_percent": sample["ram_percent"],
                "gpu": self._last_gpu,
                "gpu_error": self._last_gpu_error,
            }
        ).as_dict()

    def history(self, limit: int = 60) -> list[dict[str, Any]]:
        return list(self._history)[-limit:]

    def set_queue_depth(self, depth: int) -> None:
        self._queue_depth = max(0, int(depth))

    @property
    def active_generations(self) -> int:
        return self._active

    def stats(self) -> dict[str, Any]:
        return {
            **self._stats,
            "active_generations": self._active,
            "peak_ram_percent": round(self.peak_ram_percent, 1),
            "peak_vram_percent": round(self.peak_vram_percent, 1),
            "limits": self._limits.as_dict(),
        }

    # --------------------------------------------------------------- admission
    async def acquire(self, *, wait_s: float = 0.0, label: str = "generation") -> None:
        """Reserve an inference slot.

        Raises ResourcePressure immediately when memory is already unsafe, and
        GovernorBusy when no slot frees up within `wait_s`.
        """
        deadline = time.monotonic() + max(0.0, wait_s)

        while True:
            snapshot = await self.refresh()
            if snapshot.pressure == "critical":
                self._stats["refusals"] += 1
                raise ResourcePressure("; ".join(snapshot.reasons), snapshot=snapshot.as_dict())
            if self._active < self._limits.max_concurrent_generations:
                self._active += 1
                return
            if time.monotonic() >= deadline:
                self._stats["timeouts"] += 1
                raise GovernorBusy(
                    f"no inference slot available; {self._active} active at limit "
                    f"{self._limits.max_concurrent_generations} ({label})"
                )
            self._stats["waits"] += 1
            await asyncio.sleep(0.25)

    async def release(self) -> None:
        self._active = max(0, self._active - 1)

    def require_capacity_for(self, model_size_gb: float, *, ram_free_gb: float, vram_free_gb: float) -> None:
        """Static pre-flight check before loading a model (used by the router)."""
        if ram_free_gb < model_size_gb + 1.0:
            raise ResourcePressure(
                f"needs about {model_size_gb + 1.0:.1f} GB RAM, {ram_free_gb:.1f} GB available"
            )
        if vram_free_gb and vram_free_gb < 0.5 and model_size_gb > 0.5:
            raise ResourcePressure(
                f"only {vram_free_gb:.1f} GB VRAM free; the model would run mostly on CPU"
            )

    # -------------------------------------------------------------- background
    async def start_sampling(self, interval: float = 3.0) -> None:
        if self._sample_task is not None and not self._sample_task.done():
            return
        self._sample_interval = max(0.05, interval)

        async def loop() -> None:
            while True:
                try:
                    await self.refresh()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - a sampling failure must not kill the app
                    log.warning("governor sampling failed", extra={"error": redact_value(str(exc))})
                await asyncio.sleep(self._sample_interval)

        self._sample_task = asyncio.create_task(loop(), name="creatoros-governor-sampler")

    async def stop_sampling(self) -> None:
        if self._sample_task is not None:
            self._sample_task.cancel()
            try:
                await self._sample_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                log.warning("governor sampler stopped with an error", extra={"error": redact_value(str(exc))})
            self._sample_task = None
        self._stop.set()
