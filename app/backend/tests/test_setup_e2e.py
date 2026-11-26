"""Process-level end-to-end tests for the first-run setup wizard.

These tests do not use FastAPI's TestClient. They run the harness in scripts/e2e_setup.py,
which starts the real application with "python -m creatoros serve" against a throwaway
CREATOROS_ROOT, hashes the repository's real pinned FFmpeg build, and drives the whole user
journey over HTTP. The only substituted values are the temporary data root, the loopback port
and the test-only CREATOROS_DISABLE_WORKERS / CREATOROS_SOCIAL_CALLBACK_ENABLED isolation
flags; no setup route, service, manifest or HTTP layer is mocked.

Run with the backend virtualenv:

    app/backend/.venv/Scripts/python.exe -m pytest tests/test_setup_e2e.py -q

The harness is also a standalone command:

    app/backend/.venv/Scripts/python.exe scripts/e2e_setup.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS_PATH = REPO_ROOT / "scripts" / "e2e_setup.py"


def _load_harness() -> ModuleType:
    if not HARNESS_PATH.is_file():
        pytest.fail(f"process-level E2E harness is missing: {HARNESS_PATH}")
    spec = importlib.util.spec_from_file_location("creatoros_e2e_setup", HARNESS_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - impossible for a real file
        pytest.fail(f"cannot load the process-level E2E harness: {HARNESS_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Register before execution: dataclasses resolves string annotations through
    # sys.modules[cls.__module__], which is absent for a bare exec_module().
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


@pytest.fixture(scope="module")
def harness() -> tuple[ModuleType, Path]:
    module = _load_harness()
    try:
        python = module.find_python(REPO_ROOT, None)
    except module.HarnessError as exc:
        # CI installs the backend dependencies into the runner's interpreter, not into
        # app/backend/.venv, so the process harness has no interpreter to launch. That is an
        # absent environment, not a product failure; the release machine has the venv.
        pytest.skip(str(exc))
    return module, python


def _run(harness: tuple[ModuleType, Path], journey_name: str) -> None:
    module, python = harness
    report = module.Report()
    journey = getattr(module, journey_name)
    try:
        journey(REPO_ROOT, python, report)
    except module.E2EFailure as exc:
        pytest.fail(f"process-level setup E2E failed after {len(report.steps)} checks: {exc}")
    except module.HarnessError as exc:  # pragma: no cover - environment problem
        pytest.fail(f"process-level setup E2E could not run: {exc}")
    assert report.ok, "the harness reported failure without raising"


def test_first_run_completion_is_refused_then_overridden_across_restart_and_upgrade(
    harness: tuple[ModuleType, Path],
) -> None:
    """409 on a failing required check, explicit override, restart persistence, version_changed.

    Also pins the API error contract the desktop wizard consumes: the 409 body carries
    code="REQUIRED_CHECKS_FAILING" and message="media.ffmpeg". See
    app/desktop/tests/setupE2E.test.mjs for the integration consequence of that shape.
    """
    _run(harness, "run_override_journey")


def test_first_run_completion_without_override_when_required_checks_pass(
    harness: tuple[ModuleType, Path],
) -> None:
    """Present-but-unverified -> verify the real pinned bytes -> complete without an override."""
    _run(harness, "run_happy_journey")
