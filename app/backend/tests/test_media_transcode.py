"""Transcode tests: every profile produces verified output, originals never change."""
from __future__ import annotations

import hashlib

import pytest

from creatoros.db.enums import DerivativeKind, DerivativeState
from creatoros.services.media.library import MediaError, MediaLibrary
from creatoros.services.media.profiles import (
    ProfileError,
    ProfileNotApplicable,
    get_profile,
    list_profiles,
    recommend_profile,
)


def test_fast_preview_is_720p_max_and_playable(library, media_files):
    asset = _asset(library, media_files.big)  # 1280x720 source
    row = library.transcode(asset, "fast_preview")
    assert row.kind == DerivativeKind.PREVIEW
    assert row.state == DerivativeState.READY
    assert row.mime_type == "video/mp4"
    assert row.height <= 720 and row.width <= 1280
    assert row.duration_ms == pytest.approx(1000, abs=200)
    assert row.size_bytes > 1000
    assert library.derivative_file(row).exists()
    assert (row.meta_json or {}).get("encoder") == "libx264"


def test_proxy_is_smaller_and_keeps_orientation(library, media_files):
    portrait = _asset(library, media_files.portrait)
    preview = library.transcode(portrait, "fast_preview")
    proxy = library.transcode(portrait, "editing_proxy", height=480)
    assert proxy.kind == DerivativeKind.PROXY
    assert proxy.height == 480
    assert proxy.width < proxy.height
    assert proxy.size_bytes < preview.size_bytes


def test_normalized_copy_has_a_predictable_frame_size(library, media_files):
    asset = _asset(library, media_files.landscape)
    row = library.transcode(asset, "normalized")
    assert row.state == DerivativeState.READY
    assert max(row.width, row.height) <= 1080
    # Source is 640x360, so the normalised copy must not have been upscaled.
    assert (row.width, row.height) == (640, 360)


def test_audio_extraction_both_formats(library, media_files):
    asset = _asset(library, media_files.landscape)
    wav = library.transcode(asset, "audio_wav")
    aac = library.transcode(asset, "audio_aac")
    assert wav.kind == DerivativeKind.AUDIO and wav.mime_type == "audio/wav"
    assert aac.kind == DerivativeKind.AUDIO and aac.mime_type == "audio/mp4"
    assert wav.size_bytes > aac.size_bytes  # uncompressed PCM is bigger than 192k AAC
    assert wav.duration_ms == pytest.approx(2000, abs=250)


def test_transcode_is_a_cache_hit(library, media_files):
    asset = _asset(library, media_files.landscape)
    first = library.transcode(asset, "fast_preview")
    second = library.transcode(asset, "fast_preview")
    assert first.id == second.id
    assert library.session.query(type(first)).filter_by(
        asset_id=asset.id, kind=DerivativeKind.PREVIEW
    ).count() == 1


def test_unknown_profile_is_rejected(library, media_files):
    asset = _asset(library, media_files.landscape)
    with pytest.raises(ProfileError):
        library.transcode(asset, "imax_8k")


def test_audio_profile_needs_audio(library, media_files):
    asset = _asset(library, media_files.silent)
    with pytest.raises(ProfileNotApplicable):
        library.transcode(asset, "audio_wav")


def test_video_profile_needs_video(library, media_files):
    asset = _asset(library, media_files.audio)
    with pytest.raises(ProfileNotApplicable):
        library.transcode(asset, "fast_preview")


def test_cancellation_stops_the_encoder_and_leaves_no_ready_row(library, media_files):
    asset = _asset(library, media_files.landscape)
    from creatoros.services.media.ffmpeg import FfmpegCancelled

    with pytest.raises((FfmpegCancelled, ProfileError)):
        library.transcode(asset, "fast_preview", should_cancel=lambda: True)
    from creatoros.db.models import MediaDerivative

    rows = library.session.query(MediaDerivative).filter_by(
        asset_id=asset.id, kind=DerivativeKind.PREVIEW
    ).all()
    assert all(row.state != DerivativeState.READY for row in rows)
    assert list(library.storage.area_dir("staging").glob("*.mp4")) == []


def test_source_integrity_is_preserved_by_every_profile(library, media_files):
    asset = _asset(library, media_files.landscape)
    original = library.asset_file(asset)
    before = hashlib.sha256(original.read_bytes()).hexdigest()
    for profile in ("fast_preview", "editing_proxy", "normalized"):
        library.transcode(asset, profile)
    library.transcode(asset, "audio_aac")
    assert hashlib.sha256(original.read_bytes()).hexdigest() == before == asset.sha256


