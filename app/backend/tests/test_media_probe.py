"""Probe engine tests against real files, plus every malformed input we can produce."""
from __future__ import annotations

import json
import time

import pytest

from creatoros.services.media.probe import (
    PROBE_VERSION,
    ProbeEngine,
    ProbeError,
    ProbeTimeout,
    UnsupportedMedia,
    classify,
    parse_duration_seconds,
    parse_rate,
    parse_rotation,
)


# --------------------------------------------------------------------- helpers
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("30000/1001", 29.9700),
        ("25/1", 25.0),
        ("30", 30.0),
        ("0/0", None),
        ("N/A", None),
        (None, None),
        ("", None),
        ("1/0", None),
        ("1000/1", None),  # implausible frame rate is dropped, not trusted
    ],
)
def test_parse_rate(raw, expected):
    assert parse_rate(raw) == (expected if expected is None else pytest.approx(expected, abs=1e-4))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12.345", 12.345),
        ("00:01:23.500", 83.5),
        ("N/A", None),
        ("-4", None),
        ("nan", None),
        (None, None),
        ("not a number", None),
    ],
)
def test_parse_duration(raw, expected):
    value = parse_duration_seconds(raw)
    if expected is None:
        assert value is None
    else:
        assert value == pytest.approx(expected, abs=1e-6)


def test_parse_rotation_reads_side_data_and_tags():
    assert parse_rotation({"side_data_list": [{"rotation": -90}]}) == 270
    assert parse_rotation({"tags": {"rotate": "90"}}) == 90
    assert parse_rotation({}) == 0
    assert parse_rotation({"side_data_list": [{"rotation": "banana"}]}) == 0


def test_classify_uses_streams_not_the_extension():
    assert classify(format_name="mov,mp4", video={"codec_name": "h264"}, audio=None) == "video"
    assert classify(format_name="mp3", video=None, audio={"codec_name": "mp3"}) == "audio"
    assert classify(format_name="png_pipe", video={"codec_name": "png"}, audio=None) == "image"
    assert classify(format_name="weird", video=None, audio=None) == "unknown"


# ----------------------------------------------------------------- real probes
def test_probe_landscape_video(probe, media_files):
    result = probe.probe(media_files.landscape)
    assert result.kind == "video"
    assert result.width == 640 and result.height == 360
    assert result.orientation == "landscape"
    assert result.duration_ms == pytest.approx(2000, abs=120)
    assert result.video.codec == "h264"
    assert result.audio.codec == "aac"
    assert result.has_audio is True
    assert result.fps == pytest.approx(25, abs=0.5)
    assert result.video.aspect_ratio == "16:9"
    assert result.mime_type == "video/mp4"
    assert result.probe_version == PROBE_VERSION
    assert result.elapsed_ms >= 0


def test_probe_portrait_video_reports_display_dimensions(probe, media_files):
    result = probe.probe(media_files.portrait)
    assert result.width == 360 and result.height == 640
    assert result.orientation == "portrait"


def test_probe_silent_video_has_no_audio(probe, media_files):
    result = probe.probe(media_files.silent)
    assert result.kind == "video"
    assert result.has_audio is False
    assert result.audio is None


def test_probe_image(probe, media_files):
    result = probe.probe(media_files.image)
    assert result.kind == "image"
    assert (result.width, result.height) == (800, 600)
    assert result.mime_type == "image/jpeg"
    assert result.video.codec == "mjpeg"


def test_probe_png_image(probe, media_files):
    result = probe.probe(media_files.image_png)
    assert result.kind == "image"
    assert result.mime_type == "image/png"


def test_probe_audio_file(probe, media_files):
    result = probe.probe(media_files.audio)
    assert result.kind == "audio"
    assert result.has_audio is True
    assert result.width is None and result.height is None
    assert result.audio.codec == "mp3"
    assert result.audio.sample_rate in (44100, 48000)
    assert result.duration_ms == pytest.approx(2000, abs=150)


def test_probe_wav_reports_pcm(probe, media_files):
    result = probe.probe(media_files.audio_wav)
    assert result.kind == "audio"
    assert result.audio.codec == "pcm_s16le"


