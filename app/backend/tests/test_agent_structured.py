"""Structured output parsing: tolerant of noise, intolerant of nonsense."""
from __future__ import annotations

import pytest

from creatoros.schemas.agents import AgentArtifact
from creatoros.services.structured import (
    MalformedOutput,
    coerce_artifact,
    extract_json,
    parse_artifact,
    repair_instruction,
)


def test_extracts_a_fenced_object():
    text = 'Here you go:\n```json\n{"summary": "ok"}\n```\nthanks'
    assert extract_json(text) == {"summary": "ok"}


def test_extracts_an_object_from_prose():
    text = 'Sure. {"summary": "ok", "nested": {"a": "}"}} trailing words'
    assert extract_json(text)["summary"] == "ok"


def test_handles_braces_inside_strings():
    text = '{"summary": "use {curly} braces", "note": "\\"quoted\\""}'
    assert extract_json(text)["summary"] == "use {curly} braces"


def test_rejects_empty_and_non_object():
    with pytest.raises(MalformedOutput):
        extract_json("")
    with pytest.raises(MalformedOutput):
        extract_json("[1, 2, 3]")


def test_noise_is_repaired_but_missing_findings_are_not_invented():
    artifact = parse_artifact('{"summary": "s", "findings": ["a claim", {"claim": "b claim"}]}', "analysis")
    assert [finding.statement for finding in artifact.findings] == ["a claim", "b claim"]
    with pytest.raises(MalformedOutput):
        parse_artifact('{"summary": "s"}', "analysis")


def test_copy_strings_become_items_and_contract_is_forced():
    artifact = parse_artifact('{"summary": "two drafts", "items": ["first", "second"]}', "copy")
    assert artifact.contract == "copy"
    assert [item.text for item in artifact.items] == ["first", "second"]


def test_review_without_verdict_is_inferred_from_severity():
    artifact = parse_artifact(
        '{"summary": "s", "issues": [{"severity": "critical", "issue": "wrong claim"}]}', "review"
    )
    assert artifact.verdict == "reject"
    mild = parse_artifact('{"summary": "s", "verdict": "pass", "issues": [{"issue": "nit"}]}', "review")
    assert mild.verdict == "pass"


def test_confidence_percentages_are_normalised():
    artifact = parse_artifact('{"summary": "s", "steps": ["a concrete step"]}', "plan")
    assert 0.0 <= artifact.confidence <= 1.0
    confident = parse_artifact('{"summary": "s", "steps": ["a concrete step"], "confidence": 88}', "plan")
    assert confident.confidence == pytest.approx(0.88)


def test_plan_without_steps_is_rejected():
    with pytest.raises(MalformedOutput):
        parse_artifact('{"summary": "do things"}', "plan")


def test_report_section_from_string():
    artifact = parse_artifact('{"summary": "s", "sections": ["body text"]}', "report")
    assert artifact.sections[0].body == "body text"


def test_coerce_is_idempotent_on_clean_input():
    clean = {
        "contract": "analysis",
        "summary": "why",
        "findings": [{"statement": "because", "kind": "fact", "confidence": 0.9}],
        "confidence": 0.8,
    }
    artifact = coerce_artifact(clean, "analysis")
    again = coerce_artifact(artifact.model_dump(), "analysis")
    assert again.findings[0].statement == "because"
    assert again.findings[0].kind == "fact"


def test_artifact_validation_rejects_wrong_types():
    with pytest.raises(MalformedOutput):
        coerce_artifact({"summary": "s", "findings": [{"statement": 5}]}, "analysis")


def test_repair_instruction_names_the_contract_and_the_problem():
    message = repair_instruction("expected JSON object", "plan")
    assert "plan" in message
    assert "expected JSON object" in message
    assert "JSON" in message


def test_artifact_dump_round_trips():
    artifact = AgentArtifact(contract="plan", summary="s", steps=[{"step": "do it"}])
    assert AgentArtifact.model_validate(artifact.model_dump()).steps[0].step == "do it"


# --------------------------------------------------------------------------- measured local-model shapes
# These three come from a real qwen3:4b run on this machine (2026-11-24). Before the fixes they made the
# chief orchestrator fail with MALFORMED_AGENT_OUTPUT three attempts in a row, so no objective could run.

