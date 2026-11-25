"""Transcode profiles: explicit, documented FFmpeg recipes.

Every profile is a dataclass that builds an argv list from a small, typed option set. No
string is ever assembled from user input, and no profile can touch the source: the output
path always comes from :class:`MediaStorage`, which derives it from the content hash.

Profiles are versioned. ``PROFILE_VERSION`` is stored on every derivative row, so changing
a recipe produces new files instead of silently mixing outputs from two recipes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .ffmpeg import EncoderChoice, encoder_args

PROFILE_VERSION = 2

ProfileId = Literal["fast_preview", "editing_proxy", "normalized", "audio_wav", "audio_aac"]

# Library playback ceiling. Both are "at most", never "upscale to": a 480p source stays 480p.
FAST_PREVIEW_MAX_WIDTH = 1280
FAST_PREVIEW_MAX_HEIGHT = 720


class ProfileError(ValueError):
    code = "UNSUPPORTED_PROFILE"
    status = 422


class ProfileNotApplicable(ValueError):
    code = "PROFILE_NOT_APPLICABLE"
    status = 409


@dataclass(slots=True)
class TranscodeProfile:
    id: ProfileId
    label: str
    purpose: str
    container: str
    video_codec: str
    audio_codec: str | None
    max_width: int | None = None
    max_height: int | None = None
    crf: int = 23
    preset: str = "veryfast"
    audio_bitrate_k: int = 128
    fps_cap: int | None = None
    needs_video: bool = True
    version: int = PROFILE_VERSION
    extra: dict[str, Any] = field(default_factory=dict)

    def extension(self) -> str:
        return ".wav" if self.container == "wav" else (".m4a" if self.container == "m4a" else ".mp4")

    def mime_type(self) -> str:
        return {
            "wav": "audio/wav",
            "m4a": "audio/mp4",
        }.get(self.container, "video/mp4")

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": "audio" if not self.needs_video else "video",
            "label": self.label,
            "purpose": self.purpose,
            "container": self.container,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "max_width": self.max_width,
            "max_height": self.max_height,
            "crf": self.crf,
            "preset": self.preset,
            "audio_bitrate_k": self.audio_bitrate_k,
            "fps_cap": self.fps_cap,
            "version": self.version,
            "extension": self.extension(),
            "hardware_capable": self.video_codec == "h264",
        }

    # ------------------------------------------------------------------ argv
    def build_args(
        self,
        *,
        source: str,
        destination: str,
        encoder: EncoderChoice,
        has_audio: bool,
        source_width: int | None = None,
        source_height: int | None = None,
        height_override: int | None = None,
        crf_override: int | None = None,
        fps_override: int | None = None,
        source_fps: float | None = None,
    ) -> list[str]:
        if self.needs_video and not source_width:
            raise ProfileNotApplicable(f"profile {self.id} needs a video stream")
        if not self.needs_video and not has_audio:
            raise ProfileNotApplicable(f"profile {self.id} needs an audio stream")

        crf = crf_override if crf_override is not None else self.crf
        args: list[str] = ["-i", source]

        if self.needs_video:
            target_height = height_override or self.max_height
            target_width = self.max_width
            if height_override and self.max_height:
                # Keep the profile's aspect policy: scale to the requested height only.
                target_height = min(height_override, self.max_height)
            filters: list[str] = []
            # ``min(...)`` keeps small sources small: never upscale, never destroy detail.
            # Two passes rather than one: ffmpeg 9.1 produces zero frames when
            # ``force_original_aspect_ratio=decrease`` is combined with min() expressions in
            # both dimensions (measured), while capping width and then height with ``-2``
            # keeps the aspect ratio and works for every source shape.
            if target_height and target_width:
                filters.append(f"scale=w='min({target_width},iw)':h=-2")
                filters.append(f"scale=w=-2:h='min({target_height},ih)'")
            elif target_height:
                filters.append(f"scale=w=-2:h='min({target_height},ih)'")
            # Only ever lower the frame rate: raising it would duplicate frames and grow the
            # file without adding information.
            fps_cap = fps_override or self.fps_cap
            if fps_cap and source_fps and source_fps > fps_cap:
                filters.append(f"fps={int(fps_cap)}")
            filters.append("setsar=1")
            args += ["-vf", ",".join(filters)]
            args += encoder_args(encoder, crf=crf, preset=self.preset)
            args += ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        else:
            args += ["-vn"]

        if self.audio_codec and has_audio:
            args += ["-c:a", self.audio_codec, "-b:a", f"{self.audio_bitrate_k}k", "-ac", "2"]
        elif self.audio_codec is None:
            args += ["-an"]
        else:
            args += ["-an"]

        args += ["-progress", "pipe:1", "-nostats", destination]
        return args


PROFILES: dict[str, TranscodeProfile] = {
    "fast_preview": TranscodeProfile(
        id="fast_preview",
        label="Fast preview (720p MP4)",
        purpose="Play the asset smoothly inside the library without touching the original.",
        container="mp4",
        video_codec="h264",
        audio_codec="aac",
        max_width=FAST_PREVIEW_MAX_WIDTH,
        max_height=FAST_PREVIEW_MAX_HEIGHT,
        crf=23,
        preset="veryfast",
        audio_bitrate_k=128,
    ),
    "editing_proxy": TranscodeProfile(
        id="editing_proxy",
        label="Editing proxy (480p MP4)",
        purpose="A light, consistent copy for scrubbing and cutting on this laptop.",
        container="mp4",
        video_codec="h264",
        audio_codec="aac",
        max_height=480,
        crf=28,
        preset="veryfast",
        audio_bitrate_k=96,
        fps_cap=30,
    ),
    "normalized": TranscodeProfile(
        id="normalized",
        label="Normalised working copy (H.264/AAC, up to 1080p30)",
        purpose="A compatible working copy with a predictable frame rate for later stages.",
        container="mp4",
        video_codec="h264",
        audio_codec="aac",
        max_height=1080,
        crf=21,
        preset="medium",
        audio_bitrate_k=160,
        fps_cap=30,
    ),
    "audio_wav": TranscodeProfile(
        id="audio_wav",
        label="Audio extraction (WAV 48 kHz)",
        purpose="Lossless audio track for transcription and loudness work.",
        container="wav",
        video_codec="none",
        audio_codec="pcm_s16le",
        needs_video=False,
        audio_bitrate_k=0,
    ),
    "audio_aac": TranscodeProfile(
        id="audio_aac",
        label="Audio extraction (AAC 192 kbps)",
        purpose="Compact audio track for review and sharing.",
        container="m4a",
        video_codec="none",
        audio_codec="aac",
        needs_video=False,
        audio_bitrate_k=192,
    ),
}


def get_profile(profile_id: str) -> TranscodeProfile:
    profile = PROFILES.get(profile_id)
    if profile is None:
        raise ProfileError(f"unknown transcode profile {profile_id!r}; known: {', '.join(sorted(PROFILES))}")
    return profile


def list_profiles() -> list[dict[str, Any]]:
    return [profile.describe() for profile in PROFILES.values()]


def recommend_profile(
    *,
    kind: str,
    width: int | None,
    height: int | None,
    duration_ms: int | None,
    has_audio: bool,
) -> dict[str, Any]:
    """Deterministic profile suggestion based on measured facts, with the reason stated.

    This is what the transcode agent is allowed to answer: a rule over probe values, not a
    claim that a model "watched" the video.
    """
    if kind == "audio":
        return {
            "profile": "audio_wav",
            "reason": "the asset is audio-only, so extraction keeps the full-quality track",
            "confidence": 0.9,
        }
    long_edge = max(width or 0, height or 0)
    minutes = (duration_ms or 0) / 60000
    if minutes >= 8:
        return {
            "profile": "editing_proxy",
            "reason": f"{minutes:.1f} min is long enough that a 480p proxy saves real time on this laptop",
            "confidence": 0.8,
        }
    if long_edge >= 1900 and minutes >= 3:
        return {
            "profile": "editing_proxy",
            "reason": f"{long_edge}px and {minutes:.1f} min is heavy for this laptop; a 480p proxy keeps editing responsive",
            "confidence": 0.8,
        }
    if long_edge and long_edge <= 1080 and minutes < 5:
        return {
            "profile": "fast_preview",
            "reason": f"{long_edge}px source under 5 min only needs a light preview transcode",
            "confidence": 0.75,
        }
    return {
        "profile": "normalized",
        "reason": "mixed dimensions or length; a normalised working copy is the safest base",
        "confidence": 0.6,
    }


__all__ = [
    "PROFILES",
    "PROFILE_VERSION",
    "ProfileError",
    "ProfileId",
    "ProfileNotApplicable",
    "TranscodeProfile",
    "get_profile",
    "list_profiles",
    "recommend_profile",
]
