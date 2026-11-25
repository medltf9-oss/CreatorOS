"""FFprobe integration: turn a media file into validated facts.

Design rules that come from real failures seen in earlier phases:

* Never trust the extension. A ``.mp4`` that is actually a text file must be rejected by
  the probe, not accepted because the name looks right.
* Never trust ffprobe's JSON shape. Streams can be missing, ``avg_frame_rate`` can be
  ``"0/0"``, ``duration`` can be ``"N/A"``, timestamps can be malformed.
* Never return partial garbage: every number is parsed, checked for finiteness and range,
  and either accepted or dropped. The caller gets a typed :class:`ProbeResult` plus a list
  of warnings explaining what was missing.
* Always run with a timeout and a bounded output size. A corrupt file can make ffprobe
  emit megabytes of errors or hang on a broken stream.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from ...core.logging import get_logger
from ...core.settings import get_settings

log = get_logger(__name__)

PROBE_VERSION = "p5.1"
"""Bumped when the parser's meaning changes, so cached probe documents can be invalidated."""

MediaKind = Literal["video", "audio", "image", "unknown"]

# Textual containers that ffprobe reports, mapped to a kind when no image stream exists.
_CONTAINER_KINDS: dict[str, MediaKind] = {
    "mp3": "audio",
    "wav": "audio",
    "flac": "audio",
    "ogg": "audio",
    "aac": "audio",
    "mov": "video",
    "mp4": "video",
    "matroska": "video",
    "webm": "video",
    "avi": "video",
    "mpeg": "video",
    "mpegts": "video",
    "flv": "video",
    "asf": "video",
}

_MIME_BY_FORMAT: dict[str, str] = {
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "matroska": "video/x-matroska",
    "webm": "video/webm",
    "avi": "video/x-msvideo",
    "mpeg": "video/mpeg",
    "mpegts": "video/mp2t",
    "flv": "video/x-flv",
    "asf": "video/x-ms-wmv",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "aac": "audio/aac",
    "image2": "image/jpeg",
    "png_pipe": "image/png",
    "webp_pipe": "image/webp",
    "gif": "image/gif",
    "jpeg_pipe": "image/jpeg",
}

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.\-]{1,48}$")


class ProbeError(RuntimeError):
    code = "MEDIA_PROBE_FAILED"
    status = 422

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.detail = detail


class ProbeTimeout(ProbeError):
    code = "MEDIA_PROBE_TIMEOUT"
    status = 504


class ProbeBinaryMissing(ProbeError):
    code = "FFPROBE_MISSING"
    status = 503


class UnsupportedMedia(ProbeError):
    code = "UNSUPPORTED_MEDIA"
    status = 415


@dataclass(slots=True)
class VideoStream:
    index: int
    codec: str | None
    width: int | None
    height: int | None
    pixel_format: str | None
    fps: float | None
    avg_fps: float | None
    bitrate: int | None
    rotation: int
    color_space: str | None
    color_transfer: str | None
    color_primaries: str | None
    is_hdr: bool
    frame_count: int | None
    duration_ms: int | None
    display_width: int | None = None
    display_height: int | None = None
    aspect_ratio: str | None = None


@dataclass(slots=True)
class AudioStream:
    index: int
    codec: str | None
    sample_rate: int | None
    channels: int | None
    channel_layout: str | None
    bitrate: int | None
    duration_ms: int | None


@dataclass(slots=True)
class ProbeResult:
    path: str
    size_bytes: int
    kind: MediaKind
    format_name: str | None
    format_long_name: str | None
    mime_type: str | None
    duration_ms: int | None
    bitrate: int | None
    video: VideoStream | None
    audio: AudioStream | None
    streams_total: int
    creation_time: str | None
    tags: dict[str, str]
    width: int | None
    height: int | None
    fps: float | None
    rotation: int
    orientation: str | None
    has_audio: bool
    is_hdr: bool
    probe_version: str = PROBE_VERSION
    elapsed_ms: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "probe_version": self.probe_version,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "kind": self.kind,
            "format_name": self.format_name,
            "format_long_name": self.format_long_name,
            "mime_type": self.mime_type,
            "duration_ms": self.duration_ms,
            "bitrate": self.bitrate,
            "streams_total": self.streams_total,
            "creation_time": self.creation_time,
            "tags": self.tags,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "rotation": self.rotation,
            "orientation": self.orientation,
            "has_audio": self.has_audio,
            "is_hdr": self.is_hdr,
            "elapsed_ms": self.elapsed_ms,
            "warnings": self.warnings,
            "video": None
            if self.video is None
            else {
                "index": self.video.index,
                "codec": self.video.codec,
                "width": self.video.width,
                "height": self.video.height,
                "display_width": self.video.display_width,
                "display_height": self.video.display_height,
                "pixel_format": self.video.pixel_format,
                "fps": self.video.fps,
                "avg_fps": self.video.avg_fps,
                "bitrate": self.video.bitrate,
                "rotation": self.video.rotation,
                "aspect_ratio": self.video.aspect_ratio,
                "color_space": self.video.color_space,
                "color_transfer": self.video.color_transfer,
                "color_primaries": self.video.color_primaries,
                "is_hdr": self.video.is_hdr,
                "frame_count": self.video.frame_count,
                "duration_ms": self.video.duration_ms,
            },
            "audio": None
            if self.audio is None
            else {
                "index": self.audio.index,
                "codec": self.audio.codec,
                "sample_rate": self.audio.sample_rate,
                "channels": self.audio.channels,
                "channel_layout": self.audio.channel_layout,
                "bitrate": self.audio.bitrate,
                "duration_ms": self.audio.duration_ms,
            },
        }


