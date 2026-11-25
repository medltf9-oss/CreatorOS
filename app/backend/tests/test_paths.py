import pytest

from creatoros.core.paths import PathPolicyError, is_inside_root, resolve_under_root

BAD_ESCAPES = [
    "../escape.mp4",
    "..\\escape.mp4",
    "a/../../b.mp4",
    "C:\\Windows\\x.mp4",
    "\\\\server\\share\\x.mp4",
    "/etc/passwd",
]


def test_resolves_inside_root():
    path = resolve_under_root("media_incoming", "clip.mp4")
    assert is_inside_root(path)
    assert path.name == "clip.mp4"


@pytest.mark.parametrize("bad", BAD_ESCAPES)
def test_rejects_escapes(bad):
    with pytest.raises(PathPolicyError):
        resolve_under_root("media_incoming", bad)


@pytest.mark.parametrize("bad", ["CON", "nul.mp4", "aux.txt", "com1", "lpt9.mov"])
def test_rejects_reserved_names(bad):
    with pytest.raises(PathPolicyError):
        resolve_under_root("media_incoming", bad)


@pytest.mark.parametrize("bad", ["bad:name.mp4", "what?.mp4", "star*.mp4", 'quote".mp4'])
def test_rejects_illegal_chars(bad):
    with pytest.raises(PathPolicyError):
        resolve_under_root("media_incoming", bad)


def test_rejects_trailing_dot_and_space():
    # Windows silently strips trailing dots/spaces from a path component, which can
    # make two different user-supplied names collide. Reject them explicitly.
    for bad in ("screenshot.", "screenshot "):
        with pytest.raises(PathPolicyError):
            resolve_under_root("media_incoming", bad)


def test_nested_ok():
    path = resolve_under_root("media_exports", "proj-1", "instagram", "reel.mp4")
    assert is_inside_root(path)
    assert path.parent.name == "instagram"
