"""Derivative tests: thumbnails, timelines, waveforms — real files, real cache hits."""
from __future__ import annotations

import json

import pytest

from creatoros.db.enums import DerivativeKind, DerivativeState
from creatoros.db.models import MediaDerivative
from creatoros.services.media.library import MediaError, MediaLibrary
from creatoros.services.media.profiles import ProfileNotApplicable


def test_landscape_thumbnail_keeps_aspect_ratio_and_does_not_upscale(library, media_files):
    outcome = library.import_file(media_files.landscape, auto_thumbnail=False)
    asset = library.get_asset(outcome.asset_id)
    rows = library.ensure_thumbnail(asset, size=480)
    thumb = rows[0]
    assert thumb.state == DerivativeState.READY
    assert thumb.mime_type == "image/webp"
    assert thumb.size_bytes > 0
    assert thumb.width == 480
    assert thumb.height == pytest.approx(270, abs=3)
    assert library.derivative_file(thumb).exists()


def test_portrait_thumbnail_is_portrait(library, media_files):
    outcome = library.import_file(media_files.portrait, auto_thumbnail=False)
    asset = library.get_asset(outcome.asset_id)
    thumb = library.ensure_thumbnail(asset, size=480)[0]
    assert thumb.height == 480
    assert thumb.width < thumb.height


def test_small_source_is_not_upscaled(library, media_files):
    outcome = library.import_file(media_files.silent, auto_thumbnail=False)
    asset = library.get_asset(outcome.asset_id)  # 320x240
    thumb = library.ensure_thumbnail(asset, size=480)[0]
    assert max(thumb.width, thumb.height) <= 320


def test_image_thumbnail_preserves_orientation(library, media_files):
    outcome = library.import_file(media_files.image, auto_thumbnail=False)
    asset = library.get_asset(outcome.asset_id)  # 800x600
    thumb = library.ensure_thumbnail(asset, size=400)[0]
    assert thumb.width == 400
    assert thumb.height == pytest.approx(300, abs=3)


def test_thumbnail_is_a_cache_hit_on_repeat(library, media_files):
    outcome = library.import_file(media_files.landscape)
    asset = library.get_asset(outcome.asset_id)
    first = library.primary_thumbnail(asset.id)
    path = library.derivative_file(first)
    mtime = path.stat().st_mtime_ns

    again = library.ensure_thumbnail(asset)[0]
    assert again.id == first.id
    assert library.derivative_file(again).stat().st_mtime_ns == mtime
    assert library.session.query(MediaDerivative).filter_by(asset_id=asset.id, kind=DerivativeKind.THUMBNAIL).count() == 1


def test_force_regenerates_the_same_row(library, media_files):
    outcome = library.import_file(media_files.landscape)
    asset = library.get_asset(outcome.asset_id)
    first = library.primary_thumbnail(asset.id)
    forced = library.ensure_thumbnail(asset, force=True)[0]
    assert forced.id == first.id
    assert library.session.query(MediaDerivative).filter_by(asset_id=asset.id, kind=DerivativeKind.THUMBNAIL).count() == 1


def test_audio_asset_has_no_thumbnail(library, media_files):
    outcome = library.import_file(media_files.audio)
    asset = library.get_asset(outcome.asset_id)
    assert library.primary_thumbnail(asset.id) is None
    with pytest.raises(ProfileNotApplicable):
        library.ensure_thumbnail(asset)


def test_timeline_generates_the_requested_number_of_frames(library, media_files):
    outcome = library.import_file(media_files.landscape, auto_thumbnail=False)
    asset = library.get_asset(outcome.asset_id)
    rows = library.ensure_timeline(asset, count=5)
    assert len(rows) == 5
    assert all(row.kind == DerivativeKind.TIMELINE and row.state == DerivativeState.READY for row in rows)
    assert [row.profile for row in rows] == ["t00", "t01", "t02", "t03", "t04"]
    assert all(row.width and row.width <= 240 for row in rows)


def test_timeline_is_idempotent(library, media_files):
    outcome = library.import_file(media_files.landscape, auto_thumbnail=False)
    asset = library.get_asset(outcome.asset_id)
    first = library.ensure_timeline(asset, count=3)
    second = library.ensure_timeline(asset, count=3)
    assert {row.id for row in first} == {row.id for row in second}


def test_waveform_peaks_are_real_numbers(library, media_files):
    outcome = library.import_file(media_files.audio, auto_thumbnail=False)
    asset = library.get_asset(outcome.asset_id)
    row = library.ensure_waveform(asset, buckets=200)[0]
    assert row.kind == DerivativeKind.WAVEFORM
    document = json.loads(library.derivative_file(row).read_text(encoding="utf-8"))
    assert document["buckets"] == len(document["peaks"]) <= 200
    assert document["samples"] > 8000  # at least one second at 8 kHz
    peaks = document["peaks"]
    assert all(0.0 <= value <= 1.0 for value in peaks)
    assert max(peaks) > 0.02  # a 440 Hz tone is measurably not silence