# ------------------------------------------------------------------ parsing helpers
def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _int_in(value: Any, *, low: int, high: int) -> int | None:
    number = _finite(value)
    if number is None:
        return None
    integer = int(number)
    if integer < low or integer > high:
        return None
    return integer


def parse_rate(value: Any) -> float | None:
    """Parse ffprobe frame rates: ``"30000/1001"``, ``"25"``, ``"0/0"``, ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in ("0/0", "N/A"):
        return None
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        num = _finite(numerator)
        den = _finite(denominator)
        if not num or not den or den == 0:
            return None
        rate = num / den
    else:
        rate = _finite(text)
        if rate is None:
            return None
    # Anything outside 1..240 fps is not a plausible video frame rate; treat as unknown
    # rather than carrying a nonsense number into a transcode decision.
    if not 1.0 <= rate <= 240.0:
        return None
    return round(rate, 4)


def parse_duration_seconds(value: Any) -> float | None:
    """Parse ``"12.345000"`` or ``"N/A"`` into a bounded, finite number of seconds."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in ("N/A", "NAN"):
        return None
    if ":" in text:  # "00:01:23.456000"
        parts = text.split(":")
        if len(parts) == 3:
            try:
                hours, minutes, seconds = (float(part) for part in parts)
            except ValueError:
                return None
            text = str(hours * 3600 + minutes * 60 + seconds)
    seconds = _finite(text)
    if seconds is None or seconds < 0:
        return None
    return seconds


def parse_rotation(stream: dict[str, Any]) -> int:
    for side_data in stream.get("side_data_list") or []:
        if isinstance(side_data, dict) and "rotation" in side_data:
            rotation = _int_in(side_data.get("rotation"), low=-360, high=360)
            if rotation is not None:
                return rotation % 360
    tags = stream.get("tags") or {}
    if isinstance(tags, dict):
        raw = tags.get("rotate")
        rotation = _int_in(raw, low=-360, high=360)
        if rotation is not None:
            return rotation % 360
    return 0


def classify(
    *, format_name: str | None, video: dict[str, Any] | None, audio: dict[str, Any] | None
) -> MediaKind:
    """Decide what a file really is from its streams, falling back to the container."""
    if video is not None:
        codec = str(video.get("codec_name") or "")
        # Still images decoded by image2/png/gif demuxers come back as a video stream with
        # exactly one frame and no frame rate. Treat them as images, not one-frame videos.
        is_still = (
            codec in {"mjpeg", "png", "bmp", "gif", "webp", "tiff", "qoi"}
            and (video.get("nb_frames") in (None, "1", 1))
            and parse_rate(video.get("avg_frame_rate")) is None
        ) or (format_name or "").split(",")[0] in {"image2", "png_pipe", "webp_pipe", "jpeg_pipe", "gif"}
        return "image" if is_still else "video"
    if audio is not None:
        return "audio"
    first = (format_name or "").split(",")[0]
    return _CONTAINER_KINDS.get(first, "unknown")


def _clean_tags(tags: Any) -> dict[str, str]:
    if not isinstance(tags, dict):
        return {}
    cleaned: dict[str, str] = {}
    for key, value in tags.items():
        name = str(key)[:60]
        text = str(value)[:400]
        if name and text:
            cleaned[name] = text
    return cleaned


def _safe_text(value: Any, *, length: int = 48) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:length] if _SAFE_TOKEN.match(text[:length]) else text[:length] or None


