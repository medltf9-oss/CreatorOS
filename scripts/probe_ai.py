"""Diagnostics probe for the Phase 3 AI layer.

Run by scripts/diagnose.ps1 so the check logic lives in Python (importable,
testable) instead of inline PowerShell.

    python scripts/probe_ai.py gateway     # daemon + models + residency
    python scripts/probe_ai.py profiles    # database: profiles, executions, benchmarks
    python scripts/probe_ai.py gpu         # nvidia-smi telemetry
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

BACKEND_SRC = Path(__file__).resolve().parents[1] / "app" / "backend" / "src"


def _root() -> Path:
    value = os.environ.get("CREATOROS_ROOT")
    if value:
        return Path(value)
    return Path(__file__).resolve().parents[1]


def gateway() -> int:
    sys.path.insert(0, str(BACKEND_SRC))
    from creatoros.integrations.ollama.client import OllamaGateway

    async def main() -> int:
        client = OllamaGateway(os.environ.get("CREATOROS_OLLAMA_URL", "http://127.0.0.1:11434"))
        try:
            health = await client.health()
            models = await client.list_models()
            running = await client.running_models()
        finally:
            await client.aclose()
        total_gb = round(sum(model.size_bytes for model in models) / 1e9, 2)
        print(
            f"online v{health['version']} models={len(models)} ({total_gb} GB) "
            f"loaded={len(running)} latency={health['latency_ms']}ms"
        )
        for model in models:
            placement = ""
            resident = next((item for item in running if item.tag == model.tag), None)
            if resident is not None:
                placement = f" loaded={resident.vram_percent}%gpu"
            print(f"  {model.tag:<26} {model.size_bytes / 1e9:6.2f} GB {model.quantization or '?':<8}{placement}")
        return 0

    return asyncio.run(main())


def profiles() -> int:
    database = _root() / "data" / "database" / "creatoros.db"
    if not database.exists():
        print(f"missing database: {database}", file=sys.stderr)
        return 1
    with sqlite3.connect(database) as con:
        profile_count = con.execute("select count(*) from model_profile").fetchone()[0]
        executions = con.execute("select count(*) from ai_execution").fetchone()[0]
        measured = con.execute("select count(*) from model_benchmark where source='measured'").fetchone()[0]
        reference = con.execute("select count(*) from model_benchmark where source='reference'").fetchone()[0]
        states = dict(con.execute("select state, count(*) from ai_execution group by state").fetchall())
    if profile_count == 0:
        print("no model profiles seeded", file=sys.stderr)
        return 1
    detail = ", ".join(f"{state}={count}" for state, count in sorted(states.items())) or "none"
    print(
        f"profiles={profile_count} executions={executions} ({detail}) "
        f"benchmarks measured={measured} reference={reference}"
    )
    return 0


def gpu() -> int:
    exe = shutil.which("nvidia-smi")
    if not exe:
        print("nvidia-smi not on PATH", file=sys.stderr)
        return 1
    query = "name,utilization.gpu,memory.used,memory.total,temperature.gpu"
    completed = subprocess.run(
        [exe, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        timeout=6,
        check=False,
    )
    row = (completed.stdout or "").strip().splitlines()
    if completed.returncode != 0 or not row:
        print(f"nvidia-smi failed: {(completed.stderr or '').strip()}", file=sys.stderr)
        return 1
    parts = [part.strip() for part in row[0].split(",")]
    if len(parts) < 5:
        print(f"nvidia-smi returned an unexpected row: {row[0]}", file=sys.stderr)
        return 1
    name, util, used, total, temp = parts[:5]
    try:
        percent = round(float(used) / float(total) * 100, 1)
    except (ValueError, ZeroDivisionError):
        percent = 0.0
    print(f"{name} util={util}% vram={used}/{total} MB ({percent}%) temp={temp}C")
    return 0


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "gateway"
    handlers = {"gateway": gateway, "profiles": profiles, "gpu": gpu}
    handler = handlers.get(command)
    if handler is None:
        print(f"unknown probe: {command} (expected one of {', '.join(handlers)})", file=sys.stderr)
        return 2
    return handler()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