# ------------------------------------------------------------------ bad inputs
def test_text_file_named_mp4_is_rejected_by_content(probe, media_files):
    with pytest.raises(UnsupportedMedia):
        probe.probe(media_files.fake)


def test_truncated_file_is_rejected_cleanly(probe, media_files):
    with pytest.raises(ProbeError) as excinfo:
        probe.probe(media_files.truncated)
    assert excinfo.value.code in ("UNSUPPORTED_MEDIA", "MEDIA_PROBE_FAILED")


def test_missing_file_is_a_clean_error(probe):
    from pathlib import Path

    with pytest.raises(ProbeError):
        probe.probe(Path("<DATA_ROOT>/temp/definitely-not-here.mp4"))


def test_timeout_is_reported_as_timeout(media_files):
    engine = ProbeEngine(timeout_s=0.001)
    with pytest.raises(ProbeTimeout):
        engine.probe(media_files.landscape)


def test_invalid_json_is_rejected_without_crashing(monkeypatch, probe, media_files):
    class FakeCompleted:
        returncode = 0
        stdout = b"{not json at all"
        stderr = b""

    monkeypatch.setattr(
        "creatoros.services.media.probe.subprocess.run", lambda *a, **k: FakeCompleted()
    )
    with pytest.raises(ProbeError) as excinfo:
        probe.probe(media_files.landscape)
    assert "invalid JSON" in str(excinfo.value)


def test_document_without_streams_is_unsupported(probe, media_files):
    payload = {"format": {"format_name": "mp4"}, "streams": [], "error": {"string": "moov atom not found"}}
    with pytest.raises(UnsupportedMedia) as excinfo:
        probe.parse(payload, path=media_files.landscape)
    assert "moov atom not found" in str(excinfo.value)


def test_malformed_numbers_are_dropped_with_warnings(probe, media_files):
    payload = {
        "format": {"format_name": "mov,mp4", "duration": "N/A", "bit_rate": "abc"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": "0",
                "height": 720,
                "avg_frame_rate": "0/0",
                "r_frame_rate": "0/0",
                "duration": "N/A",
            }
        ],
    }
    result = probe.parse(payload, path=media_files.landscape, size_bytes=10)
    assert result.width is None
    assert result.height == 720
    assert result.fps is None
    assert result.duration_ms is None
    assert any("dimensions" in warning for warning in result.warnings)
    assert any("duration" in warning for warning in result.warnings)


def test_absurd_values_are_clamped_away(probe, media_files):
    payload = {
        "format": {"format_name": "mov,mp4", "duration": "999999999999", "bit_rate": "999999999999999"},
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 999999, "height": 480, "avg_frame_rate": "99999/1"}],
    }
    result = probe.parse(payload, path=media_files.landscape, size_bytes=1)
    assert result.width is None
    assert result.height == 480
    assert result.fps is None
    assert result.bitrate is None
    # duration is finite but huge; the import layer refuses it via the configured maximum
    assert result.duration_ms and result.duration_ms > 10**9


def test_hdr_detection_from_color_transfer(probe, media_files):
    payload = {
        "format": {"format_name": "mov,mp4"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 3840,
                "height": 2160,
                "avg_frame_rate": "24/1",
                "color_transfer": "smpte2084",
            }
        ],
    }
    result = probe.parse(payload, path=media_files.landscape, size_bytes=1)
    assert result.is_hdr is True
    assert result.video.color_transfer == "smpte2084"
    assert result.kind == "video"


def test_as_dict_is_json_serialisable(probe, media_files):
    result = probe.probe(media_files.landscape)
    document = result.as_dict()
    assert json.loads(json.dumps(document))["kind"] == "video"
    assert document["video"]["width"] == 640
    assert document["audio"]["codec"] == "aac"


def test_probing_is_fast_enough_to_be_reusable(probe, media_files):
    """A thumbnail-sized clip must probe well under a second; this guards regressions."""
    started = time.perf_counter()
    for _ in range(3):
        probe.probe(media_files.landscape)
    assert (time.perf_counter() - started) / 3 < 2.0


# ------------------------------------------------------------------- fixtures
@pytest.fixture()
def probe():
    return ProbeEngine()


@pytest.fixture()
def media_path(media_files):
    def resolve(name: str):
        return getattr(media_files, name)

    return resolve