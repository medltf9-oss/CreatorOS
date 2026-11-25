"""Validators that turn "will the provider accept this?" into typed findings.

The rules come from :mod:`limits`; this module only compares. Three properties are deliberate:

* **Nothing is silent.** A field this platform cannot express is reported as ``unsupported`` rather
  than dropped, and a fact the probe could not establish is ``requires_attention`` rather than a
  pass. A validator that shrugs is how a job ends up failing at the provider with a 400 that says
  nothing.
* **Nothing leaks.** Findings carry numbers and rule codes. They never carry a filesystem path, a
  URL with a signature, or a token, because these documents are stored on the variant, copied into
  the audit trail and shown in the GUI.
* **The report is deterministic.** Same input, same findings in the same order, because the report
  feeds the approval payload hash. A report whose ordering depended on a dict would invalidate
  approvals at random.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from .....db.enums import PublishMode, ValidationOutcome
from .limits import PlatformRules, mode_for_platform, rules_for

Severity = Literal["warning", "failed", "unsupported", "requires_attention"]

#: Reported severities that block publication at preflight.
BLOCKING_SEVERITIES: frozenset[str] = frozenset({"failed"})

_HASHTAG = re.compile(r"^[A-Za-z0-9_]{1,100}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

#: ffprobe reports the MP4 family as one comma-joined string ("mov,mp4,m4a,3gp,3g2,mj2").
_CONTAINER_SPLIT = ","


@dataclass(frozen=True, slots=True)
class Finding:
    """One rule's verdict, addressed to one field."""

    code: str
    severity: Severity
    field: str
    message: str
    detail: Mapping[str, Any] = field(default_factory=dict)
    docs_url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "field": self.field,
            "message": self.message,
            "detail": dict(self.detail),
            "docs_url": self.docs_url,
        }


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """The verdict for one variant, plus every finding that produced it."""

    platform: str
    mode: str
    outcome: ValidationOutcome
    findings: tuple[Finding, ...] = ()

    @property
    def blocking(self) -> bool:
        """True when preflight must refuse to queue this variant."""
        return any(finding.severity in BLOCKING_SEVERITIES for finding in self.findings)

    @property
    def needs_attention(self) -> bool:
        return any(finding.severity == "requires_attention" for finding in self.findings)

    def of(self, code: str) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.code == code)

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "mode": self.mode,
            "outcome": self.outcome.value,
            "blocking": self.blocking,
            "findings": [finding.as_dict() for finding in self.findings],
        }


def _worst(findings: Sequence[Finding]) -> ValidationOutcome:
    """Collapse findings into one outcome. Blocking beats attention beats unsupported beats advice."""
    severities = {finding.severity for finding in findings}
    if "failed" in severities:
        return ValidationOutcome.FAILED
    if "requires_attention" in severities:
        return ValidationOutcome.REQUIRES_ATTENTION
    if "unsupported" in severities:
        return ValidationOutcome.UNSUPPORTED
    if "warning" in severities:
        return ValidationOutcome.WARNING
    return ValidationOutcome.PASSED


def _report(platform: str, mode: str, findings: list[Finding]) -> ValidationReport:
    return ValidationReport(
        platform=platform, mode=mode, outcome=_worst(findings), findings=tuple(findings)
    )


# --------------------------------------------------------------------------- text


def normalize_hashtags(raw: Sequence[str] | None) -> list[str]:
    """Strip decoration and casing-unify, preserving the author's order."""
    cleaned: list[str] = []
    for item in raw or ():
        token = str(item).strip().lstrip("#").strip()
        if token:
            cleaned.append(token)
    return cleaned