# ---------------------------------------------------------------------- engine
def ffprobe_path() -> Path:
    return get_settings().ffmpeg_bin / "ffprobe.exe"


class ProbeEngine:
    """Runs ffprobe and validates the result. No caching: callers store the document."""

    def __init__(self, *, timeout_s: float | None = None, executable: Path | None = None) -> None:
        settings = get_settings()
        self.timeout_s = timeout_s or settings.media_probe_timeout_s
        self.executable = executable or ffprobe_path()

    def available(self) -> bool:
        return self.executable.exists()

    def _command(self, path: Path) -> list[str]:
        return [
            str(self.executable),
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            "-show_error",
            # Bound the work: ffprobe analyses an entire file for some formats, which on a
            # damaged multi-hour file can run for minutes. 20 MB/s worth of packets is
            # plenty to establish the facts we need.
            "-analyzeduration", "20000000",
            "-probesize", "20000000",
            str(path),
        ]

    def probe(self, path: Path, *, size_bytes: int | None = None) -> ProbeResult:
        if not self.available():
            raise ProbeBinaryMissing(f"ffprobe not found at {self.executable}")
        if not path.exists():
            raise ProbeError("file does not exist")
        started = time.perf_counter()
        command = self._command(path)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=self.timeout_s,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise ProbeTimeout(f"ffprobe exceeded {self.timeout_s:.0f}s on {path.name}") from exc
        except OSError as exc:
            raise ProbeBinaryMissing(f"could not start ffprobe: {exc}") from exc

        stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()
        stdout = (completed.stdout or b"").decode("utf-8", "replace")
        if completed.returncode != 0:
            message = stderr.splitlines()[-1] if stderr else f"ffprobe exited {completed.returncode}"
            raise UnsupportedMedia(f"ffprobe could not read this file: {message[:300]}")

        try:
            payload = json.loads(stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ProbeError("ffprobe returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ProbeError("ffprobe returned an unexpected document")

        result = self.parse(payload, path=path, size_bytes=size_bytes)
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return result

    @staticmethod
    def _mime_type(
        *,
        format_name: str | None,
        tags: dict[str, str],
        kind: MediaKind,
        path: Path,
        video: VideoStream | None,
    ) -> str | None:
        """Best truthful MIME type.

        ffprobe reports the whole ``mov,mp4,m4a,3gp,3g2,mj2`` family for both MOV and MP4;
        the major brand (``isom``/``mp42`` vs ``qt  ``) is what actually distinguishes them,
        so the brand wins and the extension is a last resort rather than the other way round.
        """
        base_format = (format_name or "").split(",")[0]
        if kind == "image" and video is not None:
            by_codec = _MIME_BY_FORMAT.get(f"{video.codec}_pipe")
            if by_codec:
                return by_codec
        major_brand = (tags.get("major_brand") or "").strip().lower()
        if base_format == "mov":
            if major_brand.startswith("qt"):
                return "video/quicktime"
            if major_brand in {"isom", "iso2", "iso4", "iso5", "iso6", "mp41", "mp42", "avc1", "dash", "mmp4", "MSNV".lower()}:
                return "video/mp4"
            suffix = path.suffix.lower()
            if suffix == ".mov":
                return "video/quicktime"
            return "video/mp4"
        return _MIME_BY_FORMAT.get(base_format)

    def parse(self, payload: dict[str, Any], *, path: Path, size_bytes: int | None = None) -> ProbeResult:
        warnings: list[str] = []
        fmt = payload.get("format") or {}
        if not isinstance(fmt, dict):
            fmt = {}
        streams = payload.get("streams") or []
        if not isinstance(streams, list):
            streams = []

        video_raw = next(
            (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"), None
        )
        audio_raw = next(
            (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"), None
        )

        if not streams:
            errors = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            detail = str(errors.get("string") or "")[:200]
            raise UnsupportedMedia(
                f"no audio or video stream found{' (' + detail + ')' if detail else ''}"
            )

        format_name = _safe_text(fmt.get("format_name"), length=120)
        kind = classify(format_name=format_name, video=video_raw, audio=audio_raw)
        if kind == "unknown":
            raise UnsupportedMedia(
                f"unrecognised media: container {format_name or 'unknown'} with no usable streams"
            )

        duration_s = parse_duration_seconds(fmt.get("duration"))
        if duration_s is None and video_raw is not None:
            duration_s = parse_duration_seconds(video_raw.get("duration"))
        if duration_s is None and audio_raw is not None:
            duration_s = parse_duration_seconds(audio_raw.get("duration"))
        if duration_s is None:
            warnings.append("duration is missing from the file")

        bitrate = _int_in(fmt.get("bit_rate"), low=0, high=10**11)
        tags = _clean_tags(fmt.get("tags"))
        creation_time = tags.get("creation_time")

        video: VideoStream | None = None
        if video_raw is not None:
            width = _int_in(video_raw.get("width"), low=1, high=20000)
            height = _int_in(video_raw.get("height"), low=1, high=20000)
            if width is None or height is None:
                warnings.append("video dimensions are missing")
            rotation = parse_rotation(video_raw)
            display_width, display_height = width, height
            if width and height and rotation in (90, 270):
                display_width, display_height = height, width
            aspect = None
            if display_width and display_height:
                fraction = Fraction(display_width, display_height).limit_denominator(50)
                aspect = f"{fraction.numerator}:{fraction.denominator}"
            color_transfer = _safe_text(video_raw.get("color_transfer"))
            video = VideoStream(
                index=_int_in(video_raw.get("index"), low=0, high=999) or 0,
                codec=_safe_text(video_raw.get("codec_name")),
                width=width,
                height=height,
                display_width=display_width,
                display_height=display_height,
                pixel_format=_safe_text(video_raw.get("pix_fmt")),
                fps=parse_rate(video_raw.get("r_frame_rate")),
                avg_fps=parse_rate(video_raw.get("avg_frame_rate")),
                bitrate=_int_in(video_raw.get("bit_rate"), low=0, high=10**11),
                rotation=rotation,
                color_space=_safe_text(video_raw.get("color_space")),
                color_transfer=color_transfer,
                color_primaries=_safe_text(video_raw.get("color_primaries")),
                is_hdr=color_transfer in HDR_TRANSFERS,
                frame_count=_int_in(video_raw.get("nb_frames"), low=0, high=10**8),
                duration_ms=(
                    int(parse_duration_seconds(video_raw.get("duration")) * 1000)
                    if parse_duration_seconds(video_raw.get("duration")) is not None
                    else None
                ),
                aspect_ratio=aspect,
            )

        audio: AudioStream | None = None
        if audio_raw is not None:
            audio = AudioStream(
                index=_int_in(audio_raw.get("index"), low=0, high=999) or 0,
                codec=_safe_text(audio_raw.get("codec_name")),
                sample_rate=_int_in(audio_raw.get("sample_rate"), low=1000, high=768000),
                channels=_int_in(audio_raw.get("channels"), low=1, high=64),
                channel_layout=_safe_text(audio_raw.get("channel_layout")),
                bitrate=_int_in(audio_raw.get("bit_rate"), low=0, high=10**8),
                duration_ms=(
                    int(parse_duration_seconds(audio_raw.get("duration")) * 1000)
                    if parse_duration_seconds(audio_raw.get("duration")) is not None
                    else None
                ),
            )

        duration_ms = int(duration_s * 1000) if duration_s is not None else None
        if kind == "image":
            # Stills have no duration; ffprobe reports a nominal one frame ("0.04s") from the
            # demuxer, and carrying that into the library would look like a 40 ms video.
            duration_ms = None
        width = video.display_width if video else None
        height = video.display_height if video else None
        orientation = None
        if width and height:
            orientation = "portrait" if height > width else ("landscape" if width > height else "square")

        mime_type = self._mime_type(
            format_name=format_name, tags=tags, kind=kind, path=path, video=video
        )

        size = size_bytes
        if size is None:
            size = _int_in(fmt.get("size"), low=0, high=10**13) or 0

        return ProbeResult(
            path=str(path),
            size_bytes=size,
            kind=kind,
            format_name=format_name,
            format_long_name=_safe_text(fmt.get("format_long_name"), length=160),
            mime_type=mime_type,
            duration_ms=duration_ms,
            bitrate=bitrate,
            video=video,
            audio=audio,
            streams_total=len(streams),
            creation_time=creation_time,
            tags=tags,
            width=width,
            height=height,
            fps=video.avg_fps or video.fps if video else None,
            rotation=video.rotation if video else 0,
            orientation=orientation,
            has_audio=audio is not None,
            is_hdr=bool(video and video.is_hdr),
            warnings=warnings,
        )


__all__ = [
    "PROBE_VERSION",
    "AudioStream",
    "MediaKind",
    "ProbeBinaryMissing",
    "ProbeEngine",
    "ProbeError",
    "ProbeResult",
    "ProbeTimeout",
    "UnsupportedMedia",
    "VideoStream",
    "classify",
    "parse_duration_seconds",
    "parse_rate",
    "parse_rotation",
]
