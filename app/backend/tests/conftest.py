"""Test fixtures: every test runs against an isolated throwaway data root.

No test may touch a real `data` directory. The root is created under the OS
temp directory (`pytest-*`) and removed afterwards.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

BASE_TEMP = Path(os.environ.get("CREATOROS_TEST_TEMP") or tempfile.gettempdir())

# The repository ships its FFmpeg build under tools/ffmpeg/bin; tests exercise that build
# when it exists and otherwise leave CREATOROS_FFMPEG_DIR at whatever the environment set.
_REPO_FFMPEG = Path(__file__).resolve().parents[3] / "tools" / "ffmpeg" / "bin"
FFMPEG_DIR = str(_REPO_FFMPEG) if _REPO_FFMPEG.is_dir() else os.environ.get("CREATOROS_FFMPEG_DIR", "")


def _reset_caches() -> None:
    from creatoros.core import settings as settings_mod
    from creatoros.db import session as session_mod

    settings_mod.get_settings.cache_clear()
    settings_mod.resolve_root.cache_clear()
    session_mod.reset_engine()


@pytest.fixture(scope="session", autouse=True)
def _isolated_root() -> Iterator[Path]:
    root = BASE_TEMP / f"pytest-{uuid.uuid4().hex[:10]}"
    root.mkdir(parents=True, exist_ok=True)
    keys = (
        "CREATOROS_ROOT",
        "CREATOROS_DISABLE_WORKERS",
        "CREATOROS_DISABLE_AI",
        "CREATOROS_FFMPEG_DIR",
        "CREATOROS_IPC_SECRET",
        "CREATOROS_SOCIAL_CALLBACK_ENABLED",
        "CREATOROS_VAULT_URL",
        "CREATOROS_VAULT_TOKEN",
    )
    previous = {key: os.environ.get(key) for key in keys}
    os.environ["CREATOROS_ROOT"] = str(root)
    os.environ["CREATOROS_DISABLE_WORKERS"] = "1"
    # Unit/API tests never talk to a real model runtime; live checks live in
    # tests/test_ai_integration.py behind CREATOROS_RUN_LIVE_AI=1.
    os.environ.setdefault("CREATOROS_DISABLE_AI", "1")
    # Media tests must exercise the real FFmpeg binary, but their outputs stay inside the
    # throwaway root. No shell signs import requests here, so no IPC secret is configured.
    os.environ["CREATOROS_FFMPEG_DIR"] = FFMPEG_DIR
    os.environ.pop("CREATOROS_IPC_SECRET", None)
    # Phase 6: tests never bind the fixed OAuth callback port (a developer machine may be
    # running the real app). The listener is exercised directly on an ephemeral port instead.
    os.environ["CREATOROS_SOCIAL_CALLBACK_ENABLED"] = "0"
    # No broker exists in tests: the real vault client must stay unavailable, and the social
    # tests install an explicit in-memory double where they need one.
    os.environ.pop("CREATOROS_VAULT_URL", None)
    os.environ.pop("CREATOROS_VAULT_TOKEN", None)
    _reset_caches()

    # Build the schema through the real migration path, not create_all.
    from alembic import command
    from alembic.config import Config

    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    command.upgrade(config, "head")

    yield root

    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _reset_caches()
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def _clean_tables(_isolated_root):
    """Each test starts from an empty schema; the schema itself is built once."""
    yield
    import sqlalchemy as sa

    from creatoros.db.base import Base
    from creatoros.db.session import session_scope

    with session_scope() as db:
        db.execute(sa.text("PRAGMA foreign_keys=OFF"))
        # A migration test may leave this root on an older schema version, where a table the current
        # models know about does not exist yet. Deleting only what is really there keeps the shared
        # teardown independent of test order.
        existing = {
            row[0]
            for row in db.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            )
        }
        for table in reversed(Base.metadata.sorted_tables):
            if table.name in existing:
                db.execute(sa.delete(table))
    # Rows are gone; delete the media bytes too, or the next test sees files whose rows
    # were removed (and storage assertions silently depend on test order).
    from creatoros.core.settings import get_settings

    media_root = get_settings().root / "data" / "media"
    if media_root.exists():
        for child in media_root.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
    _reset_caches()


@pytest.fixture()
def session(_isolated_root):
    from creatoros.db.session import session_scope

    with session_scope() as db:
        yield db


@pytest.fixture()
def vault():
    """In-memory vault double, installed into the social routes for the duration of a test."""
    from social_fixtures import InMemoryVault

    from creatoros.api.routes.social import set_vault

    double = InMemoryVault()
    set_vault(double)
    yield double
    set_vault(None)


class ListenerStub:
    """Stands in for the loopback listener: `running` is what the engine checks."""

    running = True

    def __init__(self, port: int = 47821) -> None:
        self.port = port
        self.requests = 0

    def status(self) -> dict:
        return {
            "running": True,
            "port": self.port,
            "url_base": f"http://127.0.0.1:{self.port}/oauth/callback/",
            "requests": self.requests,
        }

    def start(self) -> int:
        return self.port

    def stop(self) -> None:
        return None


@pytest.fixture()
def oauth_listener(monkeypatch):
    """Install a listener stub into the module the engine reads it from."""
    import creatoros.services.social.callback_server as callback_module

    stub = ListenerStub()
    monkeypatch.setattr(callback_module, "_listener", stub)
    return stub


@pytest.fixture()
def token() -> str:
    return "test-session-token"


@pytest.fixture(autouse=True)
def _session_token_env(token, monkeypatch):
    """The lifespan mints the token from settings; pin it so clients can auth."""
    monkeypatch.setenv("CREATOROS_SESSION_TOKEN", token)
    from creatoros.core import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    yield
    settings_mod.get_settings.cache_clear()


@pytest.fixture()
def client(_isolated_root, token):
    from fastapi.testclient import TestClient

    from creatoros.main import app

    with TestClient(app, raise_server_exceptions=False) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {token}"})
        yield test_client


@pytest.fixture()
def unauth_client(_isolated_root, token):
    from fastapi.testclient import TestClient

    from creatoros.main import app

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture()
def asset_factory(session):
    from creatoros.db.models import MediaAsset

    counter = {"n": 0}

    def make(**overrides):
        counter["n"] += 1
        digest = overrides.pop("sha256", f"{counter['n']:064x}")
        asset = MediaAsset(
            source_path=overrides.pop("source_path", f"<DATA_ROOT>\\data\\media\\incoming\\clip{counter['n']}.mp4"),
            original_filename=overrides.pop("original_filename", f"clip{counter['n']}.mp4"),
            sha256=digest,
            size_bytes=overrides.pop("size_bytes", 1024),
            **overrides,
        )
        session.add(asset)
        session.flush()
        return asset

    return make


# Phase 5 media fixtures live in their own module so the generated binaries and the
# helpers stay together; importing the names here makes them available to every test.
pytest_plugins = ("media_fixtures",)