def validate_text(
    rules: PlatformRules,
    *,
    title: str | None = None,
    caption: str | None = None,
    hashtags: Sequence[str] | None = None,
) -> ValidationReport:
    findings: list[Finding] = []
    text = rules.text
    caption_value = (caption or "").strip()
    title_value = (title or "").strip()

    if _CONTROL.search(caption_value) or _CONTROL.search(title_value):
        findings.append(
            Finding(
                code="text.control_characters",
                severity="failed",
                field="caption",
                message="The caption or title contains control characters the API cannot carry.",
                docs_url=text.docs_url,
            )
        )

    if title_value and not text.supports_title:
        findings.append(
            Finding(
                code="text.title_not_supported",
                severity="unsupported",
                field="title",
                message=f"{rules.platform} has no title field; move the text into the caption.",
                docs_url=text.docs_url,
            )
        )
    if text.title_max is not None and len(title_value) > text.title_max:
        findings.append(
            Finding(
                code="text.title_too_long",
                severity="failed",
                field="title",
                message=f"Title is {len(title_value)} characters; the limit is {text.title_max}.",
                detail={"length": len(title_value), "max": text.title_max},
                docs_url=text.docs_url,
            )
        )

    if not caption_value and text.caption_required:
        findings.append(
            Finding(
                code="text.caption_required",
                severity="failed",
                field="caption",
                message=f"{rules.platform} requires a non-empty caption in this mode.",
                docs_url=text.docs_url,
            )
        )
    if text.caption_max is not None and len(caption_value) > text.caption_max:
        findings.append(
            Finding(
                code="text.caption_too_long",
                severity="failed",
                field="caption",
                message=f"Caption is {len(caption_value)} characters; the limit is {text.caption_max}.",
                detail={"length": len(caption_value), "max": text.caption_max},
                docs_url=text.docs_url,
            )
        )
    elif text.caption_max is not None and len(caption_value) > int(text.caption_max * 0.9):
        findings.append(
            Finding(
                code="text.caption_near_limit",
                severity="warning",
                field="caption",
                message=f"Caption is {len(caption_value)} of {text.caption_max} characters.",
                detail={"length": len(caption_value), "max": text.caption_max},
                docs_url=text.docs_url,
            )
        )

    tags = normalize_hashtags(hashtags)
    invalid = [tag for tag in tags if not _HASHTAG.match(tag)]
    if invalid:
        findings.append(
            Finding(
                code="text.hashtag_invalid",
                severity="failed",
                field="hashtags",
                message="Hashtags may only contain letters, digits and underscores.",
                detail={"invalid_count": len(invalid)},
                docs_url=text.docs_url,
            )
        )
    if text.hashtags_max is not None and len(tags) > text.hashtags_max:
        findings.append(
            Finding(
                code="text.too_many_hashtags",
                severity="failed",
                field="hashtags",
                message=f"{len(tags)} hashtags; the limit is {text.hashtags_max}.",
                detail={"count": len(tags), "max": text.hashtags_max},
                docs_url=text.docs_url,
            )
        )
    seen: set[str] = set()
    duplicates = [tag for tag in tags if tag.lower() in seen or seen.add(tag.lower())]  # type: ignore[func-returns-value]
    if duplicates:
        findings.append(
            Finding(
                code="text.hashtag_duplicate",
                severity="warning",
                field="hashtags",
                message="The same hashtag appears more than once.",
                detail={"duplicate_count": len(duplicates)},
                docs_url=text.docs_url,
            )
        )

    return _report(rules.platform, rules.mode.value, findings)


# --------------------------------------------------------------------------- media


def _containers(format_name: str | None) -> set[str]:
    if not format_name:
        return set()
    return {part.strip().lower() for part in format_name.split(_CONTAINER_SPLIT) if part.strip()}


def _frame(probe: Mapping[str, Any]) -> tuple[int | None, int | None]:
    video = probe.get("video") or {}
    width = video.get("display_width") or video.get("width")
    height = video.get("display_height") or video.get("height")
    rotation = int(video.get("rotation") or probe.get("rotation") or 0)
    if width and height and rotation % 180 == 90:
        # A phone shot is stored landscape with a rotation flag; what the viewer sees is portrait.
        return int(height), int(width)
    return (int(width) if width else None, int(height) if height else None)


