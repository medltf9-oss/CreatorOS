"""The media library: import, probe, derivatives and verification.

This module is the only writer of ``media_asset``/``media_derivative`` rows and of files
under ``data/media``. It is used by the queue handlers (background) and by the API (for
read-only work and re-probing), and it never touches the network.

Invariants enforced here:

* An original is written once, from a hash-derived name, and never modified afterwards.
* A derivative is only marked ``ready`` after the file exists, is non-empty, and re-probes
  as the expected kind. A failed verification deletes the partial file and fails the job.
* Asking for the same derivative twice is a cache hit: the unique key
  ``(asset, kind, profile, version)`` makes that structural rather than a convention.
* Anything unreadable is quarantined with a reason instead of being reported as imported.

Every operation accepts ``progress``/``should_cancel`` callbacks so the queue handler can
report real progress and stop on request without this module knowing about the queue.
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...core.logging import get_logger
from ...core.settings import get_settings
from ...db.enums import DerivativeKind, DerivativeState, IntakeState, MediaKind
from ...db.models import MediaAsset, MediaDerivative
from .ffmpeg import (
    CREATE_NO_WINDOW,
    FfmpegCancelled,
    FfmpegFailed,
    FfmpegRunner,
    choose_h264_encoder,
    ffmpeg_exe,
)
from .probe import (
    PROBE_VERSION,
    ProbeEngine,
    ProbeError,
    ProbeResult,
    UnsupportedMedia,
)
from .profiles import (
    PROFILE_VERSION,
    ProfileError,
    ProfileNotApplicable,
    get_profile,
    list_profiles,
)
from .storage import (
    FileTooLarge,
    InsufficientStorage,
    MediaStorage,
    MediaStorageError,
    StagedFile,
    UnsafePath,
    normalise_extension,
    sanitise_display_name,
)

log = get_logger(__name__)

THUMBNAIL_VERSION = 2
WAVEFORM_VERSION = 2

ProgressFn = Callable[[float, dict[str, Any]], None]
CancelFn = Callable[[], bool]


class MediaError(RuntimeError):
    code = "MEDIA_ERROR"
    status = 422

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.detail = detail
        # A caller may narrow the code (e.g. DERIVATIVE_MISSING) without a new class.
        if isinstance(detail.get("code"), str):
            self.code = detail["code"]


class AssetNotFound(MediaError):
    code = "ASSET_NOT_FOUND"
    status = 404


class AssetNotReady(MediaError):
    code = "ASSET_NOT_READY"
    status = 409


class DuplicateAsset(MediaError):
    code = "DUPLICATE_ASSET"
    status = 409

    def __init__(self, message: str, *, existing_id: str, sha256: str) -> None:
        super().__init__(message)
        self.existing_id = existing_id
        self.sha256 = sha256
        self.detail = {"existing_asset_id": existing_id, "sha256": sha256}


@dataclass(slots=True)
class ImportOutcome:
    status: str  # imported | duplicate | rejected
    asset_id: str | None
    sha256: str | None
    original_filename: str
    kind: str | None = None
    reason: str | None = None
    elapsed_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "asset_id": self.asset_id,
            "sha256": self.sha256,
            "original_filename": self.original_filename,
            "kind": self.kind,
            "reason": self.reason,
            "elapsed_ms": self.elapsed_ms,
        }


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MediaLibrary:
    def __init__(
        self,
        session: Session,
        *,
        storage: MediaStorage | None = None,
        probe: ProbeEngine | None = None,
        runner: FfmpegRunner | None = None,
    ) -> None:
        self.session = session
        self.storage = storage or MediaStorage()
        self.probe_engine = probe or ProbeEngine()
        self.runner = runner or FfmpegRunner()

    # ------------------------------------------------------------------ lookup
    def get_asset(self, asset_id: str) -> MediaAsset:
        asset = self.session.get(MediaAsset, asset_id)
        if asset is None or asset.deleted_at is not None:
            raise AssetNotFound(f"media asset {asset_id} does not exist")
        return asset

    def by_sha(self, sha256: str) -> MediaAsset | None:
        return self.session.scalars(sa.select(MediaAsset).where(MediaAsset.sha256 == sha256)).first()

    def derivatives(self, asset_id: str, *, kind: str | None = None) -> list[MediaDerivative]:
        stmt = sa.select(MediaDerivative).where(
            MediaDerivative.asset_id == asset_id,
            MediaDerivative.state != DerivativeState.DELETED,
        )
        if kind:
            stmt = stmt.where(MediaDerivative.kind == kind)
        return list(self.session.scalars(stmt.order_by(MediaDerivative.created_at.asc())))

    def primary_thumbnail(self, asset_id: str) -> MediaDerivative | None:
        return self.session.scalars(
            sa.select(MediaDerivative).where(
                MediaDerivative.asset_id == asset_id,
                MediaDerivative.kind == DerivativeKind.THUMBNAIL,
                MediaDerivative.state == DerivativeState.READY,
            )
        ).first()

    def derivative_file(self, derivative: MediaDerivative) -> Path:
        if not derivative.rel_path:
            raise MediaError("derivative has no stored file", code="DERIVATIVE_MISSING")
        path = self.storage.resolve_stored(derivative.rel_path)
        if not path.exists():
            raise MediaError("derivative file is missing from disk", code="DERIVATIVE_MISSING")
        return path

    # ------------------------------------------------------------------ import
    def import_file(
        self,
        source: Path,
        *,
        progress: ProgressFn | None = None,
        should_cancel: CancelFn | None = None,
        auto_thumbnail: bool | None = None,
        original_filename: str | None = None,
    ) -> ImportOutcome:
        """Copy, hash, probe and register one file. Never modifies the source.

        Returns an outcome instead of raising for the two expected non-error cases
        (duplicate, rejected) so a batch import can continue and report per file.
        """
        started = time.perf_counter()
        settings = get_settings()
        display_name = sanitise_display_name(original_filename or source.name)

        staged: StagedFile | None = None
        try:
            staged = self.storage.copy_to_staging(
                source,
                hint=display_name[:24] or "import",
                progress=(lambda done, total: progress(done / total if total else 0.0, {"stage": "copy", "bytes": done, "total": total})) if progress else None,
                should_cancel=should_cancel,
            )
        except FileTooLarge as exc:
            return ImportOutcome("rejected", None, None, display_name, reason=str(exc), elapsed_ms=_ms(started))
        except InsufficientStorage as exc:
            return ImportOutcome("rejected", None, None, display_name, reason=str(exc), elapsed_ms=_ms(started))
        except MediaStorageError as exc:
            return ImportOutcome("rejected", None, None, display_name, reason=str(exc), elapsed_ms=_ms(started))

        duplicate = self.by_sha(staged.sha256)
        if duplicate is not None:
            staged.discard()
            # Same bytes, maybe a different filename: report it as a duplicate of the stored
            # asset instead of creating a second row or a second file.
            self._note_duplicate_filename(duplicate, display_name)
            return ImportOutcome(
                "duplicate",
                duplicate.id,
                staged.sha256,
                display_name,
                kind=str(duplicate.kind),
                reason=f"already in the library as {duplicate.original_filename}",
                elapsed_ms=_ms(started),
            )

        if progress:
            progress(0.6, {"stage": "probe"})
        try:
            probe = self.probe_engine.probe(staged.path, size_bytes=staged.size_bytes)
        except UnsupportedMedia as exc:
            self._quarantine_staged(staged, display_name, str(exc))
            return ImportOutcome(
                "rejected",
                None,
                staged.sha256,
                display_name,
                reason=f"unreadable media: {exc}",
                elapsed_ms=_ms(started),
            )
        except ProbeError as exc:
            staged.discard()
            return ImportOutcome(
                "rejected", None, staged.sha256, display_name, reason=str(exc), elapsed_ms=_ms(started)
            )

        max_duration = settings.media_max_duration_s
        if probe.duration_ms and probe.duration_ms / 1000 > max_duration:
            self._quarantine_staged(
                staged, display_name, f"duration {probe.duration_ms / 1000:.0f}s exceeds the configured limit"
            )
            return ImportOutcome(
                "rejected",
                None,
                staged.sha256,
                display_name,
                reason="media is longer than the configured maximum",
                elapsed_ms=_ms(started),
            )

        extension = normalise_extension(f"x{_extension_for(probe, source)}", fallback=".bin")
        destination = self.storage.promote(staged, extension=extension)
        probe.path = str(destination)

        asset = MediaAsset(
            sha256=staged.sha256,
            source_path=str(destination),
            original_filename=display_name,
            size_bytes=staged.size_bytes,
            duration_ms=probe.duration_ms,
            width=probe.width,
            height=probe.height,
            fps=probe.fps,
            video_codec=probe.video.codec if probe.video else None,
            audio_codec=probe.audio.codec if probe.audio else None,
            rotation=probe.rotation,
            bitrate=probe.bitrate,
            has_audio=probe.has_audio,
            kind=MediaKind(probe.kind) if probe.kind != "unknown" else MediaKind.UNKNOWN,
            intake_state=IntakeState.READY,
            mime_type=probe.mime_type,
            format_name=probe.format_name,
            probe_json=probe.as_dict(),
            probe_version=PROBE_VERSION,
            last_processed_at=_utcnow(),
        )
        self.session.add(asset)
        try:
            self.session.commit()
        except sa.exc.IntegrityError:
            # Two imports of the same file raced; the unique index is the referee.
            self.session.rollback()
            existing_asset = self.by_sha(staged.sha256)
            if existing_asset is not None:
                return ImportOutcome(
                    "duplicate",
                    existing_asset.id,
                    staged.sha256,
                    display_name,
                    kind=str(existing_asset.kind),
                    reason="imported concurrently",
                    elapsed_ms=_ms(started),
                )
            raise

        if progress:
            progress(0.85, {"stage": "registered"})

        wants_thumbnail = settings.media_auto_thumbnail if auto_thumbnail is None else auto_thumbnail
        if wants_thumbnail and probe.kind in ("video", "image"):
            try:
                self.ensure_thumbnail(asset, progress=progress, should_cancel=should_cancel)
            except (FfmpegFailed, FfmpegCancelled, MediaError, ProfileNotApplicable) as exc:
                # A thumbnail failure must not lose the imported original; it is recorded as a
                # failed derivative and the asset stays usable.
                log.warning("thumbnail for %s failed: %s", asset.id, exc)
        if probe.kind == "audio" or (probe.kind == "video" and probe.has_audio):
            try:
                self.ensure_waveform(asset, progress=progress, should_cancel=should_cancel)
            except (FfmpegFailed, FfmpegCancelled, MediaError) as exc:
                log.warning("waveform for %s failed: %s", asset.id, exc)

        if progress:
            progress(1.0, {"stage": "done"})
        return ImportOutcome(
            "imported",
            asset.id,
            staged.sha256,
            display_name,
            kind=str(asset.kind),
            elapsed_ms=_ms(started),
        )

    def _existing_by_size_and_name(self, source: Path, display_name: str) -> MediaAsset | None:
        """Cheap pre-check before copying a large file.

        Not trusted for correctness (identical size does not mean identical bytes) — it only
        lets us avoid copying an obvious repeat when the name *and* size match exactly.
        """
        try:
            size = source.stat().st_size
        except OSError:
            return None
        return self.session.scalars(
            sa.select(MediaAsset).where(
                MediaAsset.original_filename == display_name,
                MediaAsset.size_bytes == size,
                MediaAsset.deleted_at.is_(None),
            )
        ).first()

    def _note_duplicate_filename(self, asset: MediaAsset, display_name: str) -> None:
        meta = dict(asset.probe_json or {})
        seen = list(meta.get("duplicate_filenames") or [])
        if display_name != asset.original_filename and display_name not in seen:
            seen.append(display_name)
            meta["duplicate_filenames"] = seen[-20:]
            asset.probe_json = meta
            asset.updated_at = _utcnow()
            self.session.commit()

    def _quarantine_staged(self, staged: StagedFile, display_name: str, reason: str) -> str:
        extension = normalise_extension(staged.path.name, fallback=".bin")
        try:
            destination = self.storage.move_to_quarantine(staged.path, sha256=staged.sha256, extension=extension)
        except (OSError, UnsafePath):
            staged.discard()
            return "deleted"
        return str(destination)

    # --------------------------------------------------------------- probe
    def reprobe(self, asset: MediaAsset) -> ProbeResult:
        path = self.asset_file(asset)
        probe = self.probe_engine.probe(path, size_bytes=asset.size_bytes)
        asset.probe_json = probe.as_dict()
        asset.probe_version = PROBE_VERSION
        asset.last_processed_at = _utcnow()
        asset.kind = MediaKind(probe.kind) if probe.kind != "unknown" else asset.kind
        asset.width = probe.width
        asset.height = probe.height
        asset.duration_ms = probe.duration_ms
        asset.fps = probe.fps
        asset.mime_type = probe.mime_type
        asset.format_name = probe.format_name
        asset.video_codec = probe.video.codec if probe.video else None
        asset.audio_codec = probe.audio.codec if probe.audio else None
        asset.has_audio = probe.has_audio
        asset.rotation = probe.rotation
        asset.bitrate = probe.bitrate
        self.session.commit()
        return probe

    def asset_file(self, asset: MediaAsset) -> Path:
        path = self.storage.resolve_stored(asset.source_path)
        if not path.exists():
            raise AssetNotReady("the stored original is missing from disk", code="SOURCE_MISSING")
        return path

    # ----------------------------------------------------------- derivatives
    def _derivative_row(
        self,
        asset: MediaAsset,
        *,
        kind: DerivativeKind,
        profile: str,
        version: int,
    ) -> MediaDerivative | None:
        return self.session.scalars(
            sa.select(MediaDerivative).where(
                MediaDerivative.asset_id == asset.id,
                MediaDerivative.kind == kind,
                MediaDerivative.profile == profile,
                MediaDerivative.version == version,
            )
        ).first()

    def _claim_derivative(
        self,
        asset: MediaAsset,
        *,
        kind: DerivativeKind,
        profile: str,
        version: int,
        target: Path,
        job_id: str | None,
        meta: dict[str, Any] | None = None,
    ) -> tuple[MediaDerivative, bool]:
        """Return (row, is_cache_hit).

        A ready row whose file exists is a cache hit. Anything else (missing file, failed or
        stale version) is regenerated in place, so the unique key is never violated.
        """
        row = self._derivative_row(asset, kind=kind, profile=profile, version=version)
        if row is not None and row.state == DerivativeState.READY and row.rel_path:
            try:
                if self.storage.resolve_stored(row.rel_path).exists():
                    return row, True
            except (UnsafePath, OSError):
                pass
        if row is None:
            row = MediaDerivative(
                asset_id=asset.id,
                kind=kind,
                profile=profile,
                version=version,
                state=DerivativeState.RUNNING,
                job_id=job_id,
                meta_json=meta or {},
            )
            self.session.add(row)
        else:
            row.state = DerivativeState.RUNNING
            row.error_code = None
            row.error_detail = None
            row.job_id = job_id
            if meta:
                row.meta_json = {**(row.meta_json or {}), **meta}
        self.session.commit()
        return row, False

    def _finish_derivative(
        self,
        row: MediaDerivative,
        *,
        temporary: Path,
        target: Path,
        width: int | None,
        height: int | None,
        duration_ms: int | None,
        mime_type: str | None,
        meta: dict[str, Any] | None = None,
        expected_kind: str | None = None,
        verify: bool = True,
    ) -> MediaDerivative:
        """Verify the produced file, move it into place and mark the row ready.

        Verification is the difference between "ffmpeg exited 0" and "we produced usable
        media": the file must exist, be non-empty, and probe as the expected kind.
        """
        if not temporary.exists() or temporary.stat().st_size == 0:
            temporary.unlink(missing_ok=True)
            raise MediaError("ffmpeg reported success but produced no output", code="OUTPUT_INVALID")
        produced: ProbeResult | None = None
        if verify:
            try:
                produced = self.probe_engine.probe(temporary)
            except ProbeError as exc:
                temporary.unlink(missing_ok=True)
                raise MediaError(f"derivative failed verification: {exc}", code="OUTPUT_INVALID") from exc
            if expected_kind and produced.kind not in (expected_kind, "video", "audio", "image"):
                temporary.unlink(missing_ok=True)
                raise MediaError(f"derivative is {produced.kind}, expected {expected_kind}", code="OUTPUT_INVALID")

        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, target)
        row.state = DerivativeState.READY
        row.rel_path = self.storage.relative(target)
        row.size_bytes = target.stat().st_size
        row.sha256 = self.storage.sha256_of(target)
        row.width = width or (produced.width if produced else None)
        row.height = height or (produced.height if produced else None)
        row.duration_ms = duration_ms if duration_ms is not None else (produced.duration_ms if produced else None)
        row.mime_type = mime_type or (produced.mime_type if produced else None)
        row.error_code = None
        row.error_detail = None
        if meta:
            row.meta_json = {**(row.meta_json or {}), **meta}
        self.session.commit()
        return row

    def _fail_derivative(self, row: MediaDerivative, exc: Exception, *, code: str | None = None) -> None:
        row.state = DerivativeState.FAILED
        row.error_code = code or getattr(exc, "code", "DERIVATIVE_FAILED")
        row.error_detail = str(exc)[:1200]
        self.session.commit()

    def _tmp_target(self, hint: str, extension: str) -> Path:
        return self.storage.staging_path(hint, extension=extension)

    # ------------------------------------------------------------- thumbnail
    def ensure_thumbnail(
        self,
        asset: MediaAsset,
        *,
        size: int | None = None,
        job_id: str | None = None,
        progress: ProgressFn | None = None,
        should_cancel: CancelFn | None = None,
        force: bool = False,
    ) -> list[MediaDerivative]:
        settings = get_settings()
        size = size or settings.media_thumbnail_size
        if asset.kind == MediaKind.AUDIO:
            raise ProfileNotApplicable("an audio asset has no video frame to use as a thumbnail")
        if asset.kind == MediaKind.UNKNOWN:
            raise ProfileNotApplicable("the asset kind is unknown; probe it before generating a thumbnail")
        source = self.asset_file(asset)
        target = self.storage.derivative_path(
            area="thumbnails",
            sha256=asset.sha256,
            kind="primary",
            profile=f"{size}px",
            version=THUMBNAIL_VERSION,
            ext=".webp",
        )
        row, cached = self._claim_derivative(
            asset,
            kind=DerivativeKind.THUMBNAIL,
            profile=f"{size}px",
            version=THUMBNAIL_VERSION,
            target=target,
            job_id=job_id,
            meta={"size": size},
        )
        if cached and not force:
            return [row]

        temporary = self._tmp_target(f"{asset.id}-thumb", ".webp")
        try:
            if asset.kind == MediaKind.IMAGE:
                args = [
                    "-i", str(source),
                    "-frames:v", "1",
                    "-vf", f"scale=w='min({size},iw)':h='min({size},ih)':force_original_aspect_ratio=decrease",
                    "-c:v", "libwebp", "-quality", "82",
                    "-progress", "pipe:1", "-nostats",
                    str(temporary),
                ]
                result = self.runner.run(
                    args,
                    timeout_s=settings.media_thumbnail_timeout_s,
                    on_progress=(lambda f, info: progress(f, {"stage": "thumbnail", **info})) if progress else None,
                    should_cancel=should_cancel,
                )
                width, height = _image_dimensions(temporary)
            else:
                timestamp_ms = self._pick_thumbnail_time(source, asset, should_cancel=should_cancel)
                if progress:
                    progress(0.3, {"stage": "thumbnail", "frame_ms": timestamp_ms})
                args = [
                    "-ss", f"{timestamp_ms / 1000:.3f}",
                    "-i", str(source),
                    "-frames:v", "1",
                    "-vf", f"scale=w='min({size},iw)':h='min({size},ih)':force_original_aspect_ratio=decrease",
                    "-c:v", "libwebp", "-quality", "82",
                    "-progress", "pipe:1", "-nostats",
                    str(temporary),
                ]
                result = self.runner.run(
                    args,
                    timeout_s=settings.media_thumbnail_timeout_s,
                    on_progress=(lambda f, info: progress(0.3 + 0.7 * f, {"stage": "thumbnail", **info})) if progress else None,
                    should_cancel=should_cancel,
                )
                width, height = _image_dimensions(temporary)
            row = self._finish_derivative(
                row,
                temporary=temporary,
                target=target,
                width=width,
                height=height,
                duration_ms=None,
                mime_type="image/webp",
                meta={"size": size, "frame_ms": (locals().get("timestamp_ms") or None), "ffmpeg": result.as_dict()},
                expected_kind="image",
            )
            asset.thumbnail_path = row.rel_path
            asset.last_processed_at = _utcnow()
            self.session.commit()
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            if isinstance(exc, FfmpegCancelled):
                row.state = DerivativeState.QUEUED
                self.session.commit()
                raise
            self._fail_derivative(row, exc)
            raise
        return [row]

    def _pick_thumbnail_time(
        self, source: Path, asset: MediaAsset, *, should_cancel: CancelFn | None = None
    ) -> int:
        """Choose a frame that is not black, using real measurements from FFmpeg.

        Four candidates are sampled at 8 %, 25 %, 50 % and 75 % of the duration. Each is
        scored by mean luma plus chroma variance (``signalstats``). The brightest, most
        colourful frame wins. If every candidate fails to score, the midpoint is used — a
        plain frame is still better than a failure.
        """
        duration = asset.duration_ms or 0
        if duration <= 0:
            return 0
        candidates = [max(0, int(duration * fraction)) for fraction in (0.08, 0.25, 0.5, 0.75)]
        best: tuple[float, int] | None = None
        for position in candidates:
            score = self._frame_score(source, position, should_cancel=should_cancel)
            if score is None:
                continue
            if best is None or score > best[0]:
                best = (score, position)
        return best[1] if best else candidates[2]

    def _frame_score(self, source: Path, position_ms: int, *, should_cancel: CancelFn | None = None) -> float | None:
        try:
            completed = subprocess.run(
                [
                    str(ffmpeg_exe()),
                    "-hide_banner", "-nostdin",
                    "-ss", f"{position_ms / 1000:.3f}",
                    "-i", str(source),
                    "-frames:v", "1",
                    "-vf", "scale=160:-2,signalstats,metadata=print:file=-",
                    "-f", "null", "-",
                ],
                capture_output=True,
                timeout=25,
                check=False,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        text = (completed.stdout or b"").decode("utf-8", "replace")
        values: dict[str, float] = {}
        for line in text.splitlines():
            if "=" not in line or "signalstats" not in line:
                continue
            key, _, raw = line.partition("=")
            name = key.strip().rsplit(".", 1)[-1]
            try:
                values[name] = float(raw)
            except ValueError:
                continue
        luma = values.get("YAVG")
        if luma is None:
            return None
        saturation = values.get("SATAVG") or 0.0
        contrast = abs((values.get("YHIGH") or luma) - (values.get("YLOW") or luma))
        # A black frame has low luma; a flat grey frame has low contrast. Weight accordingly.
        return luma + 0.5 * saturation + 0.25 * contrast

    def ensure_timeline(
        self,
        asset: MediaAsset,
        *,
        count: int = 10,
        job_id: str | None = None,
        progress: ProgressFn | None = None,
        should_cancel: CancelFn | None = None,
    ) -> list[MediaDerivative]:
        if asset.kind != MediaKind.VIDEO or not asset.duration_ms:
            raise ProfileNotApplicable("timeline thumbnails need a video with a known duration")
        count = max(1, min(count, 40))
        source = self.asset_file(asset)
        rows: list[MediaDerivative] = []
        for index in range(count):
            position_ms = int(asset.duration_ms * (index + 0.5) / count)
            profile = f"t{index:02d}"
            target = self.storage.derivative_path(
                area="thumbnails",
                sha256=asset.sha256,
                kind="timeline",
                profile=profile,
                version=THUMBNAIL_VERSION,
                ext=".webp",
            )
            row, cached = self._claim_derivative(
                asset,
                kind=DerivativeKind.TIMELINE,
                profile=profile,
                version=THUMBNAIL_VERSION,
                target=target,
                job_id=job_id,
                meta={"index": index, "position_ms": position_ms, "count": count},
            )
            if cached:
                rows.append(row)
                continue
            temporary = self._tmp_target(f"{asset.id}-tl{index:02d}", ".webp")
            try:
                self.runner.run(
                    [
                        "-ss", f"{position_ms / 1000:.3f}",
                        "-i", str(source),
                        "-frames:v", "1",
                        "-vf", "scale=w='min(240,iw)':h='min(240,ih)':force_original_aspect_ratio=decrease",
                        "-c:v", "libwebp", "-quality", "76",
                        "-progress", "pipe:1", "-nostats",
                        str(temporary),
                    ],
                    timeout_s=get_settings().media_thumbnail_timeout_s,
                    should_cancel=should_cancel,
                )
                width, height = _image_dimensions(temporary)
                row = self._finish_derivative(
                    row,
                    temporary=temporary,
                    target=target,
                    width=width,
                    height=height,
                    duration_ms=None,
                    mime_type="image/webp",
                    meta={"index": index, "position_ms": position_ms},
                    expected_kind="image",
                )
            except Exception as exc:  # noqa: BLE001 - one bad frame must not abort the timeline
                temporary.unlink(missing_ok=True)
                self._fail_derivative(row, exc)
                continue
            rows.append(row)
            if progress:
                progress((index + 1) / count, {"stage": "timeline", "index": index, "count": count})
        return rows

    # -------------------------------------------------------------- waveform
    def ensure_waveform(
        self,
        asset: MediaAsset,
        *,
        buckets: int = 800,
        job_id: str | None = None,
        progress: ProgressFn | None = None,
        should_cancel: CancelFn | None = None,
    ) -> list[MediaDerivative]:
        """Compute real audio peaks by streaming decoded PCM.

        Why not one FFmpeg filter: ``astats`` per frame is slow on long files and produces
        hundreds of thousands of lines. Decoding to mono 8 kHz PCM and folding min/max into a
        fixed number of buckets is bounded (about 16 KB of PCM per second of audio, never the
        whole file in memory) and gives the numbers the waveform actually needs.
        """
        buckets = max(50, min(buckets, 4000))
        source = self.asset_file(asset)
        target = self.storage.derivative_path(
            area="waveforms", sha256=asset.sha256, kind="peaks", profile=f"{buckets}b", version=WAVEFORM_VERSION, ext=".json"
        )
        row, cached = self._claim_derivative(
            asset,
            kind=DerivativeKind.WAVEFORM,
            profile=f"{buckets}b",
            version=WAVEFORM_VERSION,
            target=target,
            job_id=job_id,
            meta={"buckets": buckets},
        )
        if cached:
            return [row]

        peaks, sample_count, elapsed = self._stream_peaks(source, buckets, progress=progress, should_cancel=should_cancel)
        if not peaks:
            raise MediaError("no decodable audio samples", code="NO_AUDIO_STREAM")
        document = {
            "version": WAVEFORM_VERSION,
            "buckets": len(peaks),
            "samples": sample_count,
            "sample_rate": 8000,
            "peak": max(peaks),
            "peaks": [round(value, 4) for value in peaks],
            "elapsed_ms": elapsed,
        }
        temporary = self._tmp_target(f"{asset.id}-wave", ".json")
        temporary.write_text(json.dumps(document), encoding="utf-8")
        duration = asset.duration_ms
        row = self._finish_derivative(
            row,
            temporary=temporary,
            target=target,
            width=None,
            height=None,
            duration_ms=duration,
            mime_type="application/json",
            meta={"buckets": len(peaks), "samples": sample_count},
            expected_kind=None,
            verify=False,
        )
        return [row]

    def _stream_peaks(
        self,
        source: Path,
        buckets: int,
        *,
        progress: ProgressFn | None = None,
        should_cancel: CancelFn | None = None,
    ) -> tuple[list[float], int, int]:
        started = time.perf_counter()
        command = [
            str(ffmpeg_exe()),
            "-hide_banner", "-nostdin", "-v", "error",
            "-i", str(source),
            "-ac", "1", "-ar", "8000",
            "-f", "s16le", "-",
        ]
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW
        )
        peaks: list[float] = []
        current = 0.0
        total_samples = 0
        bucket_size: int | None = None
        buffer = bytearray()
        try:
            assert process.stdout is not None
            while True:
                if should_cancel is not None and should_cancel():
                    process.kill()
                    raise FfmpegCancelled("waveform cancelled")
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                buffer.extend(chunk)
                usable = len(buffer) - (len(buffer) % 2)
                if usable <= 0:
                    continue
                samples = struct.unpack(f"<{usable // 2}h", bytes(buffer[:usable]))
                del buffer[:usable]
                if bucket_size is None:
                    # One bucket per 1/8 s of audio at 8 kHz: stable output for any length.
                    bucket_size = max(1, 8000 // 8)
                for sample in samples:
                    amplitude = abs(sample) / 32768.0
                    current = max(current, amplitude)
                    total_samples += 1
                    if total_samples % bucket_size == 0:
                        peaks.append(current)
                        current = 0.0
            if current > 0:
                peaks.append(current)
        finally:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                process.kill()

        if not peaks:
            return [], total_samples, int((time.perf_counter() - started) * 1000)

        # Fold to the requested bucket count, keeping the maximum of each group so peaks are
        # never lost by downsampling.
        if len(peaks) > buckets:
            folded: list[float] = []
            group = len(peaks) / buckets
            for index in range(buckets):
                start = int(index * group)
                end = max(start + 1, int((index + 1) * group))
                folded.append(max(peaks[start:end]))
            peaks = folded
        if progress:
            progress(0.9, {"stage": "waveform", "samples": total_samples})
        return peaks, total_samples, int((time.perf_counter() - started) * 1000)

    def ensure_waveform_image(
        self,
        asset: MediaAsset,
        *,
        job_id: str | None = None,
        should_cancel: CancelFn | None = None,
    ) -> list[MediaDerivative]:
        source = self.asset_file(asset)
        target = self.storage.derivative_path(
            area="waveforms", sha256=asset.sha256, kind="poster", profile="1200x300", version=WAVEFORM_VERSION, ext=".png"
        )
        row, cached = self._claim_derivative(
            asset,
            kind=DerivativeKind.WAVEFORM_IMAGE,
            profile="1200x300",
            version=WAVEFORM_VERSION,
            target=target,
            job_id=job_id,
        )
        if cached:
            return [row]
        temporary = self._tmp_target(f"{asset.id}-waveimg", ".png")
        try:
            self.runner.run(
                [
                    "-i", str(source),
                    "-filter_complex",
                    "aformat=channel_layouts=mono,showwavespic=s=1200x300:colors=0x38bdf8",
                    "-frames:v", "1",
                    "-progress", "pipe:1", "-nostats",
                    str(temporary),
                ],
                timeout_s=get_settings().media_thumbnail_timeout_s,
                should_cancel=should_cancel,
            )
            row = self._finish_derivative(
                row, temporary=temporary, target=target, width=1200, height=300, duration_ms=None,
                mime_type="image/png", expected_kind="image",
            )
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            self._fail_derivative(row, exc)
            raise
        return [row]

    # -------------------------------------------------------------- transcode
    def transcode(
        self,
        asset: MediaAsset,
        profile_id: str,
        *,
        height: int | None = None,
        crf: int | None = None,
        fps: int | None = None,
        job_id: str | None = None,
        progress: ProgressFn | None = None,
        should_cancel: CancelFn | None = None,
    ) -> MediaDerivative:
        profile = get_profile(profile_id)
        if profile.needs_video and asset.kind != MediaKind.VIDEO:
            raise ProfileNotApplicable(f"profile {profile.id} needs a video asset, this one is {asset.kind}")
        if not profile.needs_video and not asset.has_audio:
            raise ProfileNotApplicable(f"profile {profile.id} needs an audio track and this asset has none")

        source = self.asset_file(asset)
        area = {
            "fast_preview": "previews",
            "editing_proxy": "proxies",
            "normalized": "transcodes",
            "audio_wav": "audio",
            "audio_aac": "audio",
        }[profile.id]
        kind = {
            "fast_preview": DerivativeKind.PREVIEW,
            "editing_proxy": DerivativeKind.PROXY,
            "normalized": DerivativeKind.TRANSCODE,
            "audio_wav": DerivativeKind.AUDIO,
            "audio_aac": DerivativeKind.AUDIO,
        }[profile.id]
        variant = profile.id if not height else f"{profile.id}-{height}p"
        target = self.storage.derivative_path(
            area=area,  # type: ignore[arg-type]
            sha256=asset.sha256,
            kind=kind.value,
            profile=variant,
            version=PROFILE_VERSION,
            ext=profile.extension(),
        )
        row, cached = self._claim_derivative(
            asset, kind=kind, profile=variant, version=PROFILE_VERSION, target=target, job_id=job_id
        )
        if cached:
            return row

        encoder = choose_h264_encoder(self.runner)
        temporary = self._tmp_target(f"{asset.id}-{profile.id}", profile.extension())
        args = profile.build_args(
            source=str(source),
            destination=str(temporary),
            encoder=encoder,
            has_audio=asset.has_audio,
            source_width=asset.width,
            source_height=asset.height,
            height_override=height,
            crf_override=crf,
            fps_override=fps,
            source_fps=asset.fps,
        )
        try:
            result = self.runner.run(
                args,
                timeout_s=get_settings().media_transcode_timeout_s,
                total_duration_ms=asset.duration_ms,
                on_progress=(lambda f, info: progress(f, {"stage": profile.id, **info})) if progress else None,
                should_cancel=should_cancel,
            )
            row = self._finish_derivative(
                row,
                temporary=temporary,
                target=target,
                width=None,
                height=None,
                duration_ms=None,
                mime_type=profile.mime_type(),
                meta={
                    "profile": profile.id,
                    "encoder": encoder.name,
                    "hardware": encoder.hardware,
                    "ffmpeg": result.as_dict(),
                    "requested": {"height": height, "crf": crf, "fps": fps},
                },
                expected_kind="audio" if not profile.needs_video else "video",
            )
            asset.last_processed_at = _utcnow()
            self.session.commit()
            return row
        except (FfmpegFailed, FfmpegCancelled, MediaError) as exc:
            temporary.unlink(missing_ok=True)
            if isinstance(exc, FfmpegCancelled):
                row.state = DerivativeState.QUEUED
                self.session.commit()
                raise
            self._fail_derivative(row, exc)
            raise

    def delete_derivative(self, row: MediaDerivative) -> None:
        """Delete one derived file. Originals are never touched here."""
        if row.rel_path:
            try:
                path = self.storage.resolve_stored(row.rel_path)
                path.unlink(missing_ok=True)
            except (UnsafePath, OSError) as exc:
                log.warning("could not delete derivative file %s: %s", row.id, exc)
        row.state = DerivativeState.DELETED
        row.rel_path = None
        self.session.commit()


def _extension_for(probe: ProbeResult, source: Path) -> str:
    """Storage extension from the probed container, not from the user's filename."""
    base = (probe.format_name or "").split(",")[0]
    mapping = {
        "mov": prop_ext(probe, source),
        "mp4": ".mp4",
        "matroska": ".mkv",
        "webm": ".webm",
        "avi": ".avi",
        "mpeg": ".mpg",
        "mpegts": ".ts",
        "flv": ".flv",
        "asf": ".wmv",
        "mp3": ".mp3",
        "wav": ".wav",
        "flac": ".flac",
        "ogg": ".ogg",
        "aac": ".aac",
        "png_pipe": ".png",
        "jpeg_pipe": ".jpg",
        "webp_pipe": ".webp",
        "gif": ".gif",
        "image2": normalise_extension(source.name, fallback=".img"),
    }
    return mapping.get(base, normalise_extension(source.name, fallback=".bin"))


