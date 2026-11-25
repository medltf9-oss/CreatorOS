"""Tests for the per-platform publishing rules.

The validators exist to catch, locally, the rejections a provider would otherwise deliver minutes
into an upload. So the tests are written the way a user hits the wall: a portrait Reel that is
actually landscape, a file the phone recorded in HEVC, a caption one character too long.
"""
from __future__ import annotations

import json

import pytest

from creatoros.db.enums import PublishMode, ValidationOutcome
from creatoros.integrations.social.publishing import (
    PLATFORM_RULES,
    ValidationReport,
    rules_for,
    validate_media,
    validate_options,
    validate_text,
    validate_variant,
)
from creatoros.integrations.social.publishing.metadata.limits import mode_for_platform

GIB = 1024**3


def probe(**overrides):
    """A probe document shaped exactly like `ProbeResult.as_dict()`."""
    document = {
        "probe_version": "p5.1",
        "path": "<DATA_ROOT>\\data\\media\\ab\\cd\\0123456789abcdef.mp4",
        "size_bytes": 7_000_000,
        "kind": "video",
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "format_long_name": "QuickTime / MOV",
        "mime_type": "video/mp4",
        "duration_ms": 42_000,
        "bitrate": 1_400_000,
        "has_audio": True,
        "rotation": 0,
        "video": {
            "codec": "h264",
            "width": 1080,
            "height": 1920,
            "display_width": 1080,
            "display_height": 1920,
            "pixel_format": "yuv420p",
            "fps": 30.0,
            "rotation": 0,
            "duration_ms": 42_000,
        },
        "audio": {"codec": "aac", "sample_rate": 44_100, "channels": 2},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(document.get(key), dict):
            document[key] = {**document[key], **value}
        else:
            document[key] = value
    return document


def codes(report: ValidationReport) -> set[str]:
    return {finding.code for finding in report.findings}


def test_a_clean_variant_passes_on_every_surface():
    for platform in ("facebook", "instagram", "tiktok"):
        options = {"privacy_level": "SELF_ONLY"} if platform == "tiktok" else {}
        # Direct Post cannot be confirmed without the creator's own privacy options, so a real
        # preflight always fetches them first.
        creator = (
            {"privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"], "max_video_post_duration_sec": 600}
            if platform == "tiktok"
            else None
        )
        report = validate_variant(
            platform,
            caption="Morning routine, 40 seconds, no talking.",
            hashtags=["morning", "routine"],
            options=options,
            probe=probe(),
            creator_info=creator,
        )
        assert report.outcome in {ValidationOutcome.PASSED, ValidationOutcome.WARNING}, report.as_dict()
        assert not report.blocking


def test_the_default_mode_per_platform_is_the_one_that_exists():
    assert mode_for_platform("facebook") is PublishMode.VIDEO
    assert mode_for_platform("instagram") is PublishMode.REELS
    assert mode_for_platform("tiktok") is PublishMode.DIRECT_POST
    with pytest.raises(KeyError):
        mode_for_platform("linkedin")


def test_every_rule_carries_a_documentation_url():
    for (platform, mode), rules in PLATFORM_RULES.items():
        assert rules.platform == platform and rules.mode is mode
        assert rules.docs_url.startswith("https://")
        assert rules.text.docs_url.startswith("https://")
        assert rules.media.docs_url.startswith("https://")
        assert rules.known_options == frozenset(rules.known_options)


# --------------------------------------------------------------------------- media


def test_a_matroska_file_is_refused_before_it_is_uploaded():
    report = validate_media(rules_for("instagram", "reels"), probe=probe(format_name="matroska,webm"))
    assert "media.container_unsupported" in codes(report)
    assert report.blocking


def test_ffprobes_comma_joined_mp4_family_is_understood():
    rules = rules_for("facebook", "video")
    report = validate_media(rules, probe=probe(format_name="mov,mp4,m4a,3gp,3g2,mj2"))
    assert "media.container_unsupported" not in codes(report)


def test_hevc_video_is_refused_with_the_accepted_list_in_the_detail():
    report = validate_media(rules_for("facebook", "video"), probe=probe(video={"codec": "hevc"}))
    finding = report.of("media.video_codec_unsupported")[0]
    assert report.blocking
    assert "h264" in finding.detail["accepted"]
    assert finding.detail["codec"] == "hevc"


def test_opus_audio_is_refused_but_silence_is_fine_on_facebook():
    report = validate_media(
        rules_for("facebook", "video"), probe=probe(audio={"codec": "opus"})
    )
    assert "media.audio_codec_unsupported" in codes(report)
    silent = validate_media(rules_for("facebook", "video"), probe=probe(has_audio=False, audio=None))
    assert "media.audio_codec_unsupported" not in codes(silent)


def test_instagram_requires_an_audio_track():
    report = validate_media(
        rules_for("instagram", "reels"), probe=probe(has_audio=False, audio=None)
    )
    assert "media.audio_required" in codes(report)
    assert report.blocking


def test_a_pixel_format_meta_rejects_is_caught_locally():
    report = validate_media(
        rules_for("instagram", "reels"), probe=probe(video={"pixel_format": "yuv444p"})
    )
    assert "media.pixel_format_unsupported" in codes(report)
    assert report.blocking


