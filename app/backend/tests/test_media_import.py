"""Import tests: real files, real hashes, real duplicates, real failures."""
from __future__ import annotations

import hashlib

import pytest

from creatoros.db.enums import IntakeState, MediaKind
from creatoros.db.models import MediaAsset, MediaDerivative, Transcript
from creatoros.services.media.library import AssetNotFound, MediaLibrary
from creatoros.services.media.storage import MediaStorage


def test_import_video_records_real_facts(library, media_files):
    outcome = library.import_file(media_files.landscape)
    assert outcome.status == "imported"
    asset = library.get_asset(outcome.asset_id)

    expected = hashlib.sha256(media_files.landscape.read_bytes()).hexdigest()
    assert asset.sha256 == expected
    assert asset.kind == MediaKind.VIDEO
    assert asset.intake_state == IntakeState.READY
    assert asset.width == 640 and asset.height == 360
    assert asset.duration_ms == pytest.approx(2000, abs=150)
    assert asset.video_codec == "h264"
    assert asset.has_audio is True
    assert asset.mime_type == "video/mp4"
    assert asset.probe_version
    assert asset.probe_json["video"]["width"] == 640

    stored = library.asset_file(asset)
    assert stored.exists()
    # The stored copy is a different file with identical bytes, named after its hash.
    assert stored.name == f"{expected}.mp4"
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == expected
    # The original is untouched.
    assert media_files.landscape.exists()
    assert hashlib.sha256(media_files.landscape.read_bytes()).hexdigest() == expected


def test_import_image_and_audio(library, media_files):
    image = library.import_file(media_files.image)
    assert image.status == "imported"
    image_asset = library.get_asset(image.asset_id)
    assert image_asset.kind == MediaKind.IMAGE
    assert image_asset.width == 800 and image_asset.height == 600
    assert image_asset.duration_ms is None

    audio = library.import_file(media_files.audio)
    assert audio.status == "imported"
    audio_asset = library.get_asset(audio.asset_id)
    assert audio_asset.kind == MediaKind.AUDIO
    assert audio_asset.width is None
    assert audio_asset.has_audio is True


def test_duplicate_content_is_detected_by_hash_not_name(library, media_files, tmp_path):
    first = library.import_file(media_files.portrait)
    assert first.status == "imported"

    renamed = tmp_path / "completely-different-name.mp4"
    renamed.write_bytes(media_files.portrait.read_bytes())
    second = library.import_file(renamed)

    assert second.status == "duplicate"
    assert second.asset_id == first.asset_id
    assert second.sha256 == first.sha256
    asset = library.get_asset(first.asset_id)
    # The original metadata name is preserved; the alias is recorded, not used as a path.
    assert asset.original_filename == "portrait.mp4"
    assert "completely-different-name.mp4" in (asset.probe_json or {}).get("duplicate_filenames", [])


def test_same_name_different_content_is_a_new_asset(library, media_files, tmp_path):
    first = library.import_file(media_files.landscape)
    other = tmp_path / "landscape.mp4"
    other.write_bytes(media_files.portrait.read_bytes())
    second = library.import_file(other, original_filename="landscape.mp4")
    assert second.status == "imported"
    assert second.asset_id != first.asset_id
    # Same display name is allowed: the stored path comes from the hash.
    assert library.get_asset(second.asset_id).original_filename == "landscape.mp4"


def test_import_is_idempotent_when_repeated(library, media_files):
    first = library.import_file(media_files.image)
    second = library.import_file(media_files.image)
    assert first.status == "imported"
    assert second.status == "duplicate"
    assert second.asset_id == first.asset_id
    assert library.session.query(MediaAsset).count() == 1


def test_unreadable_file_is_rejected_and_never_becomes_an_asset(library, media_files):
    outcome = library.import_file(media_files.fake)
    assert outcome.status == "rejected"
    assert outcome.asset_id is None
    assert "unreadable" in (outcome.reason or "")
    assert library.session.query(MediaAsset).count() == 0
    # The bytes are kept in quarantine for inspection instead of being silently dropped.
    quarantine = library.storage.area_dir("quarantine")
    assert any(quarantine.rglob("*"))


def test_truncated_file_is_rejected(library, media_files):
    outcome = library.import_file(media_files.truncated)
    assert outcome.status == "rejected"
    assert library.session.query(MediaAsset).count() == 0


def test_import_refuses_a_file_over_the_size_policy(library, media_files):
    library.storage.max_import_bytes = 1024
    outcome = library.import_file(media_files.landscape)
    assert outcome.status == "rejected"
    assert "limit" in (outcome.reason or "")