def prop_ext(probe: ProbeResult, source: Path) -> str:
    """MOV vs MP4 for the shared ``mov`` demuxer: prefer the real brand, then the source name."""
    brand = ((probe.tags or {}).get("major_brand") or "").strip().lower()
    if brand.startswith("qt"):
        return ".mov"
    suffix = normalise_extension(source.name, fallback="")
    return ".mov" if suffix == ".mov" else ".mp4"


def _image_dimensions(path: Path) -> tuple[int | None, int | None]:
    """Read PNG/WebP dimensions from the header. No third-party image library needed."""
    try:
        data = path.read_bytes()[:64]
    except OSError:
        return None, None
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return width, height
        if chunk == b"VP8 " and len(data) >= 30:
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return width, height
    return None, None


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _tool_version(executable: Path) -> str | None:
    """Read a version banner from an FFmpeg-family binary without trusting a filename."""
    if not executable.exists():
        return None
    try:
        completed = subprocess.run(
            [str(executable), "-hide_banner", "-version"],
            capture_output=True,
            timeout=15,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - broken install
        return None
    lines = (completed.stdout or b"").decode("utf-8", "replace").splitlines()
    return lines[0].strip() if lines else None


def library_supports() -> dict[str, Any]:
    """What this machine can do, measured now (used by the API and the docs).

    Two distinct facts are reported, because conflating them is how products end up lying:
    ``ffmpeg.encoders`` says an encoder is *listed* by the build, while
    ``encoder_verification`` says a real encode *succeeded* on this GPU and driver.
    """
    settings = get_settings()
    runner = FfmpegRunner()
    caps = runner.capabilities()
    encoder = choose_h264_encoder(runner)
    listed = caps.get("encoders", {}) or {}
    verification: dict[str, Any] = {
        "libx264": {"available": bool(listed.get("libx264")), "hardware": False, "verified": bool(listed.get("libx264"))},
        "h264_nvenc": {
            "available": encoder.name == "h264_nvenc" and encoder.verified,
            "hardware": True,
            "verified": encoder.name == "h264_nvenc" and encoder.verified,
            "listed": bool(listed.get("h264_nvenc")),
            "note": None if encoder.name == "h264_nvenc" else "listed by the build but a real encode failed on this driver",
        },
    }
    return {
        "ffmpeg": caps,
        "h264_encoder": {
            "name": encoder.name,
            "hardware": encoder.hardware,
            "verified": encoder.verified,
            "detail": encoder.detail,
            "note": None if encoder.hardware else "NVENC is unavailable on this driver; encoding runs on the CPU",
        },
        "encoder_verification": verification,
        "thumbnail_format": "webp" if listed.get("libwebp") else "jpeg",
        "probe": {
            "available": (settings.ffmpeg_bin / "ffprobe.exe").exists(),
            "path": str(settings.ffmpeg_bin / "ffprobe.exe"),
            "version": _tool_version(settings.ffmpeg_bin / "ffprobe.exe"),
        },
        "policy": {
            "max_concurrent_ffmpeg": settings.media_max_concurrent_ffmpeg,
            "max_import_bytes": settings.media_max_import_bytes,
            "min_free_gb": settings.media_min_free_gb,
            "thumbnail_size": settings.media_thumbnail_size,
            "thumbnail_timeout_s": settings.media_thumbnail_timeout_s,
            "transcode_timeout_s": settings.media_transcode_timeout_s,
        },
        "profiles": list_profiles(),
    }


__all__ = [
    "AssetNotFound",
    "AssetNotReady",
    "DuplicateAsset",
    "ImportOutcome",
    "MediaError",
    "MediaLibrary",
    "ProfileError",
    "library_supports",
]
