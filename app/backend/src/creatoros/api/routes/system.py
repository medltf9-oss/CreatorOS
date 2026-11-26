"""System routes: health, diagnostics, settings and paths."""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import time
from typing import Any

import httpx
import psutil
import sqlalchemy as sa
from fastapi import APIRouter, HTTPException

from ...core.settings import get_settings
from ...db.models import AppSetting, DiagnosticsRun
from ...schemas.api import PathsInfo, SettingValue
from ..deps import Audit, DbSession

# Liveness must answer without a token so the shell can wait for the backend.
public = APIRouter(tags=["system"])
router = APIRouter(tags=["system"])
STARTED_AT = time.time()


@public.get("/health/live", summary="Liveness probe (no auth)")
def health_live() -> dict[str, Any]:
    settings = get_settings()
    return {"status": "live", "version": settings.version, "uptime_s": round(time.time() - STARTED_AT, 1)}


@router.get("/health/ready", summary="Readiness probe")
def health_ready(session: DbSession) -> dict[str, Any]:
    settings = get_settings()
    usage = shutil.disk_usage(settings.root)
    db_ok = True
    try:
        session.execute(sa.text("select 1"))
    except sa.exc.SQLAlchemyError:
        db_ok = False
    checks = {
        "root_exists": settings.root.is_dir(),
        "database": db_ok,
        "disk_free_gb": round(usage.free / 1e9, 1),
        "disk_ok": usage.free > 20 * 1e9,
    }
    status = "ready" if all([checks["root_exists"], db_ok, checks["disk_ok"]]) else "degraded"
    return {"status": status, "checks": checks}


def _gguf_models(url: str) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=4.0) as client:
            version = client.get(f"{url}/api/version").json()
            tags = client.get(f"{url}/api/tags").json()
        models = sorted(m.get("name", "") for m in tags.get("models", []))
        return {"online": True, "version": version.get("version"), "models": models, "model_count": len(models)}
    except (httpx.HTTPError, ValueError) as exc:
        return {"online": False, "error": type(exc).__name__, "detail": str(exc)}


def _ffmpeg_info(bin_dir) -> dict[str, Any]:
    exe = bin_dir / "ffmpeg.exe"
    if not exe.is_file():
        return {"present": False, "path": str(exe)}
    try:
        out = subprocess.run(
            [str(exe), "-hide_banner", "-version"], capture_output=True, text=True, timeout=15, check=False
        )
        lines = (out.stdout or out.stderr).splitlines()
        return {"present": True, "path": str(exe), "version": lines[0] if lines else ""}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"present": True, "path": str(exe), "error": str(exc)}


@router.get("/diagnostics")
def diagnostics(session: DbSession) -> dict[str, Any]:
    settings = get_settings()
    usage = shutil.disk_usage(settings.root)
    kinds = ("database", "media_incoming", "media_exports", "models", "logs", "temp", "config", "backups")
    payload = {
        "app": {"name": settings.app_name, "version": settings.version, "uptime_s": round(time.time() - STARTED_AT, 1)},
        "runtime": {
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "pid": psutil.Process().pid,
            "platform": platform.platform(),
        },
        "system": {
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "ram_total_gb": round(psutil.virtual_memory().total / 1e9, 1),
            "ram_available_gb": round(psutil.virtual_memory().available / 1e9, 1),
        },
        "disk": {
            "root": str(settings.root),
            "free_gb": round(usage.free / 1e9, 1),
            "total_gb": round(usage.total / 1e9, 1),
            "percent_used": round(usage.used / usage.total * 100, 1),
            "ok": usage.free > 20 * 1e9,
        },
        "paths": {kind: str(settings.path(kind)) for kind in kinds},  # type: ignore[arg-type]
        "ffmpeg": _ffmpeg_info(settings.ffmpeg_bin),
        "ollama": _gguf_models(settings.ollama_url),
    }
    session.add(DiagnosticsRun(results_json={"disk_ok": payload["disk"]["ok"]}, severity_summary="ok"))
    return payload


@router.get("/settings/paths", response_model=PathsInfo)
def settings_paths() -> PathsInfo:
    settings = get_settings()
    usage = shutil.disk_usage(settings.root)
    kinds = ("database", "media_incoming", "media_working", "media_exports", "models", "logs", "temp", "config", "backups")
    return PathsInfo(
        root=str(settings.root),
        paths={kind: str(settings.path(kind)) for kind in kinds},  # type: ignore[arg-type]
        free_gb=round(usage.free / 1e9, 1),
        total_gb=round(usage.total / 1e9, 1),
    )


@router.get("/settings", response_model=list[SettingValue])
def list_settings(session: DbSession) -> list[SettingValue]:
    rows = session.scalars(sa.select(AppSetting).order_by(AppSetting.key))
    return [SettingValue(key=row.key, value_json=row.value_json, scope=row.scope) for row in rows]


@router.put("/settings/{key}", response_model=SettingValue)
def upsert_setting(key: str, body: SettingValue, session: DbSession, audit: Audit) -> SettingValue:
    if not key.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise HTTPException(422, "setting key must be alphanumeric with - _ or .")
    row = session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value_json=body.value_json, scope=body.scope)
        session.add(row)
    else:
        row.value_json = body.value_json
        row.scope = body.scope
    audit.record("setting.upsert", resource_type="app_setting", resource_id=key, metadata={"scope": body.scope})
    return SettingValue(key=key, value_json=row.value_json, scope=row.scope)
