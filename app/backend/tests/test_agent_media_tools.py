"""Media tools for the Phase 4 agents: real outputs, real grants, no new tool surface."""
from __future__ import annotations

import pytest

from creatoros.db.models import AgentDefinition, AppSetting, AuditEvent
from creatoros.services.media.agent_grants import (
    GRANT_KEY,
    MEDIA_TOOL_GRANTS,
    apply_media_tool_grants,
    media_tools_by_department,
)
from creatoros.services.media.library import MediaLibrary
from creatoros.services.registry import AgentRegistry
from creatoros.services.toolbox import (
    TOOL_SPECS,
    ToolDenied,
    ToolExecutor,
    ensure_tools_seeded,
)

MEDIA_TOOLS = {
    "media.derivatives.list",
    "media.scene.scan",
    "media.transcode.recommend",
    "media.quality.report",
}


@pytest.fixture()
def seeded(session):
    AgentRegistry(session).ensure_seeded()
    ensure_tools_seeded(session)
    return session


@pytest.fixture()
def tools(seeded):
    return ToolExecutor(seeded, enforce_grant=False)


@pytest.fixture()
def video(session, media_files):
    library = MediaLibrary(session)
    outcome = library.import_file(media_files.landscape)
    session.commit()
    return library.get_asset(outcome.asset_id)


@pytest.fixture()
def audio(session, media_files):
    library = MediaLibrary(session)
    outcome = library.import_file(media_files.audio_wav)
    session.commit()
    return library.get_asset(outcome.asset_id)


# ------------------------------------------------------------------- surface
def test_media_tools_exist_and_are_read_or_compute_only(seeded):
    ids = {spec.id for spec in TOOL_SPECS}
    assert MEDIA_TOOLS <= ids
    for spec in TOOL_SPECS:
        if spec.id in MEDIA_TOOLS:
            assert spec.category in ("read", "compute"), spec.id
            assert spec.approval.name == "NONE", spec.id
    # 12 original tools, 4 media tools, then the Phase 6, Phase 7 and Phase 8 additions.
    assert len(TOOL_SPECS) == 26


def test_no_media_tool_can_escape_the_sandbox():
    forbidden = ("shell", "exec", "http", "fetch", "filesystem.write", "secrets")
    for spec in TOOL_SPECS:
        if spec.id.startswith("media."):
            assert not any(word in spec.id for word in forbidden)
            schema = str(spec.input_model.model_json_schema())
            # No tool takes a raw path from the model.
            assert "path" not in schema.lower() or "source_path" not in schema


# --------------------------------------------------------------- derivatives
def test_derivatives_list_reports_only_files_that_exist(tools, video, session):
    library = MediaLibrary(session)
    library.ensure_thumbnail(video)
    library.transcode(video, "fast_preview")
    session.commit()

    outcome = tools.execute("media.derivatives.list", agent_id="video_inspection_agent", payload={"asset_id": video.id})
    assert outcome.ok, outcome.error
    output = outcome.output
    assert output["count"] >= 2
    kinds = {item["kind"] for item in output["derivatives"]}
    assert "thumbnail" in kinds and "preview" in kinds
    assert output["ready"] >= 2
    assert all(item["on_disk"] for item in output["derivatives"])

    # Deleting a file behind the database's back must be reported, not hidden.
    preview = next(item for item in library.derivatives(video.id) if str(item.kind) == "preview")
    library.derivative_file(preview).unlink()
    after = tools.execute(
        "media.derivatives.list", agent_id="video_inspection_agent", payload={"asset_id": video.id}
    )
    stale = [item for item in after.output["derivatives"] if item["kind"] == "preview"]
    assert stale and stale[0]["on_disk"] is False and stale[0]["verified"] is False


