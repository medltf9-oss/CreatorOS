"""Phase 5 acceptance measurements on the real machine. Prints facts, invents nothing."""
from __future__ import annotations

import json
import shutil
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend" / "src"))

from creatoros.core.settings import get_settings, resolve_root
from creatoros.db.session import session_scope
from creatoros.services.media.ffmpeg import FfmpegFailed, FfmpegRunner, choose_h264_encoder
from creatoros.services.media.library import MediaLibrary, library_supports
from creatoros.services.media.probe import ProbeEngine

ROOT = resolve_root()
WORK = ROOT / "temp" / "p5-bench"
WORK.mkdir(parents=True, exist_ok=True)
FFMPEG = get_settings().ffmpeg_bin / "ffmpeg.exe"


def make_source() -> Path:
    target = WORK / "source-1080p.mp4"
    if target.exists():
        return target
    import subprocess

    subprocess.run(
        [
            str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=20",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-shortest", str(target),
        ],
        check=True,
    )
    return target


def drop(library, asset, kind: str, profile: str | None = None) -> None:
    """Remove a derivative (row + file) so the next run is a real encode, not a cache hit."""
    from creatoros.db.models import MediaDerivative

    query = library.session.query(MediaDerivative).filter_by(asset_id=asset.id, kind=kind)
    if profile:
        query = query.filter_by(profile=profile)
    for row in query.all():
        if row.rel_path:
            try:
                library.derivative_file(row).unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001 - a missing file is exactly what we want here
                print(f"cleanup: {exc}")
        library.session.delete(row)
    library.session.commit()


def timed(fn, repeats: int = 1) -> dict[str, object]:
    samples = []
    result = None
    for _ in range(repeats):
        started = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - started) * 1000)
    return {"median_ms": round(statistics.median(samples), 1), "runs": repeats, "result": result}


def main() -> int:
    print("== capabilities (measured now) ==")
    print(json.dumps(library_supports(), indent=2, default=str))

    print("\n== encoder selection, forced re-check ==")
    started = time.perf_counter()
    encoder = choose_h264_encoder(force_recheck=True)
    print(f"{encoder.name} hardware={encoder.hardware} verified={encoder.verified} in {(time.perf_counter() - started) * 1000:.0f} ms")
    print(f"detail: {encoder.detail}")

    print("\n== nvenc rejection, verbatim ==")
    runner = FfmpegRunner()
    try:
        runner.run(
            [
                "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=1",
                "-c:v", "h264_nvenc", "-f", "null", "-",
            ],
            timeout_s=30,
        )
        print("h264_nvenc: SUCCEEDED")
    except FfmpegFailed as exc:
        print(f"h264_nvenc: FAILED (returncode={exc.returncode})")
        print("\n".join(str(exc).splitlines()[:3]))

    source = make_source()
    probe = ProbeEngine()

    print("\n== probe ==")
    probed = timed(lambda: probe.probe(source), repeats=5)
    print(f"median {probed['median_ms']} ms over {probed['runs']} runs")
    facts = probed["result"].as_dict()
    print(json.dumps({k: facts[k] for k in ("kind", "duration_ms", "video", "audio", "container") if k in facts}, indent=2, default=str)[:1400])

    with session_scope() as session:
        library = MediaLibrary(session)
        print("\n== import (20 s 1080p) ==")
        from creatoros.db.models import MediaAsset

        for stale in session.query(MediaAsset).filter_by(original_filename=source.name).all():
            for row in library.derivatives(stale.id):
                if row.rel_path:
                    try:
                        library.derivative_file(row).unlink(missing_ok=True)
                    except Exception as exc:  # noqa: BLE001 - bench cleanup is best effort
                        print(f"cleanup: {exc}")
                session.delete(row)
            try:
                library.asset_file(stale).unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                print(f"cleanup: {exc}")
            session.delete(stale)
        session.commit()
        print(f"source is {source.stat().st_size / 1024 / 1024:.1f} MB")
        imported = timed(lambda: library.import_file(source, auto_thumbnail=False))
        print(f"import median {imported['median_ms']} ms -> {imported['result'].status}")
        asset = library.get_asset(imported["result"].asset_id)
        duration_s = (asset.duration_ms or 0) / 1000

        print("\n== derivatives ==")
        thumb = timed(lambda: library.ensure_thumbnail(asset, force=True), repeats=3)
        row = thumb["result"][0]
        print(f"thumbnail  {thumb['median_ms']:8.0f} ms  {row.width}x{row.height}  {row.size_bytes / 1024:.0f} KB")
        drop(library, asset, "timeline")
        timeline = timed(lambda: library.ensure_timeline(asset, count=8))
        print(f"timeline   {timeline['median_ms']:8.0f} ms  {len(timeline['result'])} frames")
        drop(library, asset, "waveform")
        wave = timed(lambda: library.ensure_waveform(asset, buckets=400))
        print(f"waveform   {wave['median_ms']:8.0f} ms  {wave['result'][0].size_bytes} bytes of peaks")

        print("\n== transcodes ==")
        for profile_id in ("fast_preview", "editing_proxy", "normalized", "audio_wav", "audio_aac"):
            drop(library, asset, {"fast_preview": "preview", "editing_proxy": "proxy", "normalized": "transcode", "audio_wav": "audio", "audio_aac": "audio"}[profile_id], profile_id)
            result = timed(lambda pid=profile_id: library.transcode(asset, pid))
            produced = result["result"]
            speed = duration_s / (result["median_ms"] / 1000) if result["median_ms"] else 0
            dimensions = f"{produced.width}x{produced.height}" if produced.width else "audio"
            print(
                f"{profile_id:14s} {result['median_ms']:8.0f} ms  {dimensions:10s} "
                f"{produced.size_bytes / 1024:7.0f} KB  {speed:5.1f}x realtime"
            )

        print("\n== duplicate import (SHA-256 hit) ==")
        duplicate = timed(lambda: library.import_file(source))
        print("duplicate path hashes but does not copy")
        print(f"duplicate import {duplicate['median_ms']} ms -> {duplicate['result'].status}")

        print("\n== disk ==")
        usage = shutil.disk_usage(ROOT)
        print(f"free {usage.free / 1024**3:.1f} GB of {usage.total / 1024**3:.1f} GB")
        session.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
