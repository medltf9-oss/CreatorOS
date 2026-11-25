"""Per-platform publishing limits, transcribed from the provider documentation.

Every number in this file has a documentation URL next to it. Where the providers do not publish a
limit, the value is ``None`` and the validator stays quiet: a made-up limit that rejects a file the
provider would have accepted is worse than letting the provider answer for itself.

The rules are keyed by platform *and* publish mode, because the four modes have different windows:
a 15 minute video is fine as a Facebook Page post and impossible as an Instagram Reel, and TikTok's
Direct Post has a duration ceiling that the creator's own account information can lower further.

Sources (checked when this file was written, recorded so a reader can re-check):

* Facebook Page video: https://developers.facebook.com/docs/video-api/guides/publishing
* Instagram content publishing: https://developers.facebook.com/docs/instagram-platform/content-publishing
* TikTok content posting API: https://developers.tiktok.com/doc/content-posting-api-get-started
* TikTok media transfer guide: https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .....db.enums import PublishMode

FACEBOOK_VIDEO_DOCS = "https://developers.facebook.com/docs/video-api/guides/publishing"
INSTAGRAM_PUBLISH_DOCS = "https://developers.facebook.com/docs/instagram-platform/content-publishing"
TIKTOK_POSTING_DOCS = "https://developers.tiktok.com/doc/content-posting-api-get-started"
TIKTOK_TRANSFER_DOCS = "https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide"

GIB = 1024**3


@dataclass(frozen=True, slots=True)
class TextLimits:
    """Caption and title limits.

    ``None`` means "no local cap": either the provider documents no limit, or the limit is not
    something this project is willing to invent. The provider's own error is surfaced by the
    executor, which is honest, whereas a wrong local cap silently blocks valid content.
    """

    caption_max: int | None
    title_max: int | None
    supports_title: bool
    hashtags_max: int | None
    caption_required: bool
    docs_url: str


@dataclass(frozen=True, slots=True)
class MediaLimits:
    """What the platform will accept as a video file.

    Durations are milliseconds and sizes are bytes so no unit conversion can be wrong at a boundary.
    Aspect ratio is width/height, so 9:16 is 0.5625 and a *hard* range is inclusive.
    """

    containers: frozenset[str]
    video_codecs: frozenset[str]
    audio_codecs: frozenset[str]
    audio_required: bool
    pixel_formats: frozenset[str]
    min_short_side: int
    max_short_side: int
    max_long_side: int
    aspect_min: float
    aspect_max: float
    preferred_aspect: float
    preferred_aspect_tolerance: float
    min_duration_ms: int
    max_duration_ms: int
    warn_over_duration_ms: int | None
    max_bytes: int
    docs_url: str


@dataclass(frozen=True, slots=True)
class PlatformRules:
    """Everything the validator needs to know about one (platform, mode) pair."""

    platform: str
    mode: PublishMode
    text: TextLimits
    media: MediaLimits
    docs_url: str
    #: Instagram and TikTok need the provider to fetch the bytes from a public URL; Facebook gets a
    #: direct upload. The composer uses this to decide whether a delivery provider is mandatory.
    requires_public_url: bool
    #: Options the platform understands, so unknown keys can be reported instead of ignored.
    known_options: frozenset[str]


_MP4_FAMILY = frozenset({"mp4", "mov", "m4a", "3gp", "3g2", "mj2"})
_H264 = frozenset({"h264", "avc1"})
_AAC = frozenset({"aac", "mp4a"})
_YUV420 = frozenset({"yuv420p", "yuvj420p"})

_FACEBOOK_TEXT = TextLimits(
    # Graph has no documented caption cap for Page videos; the provider rejects what it dislikes.
    caption_max=None,
    title_max=None,
    supports_title=True,
    hashtags_max=None,
    caption_required=False,
    docs_url=FACEBOOK_VIDEO_DOCS,
)
_FACEBOOK_MEDIA = MediaLimits(
    containers=_MP4_FAMILY,
    video_codecs=_H264,
    audio_codecs=_AAC,
    audio_required=False,
    pixel_formats=_YUV420,
    # "at least 240p, at most 1080p" is a statement about the short side of the frame.
    min_short_side=240,
    max_short_side=1080,
    max_long_side=1920,
    aspect_min=0.5,
    aspect_max=1.92,
    preferred_aspect=16 / 9,
    preferred_aspect_tolerance=0.02,
    min_duration_ms=1_000,
    max_duration_ms=240 * 60 * 1000,
    warn_over_duration_ms=None,
    max_bytes=4 * GIB,
    docs_url=FACEBOOK_VIDEO_DOCS,
)

_INSTAGRAM_TEXT = TextLimits(
    caption_max=2_200,
    # Instagram has no title field at all; a title is reported as unsupported rather than dropped.
    title_max=None,
    supports_title=False,
    hashtags_max=30,
    caption_required=False,
    docs_url=INSTAGRAM_PUBLISH_DOCS,
)
_INSTAGRAM_MEDIA = MediaLimits(
    containers=_MP4_FAMILY,
    video_codecs=_H264,
    audio_codecs=_AAC,
    audio_required=True,
    pixel_formats=_YUV420,
    min_short_side=540,
    max_short_side=1920,
    max_long_side=1920,
    aspect_min=0.5,
    aspect_max=0.6,
    preferred_aspect=9 / 16,
    preferred_aspect_tolerance=0.02,
    min_duration_ms=3_000,
    # The publishing API accepts up to 15 minutes; the Reels *surface* stops recommending at 90 s
    # and that is a warning, never a failure, because the API will still publish it.
    max_duration_ms=15 * 60 * 1000,
    warn_over_duration_ms=90_000,
    max_bytes=GIB,
    docs_url=INSTAGRAM_PUBLISH_DOCS,
)

_TIKTOK_TEXT = TextLimits(
    # The Direct Post `title` field carries the caption and its documented cap is 2,200 characters.
    caption_max=2_200,
    title_max=2_200,
    supports_title=True,
    hashtags_max=None,
    caption_required=True,
    docs_url=TIKTOK_POSTING_DOCS,
)
_TIKTOK_MEDIA = MediaLimits(
    containers=_MP4_FAMILY,
    video_codecs=_H264,
    audio_codecs=_AAC,
    audio_required=False,
    pixel_formats=_YUV420,
    min_short_side=360,
    max_short_side=4096,
    max_long_side=4096,
    aspect_min=0.5,
    aspect_max=1.0,
    preferred_aspect=9 / 16,
    preferred_aspect_tolerance=0.02,
    min_duration_ms=3_000,
    max_duration_ms=10 * 60 * 1000,
    warn_over_duration_ms=None,
    max_bytes=4 * GIB,
    docs_url=TIKTOK_TRANSFER_DOCS,
)

_FACEBOOK_OPTIONS = frozenset({"published", "is_explicit", "crossposted_video_id"})
_INSTAGRAM_OPTIONS = frozenset({"share_to_feed", "thumb_offset", "audio_name", "collaborators"})
_TIKTOK_OPTIONS = frozenset(
    {
        "privacy_level",
        "disable_comment",
        "disable_duet",
        "disable_stitch",
        "video_cover_timestamp_ms",
        "is_aigc",
        "brand_content_toggle",
        "brand_organic_toggle",
    }
)

PLATFORM_RULES: Mapping[tuple[str, PublishMode], PlatformRules] = {
    ("facebook", PublishMode.VIDEO): PlatformRules(
        platform="facebook",
        mode=PublishMode.VIDEO,
        text=_FACEBOOK_TEXT,
        media=_FACEBOOK_MEDIA,
        docs_url=FACEBOOK_VIDEO_DOCS,
        requires_public_url=False,
        known_options=_FACEBOOK_OPTIONS,
    ),
    ("instagram", PublishMode.REELS): PlatformRules(
        platform="instagram",
        mode=PublishMode.REELS,
        text=_INSTAGRAM_TEXT,
        media=_INSTAGRAM_MEDIA,
        docs_url=INSTAGRAM_PUBLISH_DOCS,
        requires_public_url=True,
        known_options=_INSTAGRAM_OPTIONS,
    ),
    ("tiktok", PublishMode.DIRECT_POST): PlatformRules(
        platform="tiktok",
        mode=PublishMode.DIRECT_POST,
        text=_TIKTOK_TEXT,
        media=_TIKTOK_MEDIA,
        docs_url=TIKTOK_POSTING_DOCS,
        requires_public_url=False,
        known_options=_TIKTOK_OPTIONS,
    ),
    ("tiktok", PublishMode.INBOX): PlatformRules(
        platform="tiktok",
        mode=PublishMode.INBOX,
        text=_TIKTOK_TEXT,
        media=_TIKTOK_MEDIA,
        docs_url=TIKTOK_POSTING_DOCS,
        requires_public_url=False,
        known_options=_TIKTOK_OPTIONS,
    ),
}


def rules_for(platform: str, mode: PublishMode | str) -> PlatformRules:
    """Look up the rules, or say clearly that this pair has none."""
    resolved = mode if isinstance(mode, PublishMode) else PublishMode(str(mode))
    key = (str(platform).strip().lower(), resolved)
    try:
        return PLATFORM_RULES[key]
    except KeyError as exc:  # pragma: no cover - guarded by callers, kept as a loud failure
        raise KeyError(f"no publishing rules for {key[0]}/{key[1].value}") from exc


def mode_for_platform(platform: str) -> PublishMode:
    """The single mode a platform uses when the caller does not care to choose one."""
    normalized = str(platform).strip().lower()
    if normalized == "facebook":
        return PublishMode.VIDEO
    if normalized == "instagram":
        return PublishMode.REELS
    if normalized == "tiktok":
        return PublishMode.DIRECT_POST
    raise KeyError(f"no default publish mode for {normalized}")