def validate_media(
    rules: PlatformRules,
    *,
    probe: Mapping[str, Any] | None,
    size_bytes: int | None = None,
) -> ValidationReport:
    findings: list[Finding] = []
    media = rules.media
    docs = media.docs_url

    if not probe:
        findings.append(
            Finding(
                code="media.probe_missing",
                severity="requires_attention",
                field="media",
                message="The media has no probe record, so its format cannot be verified.",
                docs_url=docs,
            )
        )
        return _report(rules.platform, rules.mode.value, findings)

    if str(probe.get("kind") or "").lower() != "video":
        findings.append(
            Finding(
                code="media.not_a_video",
                severity="failed",
                field="media",
                message=f"This surface publishes video; the asset was probed as {probe.get('kind')!r}.",
                detail={"kind": probe.get("kind")},
                docs_url=docs,
            )
        )

    formats = _containers(probe.get("format_name"))
    accepted = sorted(media.containers)
    if not formats or not (formats & media.containers):
        findings.append(
            Finding(
                code="media.container_unsupported",
                severity="failed",
                field="media.format_name",
                message=f"Container {sorted(formats) or 'unknown'} is not one of {accepted}.",
                detail={"containers": sorted(formats), "accepted": accepted},
                docs_url=docs,
            )
        )

    video = probe.get("video") or {}
    if not video:
        findings.append(
            Finding(
                code="media.video_stream_missing",
                severity="failed",
                field="media.video",
                message="No video stream was found in the asset.",
                docs_url=docs,
            )
        )
        return _report(rules.platform, rules.mode.value, findings)

    codec = str(video.get("codec") or "").strip().lower()
    if codec not in media.video_codecs:
        findings.append(
            Finding(
                code="media.video_codec_unsupported",
                severity="failed",
                field="media.video.codec",
                message=f"Video codec {codec or 'unknown'} is not one of {sorted(media.video_codecs)}.",
                detail={"codec": codec, "accepted": sorted(media.video_codecs)},
                docs_url=docs,
            )
        )

    pixel_format = video.get("pixel_format")
    if pixel_format is None:
        findings.append(
            Finding(
                code="media.pixel_format_unknown",
                severity="requires_attention",
                field="media.video.pixel_format",
                message="The pixel format is unknown, so compatibility cannot be confirmed.",
                docs_url=docs,
            )
        )
    elif str(pixel_format).lower() not in media.pixel_formats:
        findings.append(
            Finding(
                code="media.pixel_format_unsupported",
                severity="failed",
                field="media.video.pixel_format",
                message=(
                    f"Pixel format {pixel_format} is not one of {sorted(media.pixel_formats)}; "
                    "re-encode with a normalised profile."
                ),
                detail={"pixel_format": pixel_format, "accepted": sorted(media.pixel_formats)},
                docs_url=docs,
            )
        )

    audio = probe.get("audio") or {}
    has_audio = bool(probe.get("has_audio"))
    if has_audio:
        audio_codec = str(audio.get("codec") or "").strip().lower()
        if audio_codec not in media.audio_codecs:
            findings.append(
                Finding(
                    code="media.audio_codec_unsupported",
                    severity="failed",
                    field="media.audio.codec",
                    message=f"Audio codec {audio_codec or 'unknown'} is not one of {sorted(media.audio_codecs)}.",
                    detail={"codec": audio_codec, "accepted": sorted(media.audio_codecs)},
                    docs_url=docs,
                )
            )
    elif media.audio_required:
        findings.append(
            Finding(
                code="media.audio_required",
                severity="failed",
                field="media.audio",
                message=f"{rules.platform} requires an audio track for this surface.",
                docs_url=docs,
            )
        )

    duration_ms = probe.get("duration_ms") or video.get("duration_ms")
    if not duration_ms:
        findings.append(
            Finding(
                code="media.duration_unknown",
                severity="requires_attention",
                field="media.duration_ms",
                message="The duration is unknown, so the platform's window cannot be checked.",
                docs_url=docs,
            )
        )
    else:
        duration_ms = int(duration_ms)
        if duration_ms < media.min_duration_ms:
            findings.append(
                Finding(
                    code="media.duration_too_short",
                    severity="failed",
                    field="media.duration_ms",
                    message=f"Duration is {duration_ms} ms; the minimum is {media.min_duration_ms} ms.",
                    detail={"duration_ms": duration_ms, "min": media.min_duration_ms},
                    docs_url=docs,
                )
            )
        elif duration_ms > media.max_duration_ms:
            findings.append(
                Finding(
                    code="media.duration_too_long",
                    severity="failed",
                    field="media.duration_ms",
                    message=f"Duration is {duration_ms} ms; the maximum is {media.max_duration_ms} ms.",
                    detail={"duration_ms": duration_ms, "max": media.max_duration_ms},
                    docs_url=docs,
                )
            )
        elif media.warn_over_duration_ms is not None and duration_ms > media.warn_over_duration_ms:
            findings.append(
                Finding(
                    code="media.duration_over_recommended",
                    severity="warning",
                    field="media.duration_ms",
                    message=(
                        f"Duration is {duration_ms} ms; this surface recommends at most "
                        f"{media.warn_over_duration_ms} ms."
                    ),
                    detail={"duration_ms": duration_ms, "recommended": media.warn_over_duration_ms},
                    docs_url=docs,
                )
            )

    width, height = _frame(probe)
    if not width or not height:
        findings.append(
            Finding(
                code="media.dimensions_unknown",
                severity="requires_attention",
                field="media.video.width",
                message="The frame size is unknown, so orientation cannot be checked.",
                docs_url=docs,
            )
        )
    else:
        short_side, long_side = sorted((width, height))
        if short_side < media.min_short_side:
            findings.append(
                Finding(
                    code="media.resolution_too_small",
                    severity="failed",
                    field="media.video.width",
                    message=f"Frame is {width}x{height}; the short side must be at least {media.min_short_side}.",
                    detail={"width": width, "height": height, "min_short_side": media.min_short_side},
                    docs_url=docs,
                )
            )
        if short_side > media.max_short_side or long_side > media.max_long_side:
            findings.append(
                Finding(
                    code="media.resolution_too_large",
                    severity="failed",
                    field="media.video.width",
                    message=(
                        f"Frame is {width}x{height}; the limits are {media.max_short_side} and "
                        f"{media.max_long_side} pixels."
                    ),
                    detail={
                        "width": width,
                        "height": height,
                        "max_short_side": media.max_short_side,
                        "max_long_side": media.max_long_side,
                    },
                    docs_url=docs,
                )
            )
        ratio = width / height
        if not (media.aspect_min <= ratio <= media.aspect_max):
            findings.append(
                Finding(
                    code="media.aspect_ratio_unsupported",
                    severity="failed",
                    field="media.video.width",
                    message=(
                        f"Aspect ratio {ratio:.3f} is outside the accepted range "
                        f"{media.aspect_min:.3f} to {media.aspect_max:.3f}."
                    ),
                    detail={"ratio": round(ratio, 4), "min": media.aspect_min, "max": media.aspect_max},
                    docs_url=docs,
                )
            )
        elif abs(ratio - media.preferred_aspect) > media.preferred_aspect_tolerance:
            findings.append(
                Finding(
                    code="media.aspect_ratio_not_preferred",
                    severity="warning",
                    field="media.video.width",
                    message=(
                        f"Aspect ratio {ratio:.3f} is accepted but not the preferred "
                        f"{media.preferred_aspect:.3f}; the platform may add bars or crop."
                    ),
                    detail={"ratio": round(ratio, 4), "preferred": round(media.preferred_aspect, 4)},
                    docs_url=docs,
                )
            )

    measured = size_bytes if size_bytes is not None else probe.get("size_bytes")
    if not measured:
        findings.append(
            Finding(
                code="media.size_unknown",
                severity="requires_attention",
                field="media.size_bytes",
                message="The file size is unknown, so the upload limit cannot be checked.",
                docs_url=docs,
            )
        )
    elif int(measured) > media.max_bytes:
        findings.append(
            Finding(
                code="media.file_too_large",
                severity="failed",
                field="media.size_bytes",
                message=f"File is {int(measured)} bytes; the limit is {media.max_bytes} bytes.",
                detail={"size_bytes": int(measured), "max": media.max_bytes},
                docs_url=docs,
            )
        )

    return _report(rules.platform, rules.mode.value, findings)