REAL_PLAN_OUTPUT = """{
  "summary": "Delegated objective to creative_writing and strategic_intelligence.",
  "steps": [
    {"step": "1", "dependencies": []},
    {"step": "2", "dependencies": ["strategic_intelligence"]}
  ],
  "assignments": {
    "strategic_intelligence": "Return audience and positioning insights.",
    "creative_writing": "Generate three caption options with one hashtag set."
  },
  "confidence": "High",
  "warnings": "No video production step is needed for captions."
}"""


def test_a_real_local_plan_parses_instead_of_failing_the_objective():
    artifact = parse_artifact(REAL_PLAN_OUTPUT, "plan")
    assert [step.step for step in artifact.steps] == ["1", "2"]
    # `dependencies` is the schema's `depends_on` under another name; the second step keeps its edge.
    assert artifact.steps[1].depends_on == ["strategic_intelligence"]
    # A mapping of department -> reason carries the department ids in its keys.
    assert artifact.assignments == ["strategic_intelligence", "creative_writing"]
    assert artifact.warnings == ["No video production step is needed for captions."]


def test_unknown_step_keys_are_dropped_rather_than_rejecting_the_plan():
    text = '{"summary": "s", "steps": [{"step": "do it", "why": "because", "eta": "1h"}]}'
    artifact = parse_artifact(text, "plan")
    assert artifact.steps[0].step == "do it"


def test_assignments_accept_every_shape_a_model_uses():
    from creatoros.services.structured import coerce_artifact

    mapping = coerce_artifact({"summary": "s", "steps": ["go"], "assignments": {"a": "x", "b": "y"}}, "plan")
    assert mapping.assignments == ["a", "b"]

    listing = coerce_artifact({"summary": "s", "steps": ["go"], "assignments": ["a", "b"]}, "plan")
    assert listing.assignments == ["a", "b"]

    objects = coerce_artifact(
        {"summary": "s", "steps": ["go"], "assignments": [{"department": "a", "why": "x"}, {"id": "b"}]},
        "plan",
    )
    assert objects.assignments == ["a", "b"]


def test_a_step_with_no_text_uses_the_placeholder_and_invents_nothing():
    # Pre-existing contract behaviour: a missing field becomes the documented placeholder rather than a
    # made-up instruction. The point of this test is that tolerance never manufactures content.
    artifact = parse_artifact('{"summary": "s", "steps": [{"owner": "nobody"}]}', "plan")
    assert artifact.steps[0].step == "unspecified step"


def test_an_empty_plan_is_still_an_error():
    with pytest.raises(MalformedOutput):
        parse_artifact('{"summary": "s", "steps": []}', "plan")


REAL_COPY_OUTPUT = """{ "summary": "Craft three concise, original morning-routine captions.",
  "items": [
    { "caption": "Wake up, breathe, thrive.", "hashtag": "#MorningMotivation" },
    { "caption": "Fresh start, fresh energy.", "hashtag": "#HealthyStart", "confidence": 95 },
    { "caption": "Simple habits, big results.", "hashtag": "#RoutineRise" }
  ], "confidence": 95 }"""


def test_a_real_local_copy_output_keeps_its_hashtags():
    """The hashtag the caller asked for must survive the contract, not be dropped with the key."""
    artifact = parse_artifact(REAL_COPY_OUTPUT, "copy")
    assert [item.text for item in artifact.items] == [
        "Wake up, breathe, thrive.",
        "Fresh start, fresh energy.",
        "Simple habits, big results.",
    ]
    assert artifact.items[0].notes == "hashtag: #MorningMotivation"
    assert artifact.items[1].notes == "hashtag: #HealthyStart"


def test_structural_noise_on_an_item_is_dropped_not_quoted():
    artifact = parse_artifact('{"summary": "s", "items": [{"text": "go", "confidence": 95, "score": 3}]}', "copy")
    assert artifact.items[0].notes is None


def test_an_item_with_no_text_becomes_the_placeholder_and_adds_nothing():
    # `text` is required by the schema. A dict with no recognisable text yields the documented
    # placeholder and no invented content - an extra key does not become a caption.
    artifact = parse_artifact('{"summary": "s", "items": [{"hashtag": "#x"}]}', "copy")
    assert artifact.items[0].text == "empty"
    assert artifact.items[0].notes is None
