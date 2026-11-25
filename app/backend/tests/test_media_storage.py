"""Storage policy tests: deterministic paths, atomic moves, bounded copies, space checks."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from creatoros.core.paths import PathPolicyError, resolve_under_root
from creatoros.services.media.storage import (
    FileTooLarge,
    InsufficientStorage,
    MediaStorage,
    MediaStorageError,
    UnsafePath,
    normalise_extension,
    sanitise_display_name,
)

SHA = "a" * 64


def test_original_path_is_derived_from_the_hash_only(storage):
    path = storage.original_path(SHA, ".mp4")
    assert path.name == f"{SHA}.mp4"
    assert path.parent.name == "aa"
    # Same hash and extension → same path, whatever the user called the file.
    assert storage.original_path(SHA, "mp4") == path


def test_original_path_rejects_a_fake_hash(storage):
    with pytest.raises(UnsafePath):
        storage.original_path("../../etc/passwd", ".mp4")
    with pytest.raises(UnsafePath):
        storage.original_path("zz" * 32, ".mp4")


def test_extension_is_normalised_and_validated(storage):
    assert normalise_extension("Movie.MP4") == ".mp4"
    assert normalise_extension("noext") == ".bin"
    assert normalise_extension("evil.<script>") == ".bin"
    with pytest.raises(UnsafePath):
        storage.original_path(SHA, "../evil")


def test_derivative_paths_cannot_live_in_originals(storage):
    with pytest.raises(UnsafePath):
        storage.derivative_path(area="originals", sha256=SHA, kind="thumb", profile="p", version=1, ext=".webp")


def test_derivative_path_is_a_cache_key(storage):
    first = storage.derivative_path(area="thumbnails", sha256=SHA, kind="primary", profile="480px", version=2, ext=".webp")
    second = storage.derivative_path(area="thumbnails", sha256=SHA, kind="primary", profile="480px", version=2, ext=".webp")
    newer = storage.derivative_path(area="thumbnails", sha256=SHA, kind="primary", profile="480px", version=3, ext=".webp")
    assert first == second
    assert first != newer


def test_staging_names_are_unique_and_inside_staging(storage):
    first = storage.staging_path("clip", extension=".mp4")
    second = storage.staging_path("clip", extension=".mp4")
    assert first != second
    assert first.parent.name == "staging"
    assert first.suffix == ".mp4"
    with pytest.raises(UnsafePath):
        storage.staging_path("clip", extension="../../evil")


def test_display_names_are_sanitised_but_paths_never_use_them():
    assert sanitise_display_name("C:\\Users\\x\\clip.mp4") == "clip.mp4"
    assert sanitise_display_name("../../etc/passwd") == "passwd"
    assert sanitise_display_name("bad\x00name.mp4").endswith("name.mp4")
    assert sanitise_display_name("   ") == "unnamed"


def test_copy_computes_the_real_hash_and_reports_progress(storage, media_files):
    seen: list[tuple[int, int]] = []
    staged = storage.copy_to_staging(
        media_files.landscape, hint="land", progress=lambda done, total: seen.append((done, total))
    )
    try:
        expected = hashlib.sha256(media_files.landscape.read_bytes()).hexdigest()
        assert staged.sha256 == expected
        assert staged.size_bytes == media_files.landscape.stat().st_size
        assert seen and seen[-1][0] == seen[-1][1]
        assert staged.path.parent.name == "staging"
    finally:
        staged.discard()


def test_copy_can_be_cancelled_and_leaves_no_partial_file(storage, media_files):
    calls = {"n": 0}

    def cancel_after_first_chunk() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    with pytest.raises(MediaStorageError):
        storage.copy_to_staging(media_files.landscape, should_cancel=cancel_after_first_chunk)
    leftovers = list(storage.area_dir("staging").glob("*.part"))
    assert leftovers == []


def test_promote_is_atomic_and_idempotent(storage, media_files):
    staged = storage.copy_to_staging(media_files.landscape, hint="promote")
    destination = storage.promote(staged, extension=".mp4")
    assert destination.exists()
    assert destination.name == f"{staged.sha256}.mp4"
    assert not staged.path.exists()

    # A second promotion of the same bytes keeps the canonical file and drops the copy.
    staged2 = storage.copy_to_staging(media_files.landscape, hint="promote")
    again = storage.promote(staged2, extension=".mp4")
    assert again == destination
    assert not staged2.path.exists()


def test_source_policy_rejects_symlinks_empty_and_oversized(storage, media_files, tmp_path):
    link = tmp_path / "link.mp4"
    try:
        link.symlink_to(media_files.landscape)
    except (OSError, NotImplementedError):  # pragma: no cover - needs privilege on Windows
        pytest.skip("symlinks are not available on this account")
    with pytest.raises(UnsafePath):
        storage.check_source(link)

    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(MediaStorageError):
        storage.check_source(empty)

    tiny_budget = MediaStorage(max_import_bytes=100)
    with pytest.raises(FileTooLarge):
        tiny_budget.check_source(media_files.landscape)


def test_space_guard_refuses_an_impossible_import(storage):
    with pytest.raises(InsufficientStorage):
        storage.require_space(10**15)


def test_usage_and_staging_sweep(storage, media_files):
    staged = storage.copy_to_staging(media_files.audio, hint="sweep")
    assert storage.usage()["staging"]["files"] >= 1
    swept = storage.sweep_staging(older_than_seconds=0)
    assert swept["removed"] >= 1
    assert not staged.path.exists()


def test_resolve_stored_refuses_an_escaping_path(storage):
    with pytest.raises(UnsafePath):
        storage.resolve_stored(Path("C:/Windows/System32/drivers/etc/hosts"))


def test_sanitise_extension_of_a_real_upload_never_keeps_the_original_name(storage, media_files):
    staged = storage.copy_to_staging(media_files.image, hint="x")
    try:
        path = storage.original_path(staged.sha256, normalise_extension("my holiday (1).JPG"))
        assert path.name == f"{staged.sha256}.jpg"
        assert "holiday" not in str(path)
    finally:
        staged.discard()


def test_long_hints_are_truncated_and_resolved_inside_the_root(storage):
    path = storage.staging_path("x" * 200, extension=".mp4")
    assert len(path.stem) <= 60
    assert path.exists() is False
    assert path.parent.name == "staging"


def test_path_policy_rejects_absolute_hints(storage):
    with pytest.raises(PathPolicyError):
        resolve_under_root("media_staging", r"C:\Windows\evil.mp4")


@pytest.fixture()
def storage():
    return MediaStorage()
