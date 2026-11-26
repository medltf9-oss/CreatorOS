"""Model registry + router tests.

The router is the component that decides which model answers a request, so it is
tested for the properties we promise: never pick an uninstalled model, always
explain the choice, degrade instead of failing, and never reach for a cloud API.
"""
from __future__ import annotations

import pytest

from creatoros.services.governor import ResourceGovernor, ResourceLimits
from creatoros.services.models import (
    DEFAULT_PROFILES,
    ModelRegistry,
    ModelRouter,
    RoutingError,
    normalise_tag,
)
from tests.fakes import FakeGateway


class FakeSmi:
    def __call__(self):
        return (
            {
                "name": "fake",
                "total_mb": 4096,
                "used_mb": 1200,
                "free_mb": 2896,
                "memory_percent": 29.3,
                "utilization_percent": 5,
                "temperature_c": 40,
                "driver": "fake",
            },
            None,
        )


def make_router(session, *, tags: list[str] | None = None, online: bool = True) -> ModelRouter:
    gateway = FakeGateway(online=online)
    if tags is not None:
        from creatoros.integrations.ollama.client import ModelSummary

        gateway.models = [
            ModelSummary(tag=tag, size_bytes=1_000_000_000, digest="d" * 64, modified_at=None)
            for tag in tags
        ]
    registry = ModelRegistry(session)
    registry.ensure_seeded()
    governor = ResourceGovernor(gateway, gpu_reader=FakeSmi())
    return ModelRouter(registry, governor, gateway)


ALL_TAGS = [
    "qwen3:0.6b",
    "qwen3:1.7b",
    "qwen3:4b",
    "gemma3:4b",
    "qwen2.5-coder:3b",
    "nomic-embed-text:latest",
]


# ---------------------------------------------------------------- registry
def test_seeding_creates_the_documented_profiles(session):
    registry = ModelRegistry(session)
    registry.ensure_seeded()
    names = {profile.name for profile in registry.profiles()}
    assert names == {spec["name"] for spec in DEFAULT_PROFILES}


def test_seeding_is_idempotent(session):
    registry = ModelRegistry(session)
    registry.ensure_seeded()
    count = len(registry.profiles())
    registry.ensure_seeded()
    assert len(registry.profiles()) == count


def test_reference_benchmarks_carry_their_provenance(session):
    registry = ModelRegistry(session)
    registry.ensure_seeded()
    latest = registry.latest_benchmarks()
    assert latest
    for profile_name, row in latest.items():
        assert row["tokens_per_sec"] > 0
        assert "ADR-09" in row["notes"], f"{profile_name} benchmark has no provenance"


def test_measured_benchmark_supersedes_the_reference_value(session):
    registry = ModelRegistry(session)
    registry.ensure_seeded()
    registry.set_benchmark("specialist", 61.5, context_len=4096, notes="measured in test")
    row = registry.latest_benchmarks()["specialist"]
    assert row["tokens_per_sec"] == pytest.approx(61.5)
    assert row["notes"] == "measured in test"


def test_profile_update_changes_the_tag_and_is_validated(session):
    registry = ModelRegistry(session)
    registry.ensure_seeded()
    updated = registry.update_profile("specialist", ollama_tag="gemma3:4b", context_len=8192, temperature=0.4)
    assert updated.ollama_tag == "gemma3:4b"
    assert updated.context_len == 8192
    with pytest.raises(KeyError):
        registry.update_profile("nope", ollama_tag="x", context_len=None, temperature=None)


# ------------------------------------------------------------------ router
async def test_light_profile_for_simple_tasks(session):
    decision = await make_router(session, tags=ALL_TAGS).route("low")
    assert decision.profile == "router_light"
    assert decision.model == "qwen3:0.6b"
    assert decision.reason


async def test_complex_tasks_route_to_the_planner(session):
    decision = await make_router(session, tags=ALL_TAGS).route("high")
    assert decision.profile == "planner"
    assert decision.model == "qwen3:4b"