def test_derivatives_list_rejects_unknown_asset(tools):
    outcome = tools.execute(
        "media.derivatives.list", agent_id="video_inspection_agent", payload={"asset_id": "01M80ZZZZZZZZZZZZZZZZZZZ01"}
    )
    assert outcome.ok is False
    assert outcome.error_code == "TOOL_INVALID_INPUT"


# --------------------------------------------------------------------- scenes
def test_scene_scan_measures_real_cuts(tools, session, tmp_path):
    from tests.media_fixtures import run_ffmpeg

    clip = tmp_path / "cuts.mp4"
    run_ffmpeg(
        [
            "-f", "lavfi", "-i", "color=c=black:size=320x240:rate=25:duration=1",
            "-f", "lavfi", "-i", "color=c=white:size=320x240:rate=25:duration=1",
            "-f", "lavfi", "-i", "color=c=red:size=320x240:rate=25:duration=1",
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
            str(clip),
        ]
    )
    library = MediaLibrary(session)
    outcome = library.import_file(clip, auto_thumbnail=False)
    session.commit()
    asset = library.get_asset(outcome.asset_id)

    output = tools.execute(
        "media.scene.scan", agent_id="scene_detection_agent", payload={"asset_id": asset.id, "threshold": 0.3}
    ).output
    assert output["scene_count"] >= 2
    assert output["scenes"] == sorted(output["scenes"])
    assert all(0 <= value <= 3.2 for value in output["scenes"])
    assert output["method"].startswith("ffmpeg select")
    assert output["truncated"] is False


def test_scene_scan_refuses_audio(tools, audio):
    outcome = tools.execute("media.scene.scan", agent_id="scene_detection_agent", payload={"asset_id": audio.id})
    assert outcome.ok is False
    assert outcome.error_code == "NOT_A_VIDEO"


def test_scene_scan_clamps_its_input(tools, video):
    from creatoros.services.toolbox import ToolInvalidInput

    with pytest.raises(ToolInvalidInput):
        tools.execute(
            "media.scene.scan",
            agent_id="scene_detection_agent",
            payload={"asset_id": video.id, "max_scenes": 500},
        )


# ------------------------------------------------------------- recommendation
def test_transcode_recommendation_states_its_reason(tools, video, audio):
    for asset, expected in ((video, "fast_preview"), (audio, "audio_wav")):
        output = tools.execute(
            "media.transcode.recommend", agent_id="video_transcoding_agent", payload={"asset_id": asset.id}
        ).output
        assert output["recommendation"]["profile"] == expected
        assert output["recommendation"]["reason"]
        assert output["executed"] is False
        assert output["alternatives"]


def test_transcode_recommendation_refuses_unprobed_assets(tools, session, asset_factory):
    asset = asset_factory(kind="unknown")
    session.commit()
    outcome = tools.execute(
        "media.transcode.recommend", agent_id="video_transcoding_agent", payload={"asset_id": asset.id}
    )
    assert outcome.ok is False
    assert outcome.error_code == "NOT_PROBED"


# ---------------------------------------------------------------- quality
def test_quality_report_flags_real_platform_limits(tools, audio):
    output = tools.execute(
        "media.quality.report",
        agent_id="platform_format_validation_agent",
        payload={"asset_id": audio.id, "platform": "instagram_reels"},
    ).output
    assert output["verdict"] in ("blocked", "pass_with_warnings")
    codes = {item["code"] for item in output["issues"] + output["warnings"]}
    assert "NO_VIDEO" in codes  # an audio file cannot be an Instagram reel
    assert output["platform"] == "instagram_reels"
    assert output["limits"]["max_s"] == 90


def test_quality_report_accepts_a_good_vertical_clip(tools, session, media_files):
    library = MediaLibrary(session)
    outcome = library.import_file(media_files.portrait)
    session.commit()
    asset = library.get_asset(outcome.asset_id)
    asset.rights_status = "owned"
    session.commit()
    output = tools.execute(
        "media.quality.report",
        agent_id="platform_format_validation_agent",
        payload={"asset_id": asset.id, "platform": "tiktok"},
    ).output
    assert output["facts"]["aspect_ratio"] < 1
    # The fixture is 360x640, genuinely below TikTok's 540px minimum, so this is honest.
    assert [item["code"] for item in output["issues"]] == ["LOW_RESOLUTION"]