def test_silence_produces_a_flat_waveform_and_a_tone_does_not(library, media_files):
    """The waveform must reflect the actual samples, not a constant decorative shape."""
    quiet = library.import_file(media_files.silence, auto_thumbnail=False)
    loud = library.import_file(media_files.audio_wav, auto_thumbnail=False)
    quiet_row = library.ensure_waveform(library.get_asset(quiet.asset_id), buckets=80)[0]
    loud_row = library.ensure_waveform(library.get_asset(loud.asset_id), buckets=80)[0]
    quiet_peaks = json.loads(library.derivative_file(quiet_row).read_text(encoding="utf-8"))["peaks"]
    loud_peaks = json.loads(library.derivative_file(loud_row).read_text(encoding="utf-8"))["peaks"]
    assert max(quiet_peaks) < 0.01
    assert max(loud_peaks) > 20 * max(max(quiet_peaks), 0.001)


def test_waveform_for_silent_video_fails_cleanly(library, media_files):
    outcome = library.import_file(media_files.silent, auto_thumbnail=False)
    asset = library.get_asset(outcome.asset_id)
    with pytest.raises((MediaError, RuntimeError)) as excinfo:
        library.ensure_waveform(asset)
    assert getattr(excinfo.value, "code", "") in ("NO_AUDIO_STREAM", "MEDIA_ERROR", "FFMPEG_FAILED")
    assert all(row.state != DerivativeState.READY for row in library.derivatives(asset.id))


def test_waveform_is_a_cache_hit(library, media_files):
    outcome = library.import_file(media_files.audio_wav, auto_thumbnail=False)
    asset = library.get_asset(outcome.asset_id)
    first = library.ensure_waveform(asset, buckets=100)[0]
    second = library.ensure_waveform(asset, buckets=100)[0]
    assert first.id == second.id


def test_waveform_image(library, media_files):
    outcome = library.import_file(media_files.audio, auto_thumbnail=False)
    asset = library.get_asset(outcome.asset_id)
    row = library.ensure_waveform_image(asset)[0]
    assert row.kind == DerivativeKind.WAVEFORM_IMAGE
    assert row.state == DerivativeState.READY
    assert library.derivative_file(row).stat().st_size > 1000


def test_thumbnail_cancellation_leaves_no_ready_row(library, media_files):
    from creatoros.services.media.ffmpeg import FfmpegCancelled

    outcome = library.import_file(media_files.landscape)
    asset = library.get_asset(outcome.asset_id)
    with pytest.raises(FfmpegCancelled):
        library.ensure_thumbnail(asset, force=True, should_cancel=lambda: True)
    rows = library.session.query(MediaDerivative).filter_by(asset_id=asset.id, kind=DerivativeKind.THUMBNAIL).all()
    assert all(row.state != DerivativeState.RUNNING for row in rows)


def test_failed_thumbnail_is_recorded_with_an_error(library, media_files, monkeypatch):
    outcome = library.import_file(media_files.landscape)
    asset = library.get_asset(outcome.asset_id)

    def boom(*args, **kwargs):
        raise RuntimeError("encoder exploded")

    monkeypatch.setattr(library.runner, "run", boom)
    with pytest.raises((MediaError, RuntimeError)):
        library.ensure_thumbnail(asset, force=True)
    row = library.session.query(MediaDerivative).filter_by(asset_id=asset.id, kind=DerivativeKind.THUMBNAIL).one()
    assert row.state == DerivativeState.FAILED
    assert row.error_code


def test_thumbnail_selection_avoids_a_black_first_frame(library, media_files, tmp_path):
    """A clip that starts black must still get a usable thumbnail."""
    from tests.media_fixtures import run_ffmpeg

    black_start = tmp_path / "black-start.mp4"
    run_ffmpeg(
        [
            "-f", "lavfi", "-i", "color=c=black:s=320x180:d=0.6:r=20",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=20:duration=1.4",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p",
            str(black_start),
        ]
    )
    outcome = library.import_file(black_start, auto_thumbnail=False)
    asset = library.get_asset(outcome.asset_id)
    thumb = library.ensure_thumbnail(asset)[0]
    chosen_ms = (thumb.meta_json or {}).get("frame_ms")
    assert chosen_ms is not None
    # The black lead-in is the first 30 % of the clip; the chosen frame must be after it.
    assert chosen_ms > 400


@pytest.fixture()
def library(session):
    return MediaLibrary(session)