def test_an_unknown_pixel_format_is_attention_not_a_silent_pass():
    report = validate_media(
        rules_for("instagram", "reels"), probe=probe(video={"pixel_format": None})
    )
    assert "media.pixel_format_unknown" in codes(report)
    assert report.outcome is ValidationOutcome.REQUIRES_ATTENTION
    assert not report.blocking


def test_a_two_second_clip_is_too_short_for_reels():
    report = validate_media(rules_for("instagram", "reels"), probe=probe(duration_ms=2_000))
    assert "media.duration_too_short" in codes(report)


def test_a_twenty_minute_video_is_too_long_for_reels_but_fine_for_a_page():
    long_probe = probe(duration_ms=20 * 60 * 1000)
    assert "media.duration_too_long" in codes(validate_media(rules_for("instagram", "reels"), probe=long_probe))
    assert "media.duration_too_long" not in codes(
        validate_media(rules_for("facebook", "video"), probe=long_probe)
    )


def test_the_reels_ninety_second_guidance_is_a_warning_not_a_refusal():
    report = validate_media(rules_for("instagram", "reels"), probe=probe(duration_ms=120_000))
    assert "media.duration_over_recommended" in codes(report)
    assert not report.blocking


def test_an_upscaled_reel_is_refused_for_being_too_small():
    report = validate_media(
        rules_for("instagram", "reels"),
        probe=probe(video={"width": 480, "height": 854, "display_width": 480, "display_height": 854}),
    )
    assert "media.resolution_too_small" in codes(report)


def test_a_four_k_upright_recording_is_refused_for_facebook():
    report = validate_media(
        rules_for("facebook", "video"),
        probe=probe(video={"width": 2160, "height": 3840, "display_width": 2160, "display_height": 3840}),
    )
    assert "media.resolution_too_large" in codes(report)


def test_landscape_into_reels_is_refused_but_landscape_into_pages_is_not():
    landscape = probe(
        video={"width": 1920, "height": 1080, "display_width": 1920, "display_height": 1080}
    )
    reels = validate_media(rules_for("instagram", "reels"), probe=landscape)
    assert "media.aspect_ratio_unsupported" in codes(reels)
    page = validate_media(rules_for("facebook", "video"), probe=landscape)
    assert "media.aspect_ratio_unsupported" not in codes(page)


def test_a_rotated_phone_clip_is_measured_as_the_viewer_sees_it():
    portrait_flagged = probe(
        rotation=90,
        video={"width": 1920, "height": 1080, "display_width": None, "display_height": None, "rotation": 90},
    )
    report = validate_media(rules_for("instagram", "reels"), probe=portrait_flagged)
    assert "media.aspect_ratio_unsupported" not in codes(report)


def test_a_square_video_is_accepted_by_facebook_with_a_warning():
    square = probe(video={"width": 1080, "height": 1080, "display_width": 1080, "display_height": 1080})
    report = validate_media(rules_for("facebook", "video"), probe=square)
    assert "media.aspect_ratio_not_preferred" in codes(report)
    assert report.outcome is ValidationOutcome.WARNING


def test_a_file_over_the_instagram_cap_is_refused_with_the_exact_numbers():
    report = validate_media(
        rules_for("instagram", "reels"), probe=probe(size_bytes=GIB + 1), size_bytes=GIB + 1
    )
    finding = report.of("media.file_too_large")[0]
    assert finding.detail["max"] == GIB
    assert finding.detail["size_bytes"] == GIB + 1


def test_a_missing_probe_asks_for_attention_instead_of_guessing():
    report = validate_media(rules_for("facebook", "video"), probe=None)
    assert codes(report) == {"media.probe_missing"}
    assert report.outcome is ValidationOutcome.REQUIRES_ATTENTION


def test_an_audio_asset_is_not_publishable_as_video():
    report = validate_media(
        rules_for("facebook", "video"),
        probe={"kind": "audio", "format_name": "mp3", "video": None, "audio": {"codec": "mp3"}},
    )
    assert "media.not_a_video" in codes(report)
    assert "media.video_stream_missing" in codes(report)
    assert report.blocking


# --------------------------------------------------------------------------- text


def test_instagram_caption_over_the_limit_is_refused():
    report = validate_text(rules_for("instagram", "reels"), caption="x" * 2_201)
    finding = report.of("text.caption_too_long")[0]
    assert finding.detail == {"length": 2_201, "max": 2_200}
    assert report.blocking


def test_a_caption_close_to_the_limit_is_a_warning():
    report = validate_text(rules_for("instagram", "reels"), caption="x" * 2_000)
    assert "text.caption_near_limit" in codes(report)
    assert not report.blocking


def test_instagram_has_no_title_and_says_so_instead_of_dropping_it():
    report = validate_text(rules_for("instagram", "reels"), title="Morning routine", caption="hi")
    assert "text.title_not_supported" in codes(report)
    assert report.outcome is ValidationOutcome.UNSUPPORTED
    assert not report.blocking


