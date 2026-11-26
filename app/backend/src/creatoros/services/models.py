"""Model profiles, dynamic discovery and routing.

The router must never silently pick a model that is not installed, and must
always be able to explain its choice. Both properties are enforced here rather
than in the UI, so the API cannot claim something the machine cannot do.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ..core.logging import get_logger
from ..db.models import ModelBenchmark, ModelProfile
from ..integrations.ollama.client import ModelSummary, OllamaError, OllamaGateway
from .governor import ResourceGovernor, ResourceLimits

Complexity = Literal["low", "medium", "high"]


# Reference measurements taken on this machine (docs/ADR/README.md, ADR-09).
# They are seed data with provenance, never presented as live numbers.
REFERENCE_BENCHMARKS: tuple[dict[str, Any], ...] = (
    {
        "profile_name": "router_light",
        "model_tag": "qwen3:0.6b",
        "tokens_per_sec": 75.8,
        "context_len": 4096,
        "notes": "ADR-09 measurement, 2026-11-22, 100% GPU placement",
    },
    {
        "profile_name": "specialist",
        "model_tag": "qwen3:1.7b",
        "tokens_per_sec": 45.4,
        "context_len": 4096,
        "notes": "ADR-09 measurement, 2026-11-22, 100% GPU placement",
    },
    {
        "profile_name": "planner",
        "model_tag": "qwen3:4b",
        "tokens_per_sec": 15.5,
        "context_len": 4096,
        "notes": "ADR-09 measurement, 2026-11-22, 69% GPU placement (partial offload)",
    },
)

DEFAULT_PROFILES: tuple[dict[str, Any], ...] = (
    {
        "name": "router_light",
        "ollama_tag": "qwen3:0.6b",
        "context_len": 4096,
        "temperature": 0.2,
        "roles_json": ["classification", "routing", "extraction", "short_rewrite"],
        "fallback_profile": "specialist",
    },
    {
        "name": "specialist",
        "ollama_tag": "qwen3:1.7b",
        "context_len": 4096,
        "temperature": 0.7,
        "roles_json": ["caption", "hashtags", "drafting", "default_worker"],
        "fallback_profile": "router_light",
    },
    {
        "name": "planner",
        "ollama_tag": "qwen3:4b",
        # Measured on this machine (3.3 GiB usable VRAM): a 4B Q4_K_M model with a
        # 4096-token KV cache kills llama-server mid-request. 2048 is the largest
        # context that completes reliably, so the profile advertises what it can serve.
        "context_len": 2048,
        "temperature": 0.6,
        "roles_json": ["planning", "reasoning", "critique"],
        "fallback_profile": "specialist",
    },
    {
        "name": "coding",
        "ollama_tag": "qwen2.5-coder:3b",
        "context_len": 4096,
        "temperature": 0.2,
        "roles_json": ["code", "tests", "debugging"],
        "fallback_profile": "specialist",
    },
    {
        "name": "vision",
        "ollama_tag": "gemma3:4b",
        "context_len": 4096,
        "temperature": 0.4,
        "roles_json": ["image_understanding", "frame_analysis"],
        "fallback_profile": "planner",
    },
    {
        "name": "embedding",
        "ollama_tag": "nomic-embed-text",
        "context_len": 2048,
        "temperature": 0.0,
        "roles_json": ["embeddings", "semantic_search"],
        "fallback_profile": None,
    },
)

def normalise_tag(tag: str) -> str:
    """Ollama reports the implicit default tag as `name:latest`.

    Profiles are written without a tag ("nomic-embed-text"), so comparisons must
    normalise both sides or an installed model looks missing.
    """
    name = tag.strip()
    return name if ":" in name else f"{name}:latest"


COMPLEXITY_TO_PROFILE: dict[str, str] = {
    "low": "router_light",
    "medium": "specialist",
    "high": "planner",
}


class RoutingError(RuntimeError):
    code = "NO_MODEL_AVAILABLE"


@dataclass(slots=True)
class RouteDecision:
    profile: str
    model: str
    reason: str
    complexity: str
    context_len: int
    available: bool
    fallbacks_considered: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "model": self.model,
            "reason": self.reason,
            "complexity": self.complexity,
            "context_len": self.context_len,
            "available": self.available,
            "fallbacks_considered": self.fallbacks_considered,
            "warnings": self.warnings,
            "alternatives": self.alternatives,
        }


class ModelRegistry:
    """Seeds profiles/benchmarks and keeps them consistent with the database."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def ensure_seeded(self) -> None:
        existing = {p.name for p in self.session.scalars(sa.select(ModelProfile))}
        for spec in DEFAULT_PROFILES:
            if spec["name"] in existing:
                continue
            self.session.add(
                ModelProfile(
                    name=spec["name"],
                    ollama_tag=spec["ollama_tag"],
                    context_len=spec["context_len"],
                    temperature=spec["temperature"],
                    roles_json=spec["roles_json"],
                    fallback_profile=spec["fallback_profile"],
                )
            )
        # Reconcile profiles whose advertised context exceeds what the GPU can serve.
        caps = {"qwen3:4b": 2048}
        for row in self.session.scalars(sa.select(ModelProfile)):
            cap = caps.get(row.ollama_tag)
            if cap is not None and row.context_len > cap:
                get_logger(__name__).warning(
                    "lowering %s context from %s to %s: measured llama-server crashes above it on this GPU",
                    row.name,
                    row.context_len,
                    cap,
                )
                row.context_len = cap

        seeded_benchmarks = {
            (b.profile_name, b.tokens_per_sec) for b in self.session.scalars(sa.select(ModelBenchmark))
        }
        for bench in REFERENCE_BENCHMARKS:
            if (bench["profile_name"], bench["tokens_per_sec"]) in seeded_benchmarks:
                continue
            self.session.add(
                ModelBenchmark(
                    profile_name=bench["profile_name"],
                    tokens_per_sec=bench["tokens_per_sec"],
                    context_len=bench["context_len"],
                    notes=bench["notes"],
                    source="reference",
                )
            )
        self.session.commit()

    def profiles(self) -> list[ModelProfile]:
        return list(self.session.scalars(sa.select(ModelProfile).order_by(ModelProfile.name)))

    def profile(self, name: str) -> ModelProfile | None:
        return self.session.get(ModelProfile, name)

    def latest_benchmarks(self) -> dict[str, dict[str, Any]]:
        rows = self.session.scalars(
            # id desc breaks timestamp ties: two measurements can land in the same
            # second, and "latest" must still be the one that was written last.
            sa.select(ModelBenchmark)
            .order_by(ModelBenchmark.created_at.desc(), ModelBenchmark.id.desc())
            .limit(200)
        )
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            current = latest.get(row.profile_name)
            if current is not None and not (row.source == "measured" and current["source"] != "measured"):
                continue
            latest[row.profile_name] = {
                "tokens_per_sec": row.tokens_per_sec,
                "context_len": row.context_len,
                "measured_at": row.created_at.isoformat(),
                "notes": row.notes,
                "source": row.source,
            }
        return latest

    def set_benchmark(self, profile_name: str, tokens_per_sec: float, *, context_len: int, notes: str) -> None:
        self.session.add(
            ModelBenchmark(
                profile_name=profile_name,
                tokens_per_sec=round(tokens_per_sec, 2),
                context_len=context_len,
                notes=notes,
                source="measured",
            )
        )
        self.session.commit()

    def update_profile(self, name: str, *, ollama_tag: str | None, context_len: int | None, temperature: float | None) -> ModelProfile:
        profile = self.session.get(ModelProfile, name)
        if profile is None:
            raise KeyError(name)
        if ollama_tag is not None:
            profile.ollama_tag = ollama_tag
        if context_len is not None:
            profile.context_len = max(512, min(131072, int(context_len)))
        if temperature is not None:
            profile.temperature = max(0.0, min(2.0, float(temperature)))
        self.session.commit()
        return profile