def test_import_reports_progress_stages(library, media_files):
    stages: list[str] = []

    def progress(fraction: float, info):
        stage = info.get("stage")
        if stage and (not stages or stages[-1] != stage):
            stages.append(stage)
        assert 0.0 <= fraction <= 1.0

    library.import_file(media_files.landscape, progress=progress)
    assert stages[0] == "copy"
    assert "probe" in stages
    assert stages[-1] == "done"


def test_import_can_be_cancelled_before_it_finishes(library, media_files):
    calls = {"n": 0}

    def cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    outcome = library.import_file(media_files.landscape, should_cancel=cancel)
    assert outcome.status == "rejected"
    assert library.session.query(MediaAsset).count() == 0
    assert list(library.storage.area_dir("staging").glob("*.part")) == []


def test_imported_asset_gets_a_real_thumbnail_and_waveform(library, media_files):
    outcome = library.import_file(media_files.landscape)
    asset = library.get_asset(outcome.asset_id)
    derivatives = {str(row.kind): row for row in library.derivatives(asset.id)}
    assert "thumbnail" in derivatives
    thumb = derivatives["thumbnail"]
    assert thumb.state.value == "ready"
    assert thumb.size_bytes > 0
    assert library.derivative_file(thumb).exists()
    assert thumb.width and thumb.height
    # 480px long edge, never upscaled beyond the source.
    assert max(thumb.width, thumb.height) <= 480
    assert "waveform" in derivatives


def test_thumbnail_is_not_regenerated_on_reimport(library, media_files):
    first = library.import_file(media_files.image)
    asset = library.get_asset(first.asset_id)
    thumb = library.primary_thumbnail(asset.id)
    before = (thumb.id, thumb.sha256, library.derivative_file(thumb).stat().st_mtime_ns)
    library.import_file(media_files.image)  # duplicate → no second import
    after_thumb = library.primary_thumbnail(asset.id)
    after = (after_thumb.id, after_thumb.sha256, library.derivative_file(after_thumb).stat().st_mtime_ns)
    assert before == after


def test_missing_source_after_import_is_reported(library, media_files):
    outcome = library.import_file(media_files.silent)
    asset = library.get_asset(outcome.asset_id)
    library.asset_file(asset).unlink()
    with pytest.raises(Exception) as excinfo:
        library.asset_file(asset)
    assert "missing" in str(excinfo.value)


def test_unknown_asset_raises_not_found(library):
    with pytest.raises(AssetNotFound):
        library.get_asset("01M80ZZZZZZZZZZZZZZZZZZZZZ")


def test_original_bytes_never_change_during_processing(library, media_files):
    outcome = library.import_file(media_files.portrait)
    asset = library.get_asset(outcome.asset_id)
    original = library.asset_file(asset)
    before = hashlib.sha256(original.read_bytes()).hexdigest()
    library.ensure_thumbnail(asset, force=True)
    library.transcode(asset, "fast_preview")
    after = hashlib.sha256(original.read_bytes()).hexdigest()
    assert before == after == asset.sha256


def test_delete_asset_removes_files_and_rows(library, media_files):
    outcome = library.import_file(media_files.image)
    asset = library.get_asset(outcome.asset_id)
    original = library.asset_file(asset)
    derivatives = library.derivatives(asset.id)
    assert derivatives
    for row in derivatives:
        library.delete_derivative(row)
    original.unlink()
    library.session.delete(asset)
    library.session.commit()
    assert not original.exists()
    assert library.session.query(MediaDerivative).count() == 0


def test_transcript_reference_survives_asset_lookup(library, media_files):
    """The Phase 2 tables still work with the new columns present."""
    outcome = library.import_file(media_files.audio_wav)
    asset = library.get_asset(outcome.asset_id)
    library.session.add(Transcript(asset_id=asset.id, text="hello", segments_json=[]))
    library.session.commit()
    assert library.session.query(Transcript).filter_by(asset_id=asset.id).count() == 1


def test_storage_can_be_pointed_at_a_custom_policy(library, media_files):
    strict = MediaStorage(min_free_gb=10**6)
    original = library.storage
    library.storage = strict
    try:
        outcome = library.import_file(media_files.image)
        assert outcome.status == "rejected"
        assert "GiB" in (outcome.reason or "")
    finally:
        library.storage = original


@pytest.fixture()
def library(session):
    return MediaLibrary(session)