async def test_explicit_profile_wins_over_complexity(session):
    decision = await make_router(session, tags=ALL_TAGS).route("low", preferred_profile="coding")
    assert decision.profile == "coding"
    assert decision.model == "qwen2.5-coder:3b"


async def test_router_never_returns_an_uninstalled_model(session):
    router = make_router(session, tags=["qwen3:0.6b"])
    decision = await router.route("high")
    assert decision.model == "qwen3:0.6b"
    assert any("not installed" in warning for warning in decision.warnings)
    assert "fallback" in decision.reason.lower() or decision.profile == "router_light"


async def test_router_explains_a_missing_preferred_profile(session):
    router = make_router(session, tags=["qwen3:1.7b"])
    decision = await router.route("low", preferred_profile="vision")
    assert decision.model == "qwen3:1.7b"
    assert any("vision" in warning for warning in decision.warnings)


async def test_router_fails_loudly_when_nothing_is_installed(session):
    router = make_router(session, tags=[])
    with pytest.raises(RoutingError, match="no models are installed"):
        await router.route("low")


async def test_router_fails_loudly_when_the_daemon_is_offline(session):
    router = make_router(session, online=False)
    with pytest.raises(RoutingError):
        await router.route("low")


async def test_router_enforces_a_minimum_context_window(session):
    router = make_router(session, tags=ALL_TAGS)
    with pytest.raises(RoutingError, match="enough context"):
        await router.route("low", needed_context=999_999)
    decision = await router.route("low", needed_context=2048)
    assert decision.context_len >= 2048


async def test_router_downgrades_under_memory_pressure(session):
    router = make_router(session, tags=ALL_TAGS)
    router.governor.update_limits(ResourceLimits(min_free_ram_gb=32.0))
    await router.governor.refresh()
    if router.governor.snapshot_cached()["pressure"] != "critical":
        pytest.skip("host has enough free RAM that the artificial floor is not reached")
    decision = await router.route("high")
    assert decision.profile == "specialist"  # one step down planner -> specialist
    assert decision.model == "qwen3:1.7b"
    assert any("downgraded" in warning for warning in decision.warnings)


async def test_router_honours_a_task_type_mapping(session):
    decision = await make_router(session, tags=[*ALL_TAGS, "nomic-embed-text"]).route(
        "medium", task_type="embeddings"
    )
    assert decision.profile == "embedding"
    assert decision.model.startswith("nomic-embed-text")


async def test_decision_serialises_with_its_audit_trail(session):
    decision = await make_router(session, tags=ALL_TAGS).route("medium")
    payload = decision.as_dict()
    for key in ("model", "profile", "reason", "complexity", "context_len", "warnings", "alternatives"):
        assert key in payload
    assert payload["alternatives"]
    assert {"profile", "model", "installed", "context_len", "roles"} <= set(payload["alternatives"][0])


def test_preflight_rejects_a_model_that_cannot_fit(session):
    router = make_router(session, tags=ALL_TAGS)
    router.preflight("qwen3:1.7b", 1_700_000_000)  # fits in the fake 4 GB card
    from creatoros.services.governor import ResourcePressure

    with pytest.raises(ResourcePressure):
        router.preflight("huge:70b", 40_000_000_000)


def test_no_cloud_provider_is_reachable_from_the_router_module():
    import inspect

    from creatoros.services import models as models_module

    source = inspect.getsource(models_module).lower()
    for forbidden in ("openai", "anthropic", "api.openai.com", "generativelanguage"):
        assert forbidden not in source


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("nomic-embed-text", "nomic-embed-text:latest"),
        ("nomic-embed-text:latest", "nomic-embed-text:latest"),
        ("qwen3:1.7b", "qwen3:1.7b"),
        ("  gemma3:4b  ", "gemma3:4b"),
    ],
)
def test_tags_are_normalised_so_implicit_latest_still_matches(raw, expected):
    assert normalise_tag(raw) == expected


async def test_embedding_profile_matches_the_installed_latest_tag(session):
    decision = await make_router(session, tags=["nomic-embed-text:latest"]).route(
        "low", preferred_profile="embedding"
    )
    assert decision.profile == "embedding"
    assert decision.model == "nomic-embed-text"