# --------------------------------------------------------------------------- options


def validate_options(
    rules: PlatformRules,
    *,
    options: Mapping[str, Any] | None = None,
    creator_info: Mapping[str, Any] | None = None,
) -> ValidationReport:
    """Platform-specific switches, including the ones TikTok only reveals per creator."""
    findings: list[Finding] = []
    chosen = dict(options or {})
    docs = rules.docs_url

    unknown = sorted(set(chosen) - set(rules.known_options))
    for key in unknown:
        findings.append(
            Finding(
                code="options.unknown_option",
                severity="unsupported",
                field=f"options.{key}",
                message=f"{rules.platform} has no option named {key!r}; it would be ignored.",
                detail={"option": key},
                docs_url=docs,
            )
        )

    if rules.platform == "tiktok":
        findings.extend(_tiktok_options(rules, chosen, creator_info, docs))

    if rules.mode is PublishMode.INBOX and "privacy_level" in chosen:
        findings.append(
            Finding(
                code="options.privacy_ignored_in_inbox",
                severity="warning",
                field="options.privacy_level",
                message="Inbox uploads are finished in the TikTok app, so privacy is chosen there.",
                docs_url=docs,
            )
        )

    return _report(rules.platform, rules.mode.value, findings)


def _tiktok_options(
    rules: PlatformRules,
    chosen: Mapping[str, Any],
    creator_info: Mapping[str, Any] | None,
    docs: str,
) -> list[Finding]:
    findings: list[Finding] = []
    direct = rules.mode is PublishMode.DIRECT_POST
    privacy = chosen.get("privacy_level")

    if direct and not privacy:
        findings.append(
            Finding(
                code="options.privacy_level_required",
                severity="failed",
                field="options.privacy_level",
                message="Direct Post requires an explicit privacy level; TikTok never picks one for you.",
                docs_url=docs,
            )
        )

    if privacy and creator_info is not None:
        allowed = list(creator_info.get("privacy_level_options") or [])
        if allowed and privacy not in allowed:
            findings.append(
                Finding(
                    code="options.privacy_level_not_allowed",
                    severity="failed",
                    field="options.privacy_level",
                    message=f"This creator cannot publish with {privacy!r}.",
                    detail={"requested": privacy, "allowed": allowed},
                    docs_url=docs,
                )
            )
    elif direct and privacy and creator_info is None:
        findings.append(
            Finding(
                code="options.creator_info_missing",
                severity="requires_attention",
                field="options.privacy_level",
                message=(
                    "TikTok's allowed privacy levels come from the creator's account information, "
                    "which has not been fetched yet."
                ),
                docs_url=docs,
            )
        )

    if creator_info is not None:
        for option, flag, wording in (
            ("disable_comment", "comment_disabled", "comments"),
            ("disable_duet", "duet_disabled", "Duet"),
            ("disable_stitch", "stitch_disabled", "Stitch"),
        ):
            if chosen.get(option) is True and creator_info.get(flag) is True:
                findings.append(
                    Finding(
                        code="options.interaction_already_disabled",
                        severity="unsupported",
                        field=f"options.{option}",
                        message=f"This creator has {wording} disabled already; the switch has no effect.",
                        detail={"option": option},
                        docs_url=docs,
                    )
                )

    duration_limit = creator_info.get("max_video_post_duration_sec") if creator_info else None
    if duration_limit:
        findings.append(
            Finding(
                code="options.duration_limit_from_creator",
                severity="warning",
                field="media.duration_ms",
                message=f"TikTok allows this creator at most {duration_limit} seconds per post.",
                detail={"max_seconds": duration_limit},
                docs_url=docs,
            )
        )
    return findings


# --------------------------------------------------------------------------- entry point


def validate_variant(
    platform: str,
    mode: PublishMode | str | None = None,
    *,
    title: str | None = None,
    caption: str | None = None,
    hashtags: Sequence[str] | None = None,
    options: Mapping[str, Any] | None = None,
    probe: Mapping[str, Any] | None = None,
    size_bytes: int | None = None,
    creator_info: Mapping[str, Any] | None = None,
) -> ValidationReport:
    """Validate everything the composer knows about one variant, in a stable order."""
    resolved_mode = mode or mode_for_platform(platform)
    rules = rules_for(platform, resolved_mode)
    findings: list[Finding] = []
    findings.extend(validate_text(rules, title=title, caption=caption, hashtags=hashtags).findings)
    findings.extend(validate_media(rules, probe=probe, size_bytes=size_bytes).findings)
    findings.extend(
        validate_options(rules, options=options, creator_info=creator_info).findings
    )
    return _report(rules.platform, rules.mode.value, findings)