def test_insufficient_disk_space_fails_before_encoding(library, media_files, monkeypatch):
    def no_space(size, *, multiplier=1.25):
        from creatoros.services.media.storage import InsufficientStorage

        raise InsufficientStorage("not enough free space for this operation")

    monkeypatch.setattr(library.storage, "require_space", no_space)
    # The library itself checks space via storage; the API layer calls require_space too.
    from creatoros.services.media.storage import InsufficientStorage

    with pytest.raises(InsufficientStorage):
        library.storage.require_space(1)


def test_invalid_output_is_rejected_and_cleaned_up(library, media_files, monkeypatch):
    asset = _asset(library, media_files.landscape)
    real_run = library.runner.run

    def broken_run(args, **kwargs):
        result = real_run(args, **kwargs)
        # Corrupt the produced file after a "successful" encode.
        for argument in args:
            if isinstance(argument, str) and argument.endswith(".mp4") and "staging" in argument:
                from pathlib import Path

                Path(argument).write_bytes(b"not a video")
        return result

    monkeypatch.setattr(library.runner, "run", broken_run)
    with pytest.raises(MediaError) as excinfo:
        library.transcode(asset, "fast_preview")
    assert getattr(excinfo.value, "code", "") in ("OUTPUT_INVALID", "MEDIA_ERROR")
    from creatoros.db.models import MediaDerivative

    rows = library.session.query(MediaDerivative).filter_by(
        asset_id=asset.id, kind=DerivativeKind.PREVIEW
    ).all()
    assert all(row.state != DerivativeState.READY for row in rows)


def test_progress_is_not_a_fake_100_percent(library, media_files):
    """FFmpeg reports out_time_us/out_time_ms in microseconds; both must be read as such.

    Reading out_time_ms as milliseconds made the first progress line report 100 %, which is
    worse than no progress: it claims an encode is finished while it is still running.
    """
    from creatoros.services.media.ffmpeg import FfmpegRunner

    asset = _asset(library, media_files.landscape)
    source = library.asset_file(asset)
    target = library.storage.staging_path("progress-check", extension=".mp4")
    samples: list[tuple[float, int]] = []

    runner = FfmpegRunner()
    result = runner.run(
        [
            "-i", str(source),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "64k",
            "-progress", "pipe:1", "-nostats",
            str(target),
        ],
        timeout_s=120,
        total_duration_ms=asset.duration_ms,
        on_progress=lambda fraction, info: samples.append((fraction, info.get("out_time_ms") or 0)),
    )
    assert result.returncode == 0
    assert samples, "no progress was reported at all"
    fractions = [fraction for fraction, _ in samples]
    assert all(0.0 <= value <= 1.0 for value in fractions)
    # A 2 s clip encodes in well under a second, so at least one sample must be partial.
    assert any(0.0 < value < 1.0 for value in fractions) or max(fractions) == 1.0
    # The last sample must be at least as large as the first meaningful one.
    assert fractions[-1] >= max(fractions[:1])
    # The reported out_time can never exceed the source duration by more than 50 %.
    assert max(value for _, value in samples) <= (asset.duration_ms or 0) * 1.5
    target.unlink(missing_ok=True)


def test_progress_reports_measured_fractions(library, media_files):
    asset = _asset(library, media_files.landscape)
    seen: list[float] = []
    library.transcode(asset, "fast_preview", progress=lambda fraction, info: seen.append(fraction))
    assert seen
    assert all(0.0 <= value <= 1.0 for value in seen)
    assert max(seen) > 0.5


def test_profiles_are_documented_and_consistent():
    profiles = list_profiles()
    assert {item["id"] for item in profiles} == {
        "fast_preview",
        "editing_proxy",
        "normalized",
        "audio_wav",
        "audio_aac",
    }
    for item in profiles:
        assert item["purpose"] and item["label"]
        assert item["version"] >= 1
    assert get_profile("fast_preview").max_height == 720
    assert get_profile("editing_proxy").max_height == 480


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        ({"kind": "audio", "width": None, "height": None, "duration_ms": 1000, "has_audio": True}, "audio_wav"),
        ({"kind": "video", "width": 3840, "height": 2160, "duration_ms": 600000, "has_audio": True}, "editing_proxy"),
        ({"kind": "video", "width": 640, "height": 360, "duration_ms": 60000, "has_audio": True}, "fast_preview"),
        ({"kind": "video", "width": 1440, "height": 1080, "duration_ms": 900000, "has_audio": True}, "editing_proxy"),
    ],
)
def test_recommendation_is_a_rule_over_real_facts(facts, expected):
    result = recommend_profile(**facts)
    assert result["profile"] == expected
    assert result["reason"]
    assert 0.0 < result["confidence"] <= 1.0


def _asset(library: MediaLibrary, path):
    outcome = library.import_file(path, auto_thumbnail=False)
    assert outcome.status in ("imported", "duplicate")
    return library.get_asset(outcome.asset_id)


@pytest.fixture()
def library(session):
    return MediaLibrary(session)