def test_tiktok_needs_a_caption():
    report = validate_text(rules_for("tiktok", "direct_post"), caption="   ")
    assert "text.caption_required" in codes(report)
    assert report.blocking


def test_instagram_refuses_more_than_thirty_hashtags():
    report = validate_text(
        rules_for("instagram", "reels"), caption="hi", hashtags=[f"tag{i}" for i in range(31)]
    )
    assert "text.too_many_hashtags" in codes(report)


def test_hashtags_are_normalised_before_they_are_counted():
    tags = ["#Morning", "morning", "  #routine  "]
    report = validate_text(rules_for("instagram", "reels"), caption="hi", hashtags=tags)
    assert "text.hashtag_duplicate" in codes(report)
    assert "text.hashtag_invalid" not in codes(report)


def test_a_hashtag_with_punctuation_is_refused():
    report = validate_text(rules_for("facebook", "video"), caption="hi", hashtags=["not a tag!"])
    assert "text.hashtag_invalid" in codes(report)


def test_control_characters_are_refused_before_they_reach_json():
    report = validate_text(rules_for("facebook", "video"), caption="hello\x00world")
    assert "text.control_characters" in codes(report)


# --------------------------------------------------------------------------- options


def test_an_unknown_option_is_reported_rather_than_ignored():
    report = validate_options(rules_for("facebook", "video"), options={"teleport": True})
    assert "options.unknown_option" in codes(report)
    assert report.outcome is ValidationOutcome.UNSUPPORTED


def test_tiktok_direct_post_requires_an_explicit_privacy_level():
    report = validate_options(rules_for("tiktok", "direct_post"), options={})
    assert "options.privacy_level_required" in codes(report)
    assert report.blocking


def test_tiktok_refuses_a_privacy_level_the_creator_does_not_have():
    report = validate_options(
        rules_for("tiktok", "direct_post"),
        options={"privacy_level": "PUBLIC_TO_EVERYONE"},
        creator_info={"privacy_level_options": ["SELF_ONLY"]},
    )
    finding = report.of("options.privacy_level_not_allowed")[0]
    assert finding.detail["allowed"] == ["SELF_ONLY"]


def test_tiktok_without_creator_info_asks_for_attention():
    report = validate_options(
        rules_for("tiktok", "direct_post"), options={"privacy_level": "SELF_ONLY"}
    )
    assert "options.creator_info_missing" in codes(report)
    assert not report.blocking


def test_tiktok_reports_a_switch_that_cannot_do_anything():
    report = validate_options(
        rules_for("tiktok", "direct_post"),
        options={"privacy_level": "SELF_ONLY", "disable_comment": True},
        creator_info={"privacy_level_options": ["SELF_ONLY"], "comment_disabled": True},
    )
    assert "options.interaction_already_disabled" in codes(report)


def test_tiktok_reports_the_creators_own_duration_ceiling():
    report = validate_options(
        rules_for("tiktok", "direct_post"),
        options={"privacy_level": "SELF_ONLY"},
        creator_info={"privacy_level_options": ["SELF_ONLY"], "max_video_post_duration_sec": 60},
    )
    assert "options.duration_limit_from_creator" in codes(report)


def test_inbox_ignores_privacy_and_says_so():
    report = validate_options(rules_for("tiktok", "inbox"), options={"privacy_level": "SELF_ONLY"})
    assert "options.privacy_ignored_in_inbox" in codes(report)


# --------------------------------------------------------------------------- reporting


def test_a_failure_dominates_attention_and_unsupported():
    report = validate_text(rules_for("instagram", "reels"), title="t", caption="x" * 2_500)
    assert report.outcome is ValidationOutcome.FAILED
    assert report.blocking


def test_the_report_is_deterministic():
    first = validate_variant(
        "instagram",
        caption="x" * 2_300,
        title="a title",
        hashtags=["bad tag!", "bad tag!"],
        options={"nope": 1},
        probe=probe(video={"codec": "hevc"}),
    )
    second = validate_variant(
        "instagram",
        caption="x" * 2_300,
        title="a title",
        hashtags=["bad tag!", "bad tag!"],
        options={"nope": 1},
        probe=probe(video={"codec": "hevc"}),
    )
    assert first.as_dict() == second.as_dict()
    assert len(first.findings) > 3


def test_no_finding_ever_carries_a_local_path_or_a_url_with_a_signature():
    report = validate_variant(
        "instagram",
        caption="x" * 2_500,
        probe=probe(video={"codec": "hevc"}),
    )
    serialized = json.dumps(report.as_dict())
    assert "CreatorOS" not in serialized
    assert "data\\\\media" not in serialized
    assert "X-Amz-Signature" not in serialized
    assert "D:\\\\" not in serialized


def test_report_can_be_serialised_for_storage_on_the_variant():
    report = validate_variant("facebook", caption="hello", probe=probe())
    document = report.as_dict()
    assert document["platform"] == "facebook"
    assert document["mode"] == "video"
    assert document["outcome"] in {outcome.value for outcome in ValidationOutcome}
    assert isinstance(document["findings"], list)
    assert json.loads(json.dumps(document)) == document
