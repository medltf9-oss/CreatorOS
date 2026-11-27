"""Resource governor tests: limits, telemetry, admission control, pressure states."""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from creatoros.services.governor import (
    GovernorBusy,
    ResourceGovernor,
    ResourceLimits,
    ResourcePressure,
    read_nvidia_smi,
)
from tests.fakes import FakeGateway


class FakeSmi:
    """Deterministic nvidia-smi replacement so tests never depend on the host GPU."""

    def __init__(self, total_mb: int = 4096, used_mb: int = 1700, util: int = 42, temp: int = 45):
        self.total_mb = total_mb
        self.used_mb = used_mb
        self.util = util
        self.temp = temp
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return (
            {
                "name": "GTX 1050 Ti Max-Q (fake)",
                "total_mb": self.total_mb,
                "used_mb": self.used_mb,
                "free_mb": self.total_mb - self.used_mb,
                "memory_percent": round(self.used_mb / self.total_mb * 100, 1),
                "utilization_percent": self.util,
                "temperature_c": self.temp,
                "driver": "fake",
            },
            None,
        )


def missing_gpu():
    return None, "nvidia-smi is not available"


def make_governor(smi=None, gateway=None) -> ResourceGovernor:
    return ResourceGovernor(gateway or FakeGateway(), gpu_reader=smi or FakeSmi())


# ------------------------------------------------------------------ limits
def test_defaults_match_the_adr_safe_defaults():
    limits = ResourceLimits()
    assert limits.max_concurrent_generations == 1
    assert limits.max_ram_percent == 85
    assert limits.min_free_ram_gb == 2.0
    assert limits.generation_timeout_s == 300


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_concurrent_generations", 0),
        ("max_concurrent_generations", 9),
        ("max_ram_percent", 40),
        ("max_ram_percent", 99),
        ("min_free_ram_gb", 0.0),
        ("min_free_ram_gb", 40.0),
        ("generation_timeout_s", 1),
        ("generation_timeout_s", 100000),
    ],
)
def test_user_supplied_limits_out_of_range_are_rejected(field, value):
    with pytest.raises(ValueError):
        ResourceLimits(**{field: value}).validate()


def test_validation_message_names_the_offending_field():
    with pytest.raises(ValueError, match="max_concurrent_generations"):
        ResourceLimits(max_concurrent_generations=7).validate()


def test_stored_limits_are_clamped_instead_of_crashing_the_boot():
    assert ResourceLimits(max_concurrent_generations=99).clamped().max_concurrent_generations == 4


def test_limits_roundtrip_through_json():
    limits = ResourceLimits(max_concurrent_generations=2, generation_timeout_s=120)
    restored = ResourceLimits.from_dict(limits.as_dict())
    assert restored.max_concurrent_generations == 2
    assert restored.generation_timeout_s == 120


# --------------------------------------------------------------- telemetry
async def test_snapshot_reports_cpu_ram_and_gpu():
    snapshot = await make_governor().refresh()
    assert 0.0 <= snapshot.cpu_percent <= 100.0
    assert snapshot.ram_total_gb > 1.0
    assert snapshot.gpu is not None
    assert snapshot.gpu["total_mb"] == 4096
    assert snapshot.gpu["memory_percent"] == pytest.approx(41.5, abs=0.2)


async def test_missing_gpu_is_reported_as_absent_not_faked():
    snapshot = await make_governor(smi=missing_gpu).refresh()
    assert snapshot.gpu is None
    assert snapshot.telemetry_errors
    assert "nvidia-smi" in snapshot.telemetry_errors[0]


def test_nvidia_smi_parser_rejects_garbage(monkeypatch):
    class Broken:
        returncode = 0
        stdout = "not a csv row"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Broken())
    gpu, error = read_nvidia_smi()
    assert gpu is None
    assert error is None or isinstance(error, str)


def test_nvidia_smi_parser_handles_missing_binary(monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(subprocess, "run", boom)
    gpu, error = read_nvidia_smi()
    assert gpu is None
    assert "nvidia-smi" in error


# ------------------------------------------------------- admission control
async def test_only_one_generation_runs_by_default():
    governor = make_governor()
    await governor.acquire(wait_s=0)
    assert governor.active_generations == 1
    with pytest.raises(GovernorBusy):
        await governor.acquire(wait_s=0)
    await governor.release()
    assert governor.active_generations == 0
    await governor.acquire(wait_s=0)
    await governor.release()


async def test_waiting_request_gets_the_slot_when_released():
    governor = make_governor()
    await governor.acquire(wait_s=0)

    async def release_soon() -> None:
        await asyncio.sleep(0.2)
        await governor.release()

    releaser = asyncio.create_task(release_soon())
    await governor.acquire(wait_s=3.0)
    assert governor.active_generations == 1
    await governor.release()
    await releaser


async def test_raising_the_limit_takes_effect_without_recreating_the_governor():
    governor = make_governor()
    await governor.acquire(wait_s=0)
    with pytest.raises(GovernorBusy):
        await governor.acquire(wait_s=0)
    governor.update_limits(ResourceLimits(max_concurrent_generations=2))
    await governor.acquire(wait_s=0)
    assert governor.active_generations == 2
    await governor.release()
    await governor.release()


async def test_pressure_blocks_admission_when_ram_is_exhausted():
    gov = make_governor()
    gov.update_limits(ResourceLimits(min_free_ram_gb=32.0))
    snapshot = await gov.refresh()
    if snapshot.pressure != "critical":  # machine with >34 GB free would be unusual here
        pytest.skip("host has more free RAM than the artificial floor")
    with pytest.raises(ResourcePressure):
        await gov.acquire(wait_s=0)
    assert gov.active_generations == 0


async def test_pressure_state_is_reported_with_a_reason():
    gov = make_governor()
    await gov.acquire(wait_s=0)
    snapshot = gov.snapshot_cached()
    assert snapshot["pressure"] == "saturated"
    assert "active generation" in snapshot["reasons"][0]
    await gov.release()


async def test_require_capacity_for_rejects_oversized_requests():
    gov = make_governor()
    with pytest.raises(ResourcePressure):
        gov.require_capacity_for(64.0, ram_free_gb=1.0, vram_free_gb=4.0)
    with pytest.raises(ResourcePressure):
        gov.require_capacity_for(2.0, ram_free_gb=8.0, vram_free_gb=0.1)


async def test_require_capacity_accepts_a_model_that_fits():
    gov = make_governor()
    gov.require_capacity_for(1.7, ram_free_gb=8.0, vram_free_gb=3.0)


# --------------------------------------------------------------- history
async def test_history_and_peaks_track_samples():
    gov = make_governor(smi=FakeSmi(used_mb=3000))
    await gov.refresh()
    history = gov.history()
    assert history
    assert history[-1]["gpu"]["used_mb"] == 3000
    assert gov.stats()["peak_vram_percent"] > 0


async def test_sampler_runs_and_stops_cleanly():
    gov = make_governor()
    await gov.start_sampling(interval=0.05)
    await asyncio.sleep(0.25)
    await gov.stop_sampling()
    assert len(gov.history()) >= 2


async def test_cached_snapshot_never_calls_the_gpu_reader_twice():
    smi = FakeSmi()
    gov = make_governor(smi=smi)
    await gov.refresh()
    first = smi.calls
    for _ in range(3):
        gov.snapshot_cached()
    assert smi.calls == first