class ModelRouter:
    """Chooses a model for a task, with an explicit, auditable reason."""

    def __init__(self, registry: ModelRegistry, governor: ResourceGovernor, gateway: OllamaGateway) -> None:
        self.registry = registry
        self.governor = governor
        self.gateway = gateway

    async def installed(self) -> dict[str, ModelSummary]:
        try:
            models = await self.gateway.list_models()
        except OllamaError:
            return {}
        # Keyed by normalised tag so "nomic-embed-text" and "nomic-embed-text:latest" match.
        return {normalise_tag(model.tag): model for model in models}

    async def route(
        self,
        complexity: Complexity = "medium",
        *,
        preferred_profile: str | None = None,
        needed_context: int | None = None,
        task_type: str | None = None,
    ) -> RouteDecision:
        limits: ResourceLimits = self.governor.limits
        installed = await self.installed()
        if not installed:
            raise RoutingError("no models are installed - pull a model in the Model Lab first")

        desired = preferred_profile or COMPLEXITY_TO_PROFILE.get(complexity, limits.default_profile)
        if task_type:
            for profile in self.registry.profiles():
                if task_type in (profile.roles_json or []):
                    desired = profile.name
                    break

        profiles = {p.name: p for p in self.registry.profiles()}
        warnings: list[str] = []
        considered: list[str] = []

        chain: list[str] = []
        cursor: str | None = desired
        while cursor and cursor not in chain and len(chain) < 6:
            chain.append(cursor)
            profile = profiles.get(cursor)
            cursor = profile.fallback_profile if profile else None

        chosen_profile = None
        chosen_model = None
        for name in chain:
            profile = profiles.get(name)
            considered.append(name)
            if profile is None:
                warnings.append(f"profile {name!r} is not configured")
                continue
            if normalise_tag(profile.ollama_tag) not in installed:
                warnings.append(f"{name}: {profile.ollama_tag} is not installed")
                continue
            if needed_context and profile.context_len < needed_context:
                warnings.append(
                    f"{name}: context {profile.context_len} is smaller than the required {needed_context}"
                )
                continue
            chosen_profile, chosen_model = profile, profile.ollama_tag
            break

        if chosen_profile is None:
            raise RoutingError(
                "none of the configured profiles has an installed model with enough context "
                f"(tried: {', '.join(considered)})"
            )

        reason = self._reason(desired, chosen_profile.name, complexity, preferred_profile)

        # Downgrade under memory pressure instead of failing outright.
        if chosen_profile.name != "router_light" and self._under_pressure():
            fallback = profiles.get(chosen_profile.fallback_profile or "router_light")
            if fallback and fallback.ollama_tag in installed:
                warnings.append(
                    f"downgraded from {chosen_profile.name} to {fallback.name} because memory headroom is low"
                )
                reason = f"memory pressure on this machine; using {fallback.name} instead of {chosen_profile.name}"
                chosen_profile, chosen_model = fallback, fallback.ollama_tag

        alternatives = [
            {
                "profile": p.name,
                "model": p.ollama_tag,
                "installed": normalise_tag(p.ollama_tag) in installed,
                "context_len": p.context_len,
                "roles": p.roles_json,
            }
            for p in self.registry.profiles()
            if p.name != chosen_profile.name
        ]

        return RouteDecision(
            profile=chosen_profile.name,
            model=chosen_model,
            reason=reason,
            complexity=complexity,
            context_len=chosen_profile.context_len,
            available=True,
            fallbacks_considered=considered,
            warnings=warnings,
            alternatives=alternatives,
        )

    def _under_pressure(self) -> bool:
        snapshot = self.governor.snapshot_cached()
        return snapshot.get("pressure") in ("critical", "saturated")

    @staticmethod
    def _reason(desired: str, chosen: str, complexity: str, preferred: str | None) -> str:
        if preferred:
            return f"you selected the {desired} profile"
        if desired == chosen:
            return f"{complexity} complexity maps to the {chosen} profile"
        return f"{complexity} complexity maps to {desired}, but {chosen} was chosen as the best available fallback"

    def preflight(self, model_tag: str, model_size_bytes: int) -> None:
        """Refuse to even attempt a load that the machine cannot hold."""
        snapshot = self.governor.snapshot_cached()
        size_gb = model_size_bytes / 1e9
        gpu = snapshot.get("gpu") or {}
        self.governor.require_capacity_for(
            size_gb,
            ram_free_gb=float(snapshot.get("ram_available_gb") or 0.0),
            vram_free_gb=float(gpu.get("memory_free_mb") or 0.0) / 1024 if gpu else 0.0,
        )


def now_utc() -> datetime:
    return datetime.now(UTC)