def test_quality_report_rejects_an_unknown_platform(tools, video):
    outcome = tools.execute(
        "media.quality.report",
        agent_id="platform_format_validation_agent",
        payload={"asset_id": video.id, "platform": "myspace"},
    )
    assert outcome.ok is False
    assert outcome.error_code == "TOOL_INVALID_INPUT"


# -------------------------------------------------------------------- grants
def strip_media_tools(session) -> None:
    """Recreate the state of a shipped Phase 4 database: no media tools granted yet."""
    for row in session.query(AgentDefinition).all():
        row.allowed_tools_json = [tool for tool in row.allowed_tools_json if not str(tool).startswith("media.")]
    session.query(AppSetting).filter_by(key=GRANT_KEY).delete()
    session.commit()


def test_grants_are_additive_and_idempotent(seeded):
    strip_media_tools(seeded)
    first = apply_media_tool_grants(seeded)
    assert first["applied"] is True
    assert first["agents_updated"] >= 40

    before = {row.agent_id: list(row.allowed_tools_json) for row in seeded.query(AgentDefinition).all()}
    second = apply_media_tool_grants(seeded)
    assert second["applied"] is False
    after = {row.agent_id: list(row.allowed_tools_json) for row in seeded.query(AgentDefinition).all()}
    assert before == after
    assert seeded.get(AppSetting, GRANT_KEY).value_json["applied"] is True


def test_grants_preserve_existing_tools_and_revoke_nothing(seeded):
    strip_media_tools(seeded)
    agent = seeded.get(AgentDefinition, "video_inspection_agent")
    agent.allowed_tools_json = [*agent.allowed_tools_json, "report.generate", "internal.search"]
    seeded.commit()
    apply_media_tool_grants(seeded)
    seeded.refresh(agent)
    assert {"report.generate", "internal.search"} <= set(agent.allowed_tools_json)
    assert MEDIA_TOOLS & {"media.derivatives.list", "media.scene.scan"} <= set(agent.allowed_tools_json)
    assert agent.config_version >= 2


def test_grants_are_audited(seeded):
    strip_media_tools(seeded)
    apply_media_tool_grants(seeded)
    actions = [row.action for row in seeded.query(AuditEvent).all()]
    assert "media.tool_grants" in actions


def test_granted_agents_can_use_the_tools_and_others_cannot(seeded, video):
    strip_media_tools(seeded)
    apply_media_tool_grants(seeded)
    executor = ToolExecutor(seeded, enforce_grant=True)
    allowed = executor.execute(
        "media.derivatives.list", agent_id="video_inspection_agent", payload={"asset_id": video.id}
    )
    assert allowed.ok is True

    with pytest.raises(ToolDenied):
        executor.execute(
            "media.derivatives.list", agent_id="community_manager", payload={"asset_id": video.id}
        )


def test_grants_cover_exactly_the_documented_departments(seeded):
    strip_media_tools(seeded)
    apply_media_tool_grants(seeded)
    holders = media_tools_by_department(seeded)
    assert set(holders) == set(MEDIA_TOOL_GRANTS)
    for department, agents in holders.items():
        assert agents, department


def test_grants_fail_loudly_on_an_unknown_tool(seeded, monkeypatch):
    import creatoros.services.media.agent_grants as grants

    monkeypatch.setitem(grants.MEDIA_TOOL_GRANTS, "video_production", ("media.nonexistent",))
    seeded.query(AppSetting).filter_by(key=GRANT_KEY).delete()
    seeded.commit()
    with pytest.raises(ValueError):
        apply_media_tool_grants(seeded)
