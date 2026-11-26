"""The publishing state machine is data, so it can be checked exhaustively.

Three invariants matter more than any single transition:

1. every canonical destination state has an entry in `PUBLISH_TRANSITIONS` (a missing entry means a
   service that writes that state can never leave it);
2. every transition target is itself a destination state (a typo cannot create a state nobody
   handles);
3. every destination can actually reach `published` from `draft` (otherwise the machine has a dead
   end that would look like a stuck job).
"""
from __future__ import annotations

from collections import deque

import pytest

from creatoros.db.enums import (
    DESTINATION_PUBLISH_STATES,
    HUMAN_RETRY_STATES,
    LEGACY_PUBLISH_STATES,
    NEEDS_HUMAN_PUBLISH_STATES,
    PUBLISH_TRANSITIONS,
    RETRYABLE_ERROR_CATEGORIES,
    TERMINAL_PUBLISH_STATES,
    PublishErrorCategory,
    PublishMode,
    PublishState,
    can_transition,
    normalize_publish_state,
    parse_publish_state,
)

CANONICAL = [
    PublishState.DRAFT,
    PublishState.PREPARING,
    PublishState.VALIDATION_PENDING,
    PublishState.AWAITING_APPROVAL,
    PublishState.APPROVED,
    PublishState.SCHEDULED,
    PublishState.READY,
    PublishState.UPLOADING,
    PublishState.UPLOADED,
    PublishState.PROCESSING,
    PublishState.PUBLISHED,
    PublishState.PARTIALLY_PUBLISHED,
    PublishState.FAILED,
    PublishState.CANCELLED,
    PublishState.BLOCKED,
    PublishState.REQUIRES_USER_ACTION,
    PublishState.UNKNOWN_REMOTE_STATE,
    PublishState.NEEDS_RECONCILIATION,
]


def test_the_canonical_vocabulary_is_exactly_the_eighteen_states():
    assert len(CANONICAL) == 18
    assert {state.value for state in CANONICAL} <= {member.value for member in PublishState}


def test_partial_publication_is_a_campaign_rollup_not_a_destination_state():
    assert PublishState.PARTIALLY_PUBLISHED not in DESTINATION_PUBLISH_STATES
    assert PublishState.PARTIALLY_PUBLISHED in CANONICAL


def test_every_destination_state_has_a_transition_entry():
    missing = DESTINATION_PUBLISH_STATES - set(PUBLISH_TRANSITIONS)
    assert missing == frozenset(), f"no transitions defined for {missing}"


def test_every_transition_target_is_a_destination_state():
    for source, targets in PUBLISH_TRANSITIONS.items():
        if source in {PublishState.PARTIALLY_PUBLISHED}:
            continue
        stray = {target for target in targets if target not in DESTINATION_PUBLISH_STATES}
        assert stray == set(), f"{source} points at non-destination states {stray}"


def test_terminal_states_are_really_terminal():
    assert TERMINAL_PUBLISH_STATES == {PublishState.PUBLISHED, PublishState.CANCELLED}
    for state in TERMINAL_PUBLISH_STATES:
        assert PUBLISH_TRANSITIONS[state] == frozenset(), f"{state} should not transition anywhere"
        for other in DESTINATION_PUBLISH_STATES - {state}:
            assert not can_transition(state, other)


def test_failed_is_retryable_by_a_human_but_never_by_a_sweeper():
    assert PublishState.FAILED in HUMAN_RETRY_STATES
    assert PublishState.FAILED not in TERMINAL_PUBLISH_STATES
    # The only way out of a failure is an explicit retry to `ready`, or a cancellation.
    assert PUBLISH_TRANSITIONS[PublishState.FAILED] == {
        PublishState.READY,
        PublishState.DRAFT,
        PublishState.CANCELLED,
    }


def test_published_is_reachable_from_draft():
    seen = {PublishState.DRAFT}
    queue = deque([PublishState.DRAFT])
    while queue:
        current = queue.popleft()
        for target in PUBLISH_TRANSITIONS[current]:
            if target not in seen:
                seen.add(target)
                queue.append(target)
    assert PublishState.PUBLISHED in seen
    # Everything except the campaign rollup is reachable; a destination state nothing can enter
    # would be a state no code path writes.
    assert set(CANONICAL) - seen == {PublishState.PARTIALLY_PUBLISHED}


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (PublishState.DRAFT, PublishState.VALIDATION_PENDING),
        (PublishState.VALIDATION_PENDING, PublishState.AWAITING_APPROVAL),
        (PublishState.AWAITING_APPROVAL, PublishState.APPROVED),
        (PublishState.APPROVED, PublishState.READY),
        (PublishState.APPROVED, PublishState.SCHEDULED),
        # Editing approved content revokes the approval and re-enters validation.
        (PublishState.APPROVED, PublishState.VALIDATION_PENDING),
        (PublishState.SCHEDULED, PublishState.VALIDATION_PENDING),
        (PublishState.SCHEDULED, PublishState.READY),
        (PublishState.READY, PublishState.UPLOADING),
        # A revoked or expired approval stops a queued destination.
        (PublishState.READY, PublishState.REQUIRES_USER_ACTION),
        (PublishState.UPLOADING, PublishState.UPLOADED),
        (PublishState.UPLOADED, PublishState.PROCESSING),
        (PublishState.PROCESSING, PublishState.PUBLISHED),
        (PublishState.UPLOADING, PublishState.UNKNOWN_REMOTE_STATE),
        # A token can be revoked while the bytes are already going up.
        (PublishState.UPLOADING, PublishState.REQUIRES_USER_ACTION),
        (PublishState.UNKNOWN_REMOTE_STATE, PublishState.NEEDS_RECONCILIATION),
        (PublishState.NEEDS_RECONCILIATION, PublishState.PUBLISHED),
        (PublishState.REQUIRES_USER_ACTION, PublishState.READY),
        (PublishState.BLOCKED, PublishState.DRAFT),
        (PublishState.FAILED, PublishState.READY),
        # A failed attempt consumed its approval; consent is per payload, so the destination can go
        # back to draft and be approved again.
        (PublishState.FAILED, PublishState.DRAFT),
    ],
)
def test_expected_transitions_are_legal(source, target):
    assert can_transition(source, target)
    # A state may always "transition" to itself; services use that to rewrite fields in place.
    assert can_transition(source, source)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (PublishState.DRAFT, PublishState.UPLOADING),
        (PublishState.DRAFT, PublishState.PUBLISHED),
        (PublishState.VALIDATION_PENDING, PublishState.READY),
        (PublishState.AWAITING_APPROVAL, PublishState.READY),
        (PublishState.PUBLISHED, PublishState.READY),
        (PublishState.CANCELLED, PublishState.READY),
        (PublishState.FAILED, PublishState.PUBLISHED),
        (PublishState.PROCESSING, PublishState.UPLOADING),
    ],
)
def test_illegal_transitions_are_refused(source, target):
    assert not can_transition(source, target)


def test_legacy_states_normalise_and_are_never_targets():
    assert normalize_publish_state("queued") is PublishState.READY
    assert normalize_publish_state("validation") is PublishState.VALIDATION_PENDING
    assert normalize_publish_state("needs_attention") is PublishState.REQUIRES_USER_ACTION
    assert normalize_publish_state("manual_publish_required") is PublishState.REQUIRES_USER_ACTION
    for legacy in LEGACY_PUBLISH_STATES:
        assert legacy not in {state.value for state in DESTINATION_PUBLISH_STATES}


def test_normalisation_is_tolerant_and_total():
    assert normalize_publish_state(None) is PublishState.DRAFT
    assert normalize_publish_state("READY") is PublishState.READY
    assert normalize_publish_state("  published  ") is PublishState.PUBLISHED
    assert normalize_publish_state("something-invented") is PublishState.DRAFT
    for member in PublishState:
        assert isinstance(normalize_publish_state(member), PublishState)


def test_states_that_need_a_human_cannot_be_left_automatically_into_uploading():
    for state in NEEDS_HUMAN_PUBLISH_STATES:
        assert not can_transition(state, PublishState.UPLOADING)


def test_only_transient_and_rate_limit_errors_are_retryable():
    assert RETRYABLE_ERROR_CATEGORIES == {
        PublishErrorCategory.TRANSIENT,
        PublishErrorCategory.RATE_LIMIT,
    }
    assert PublishErrorCategory.AMBIGUOUS not in RETRYABLE_ERROR_CATEGORIES
    assert PublishErrorCategory.PERMANENT not in RETRYABLE_ERROR_CATEGORIES


def test_publish_modes_keep_tiktoks_two_paths_apart():
    assert {mode.value for mode in PublishMode} == {"video", "reels", "direct_post", "inbox"}


def test_strict_parsing_refuses_what_normalisation_would_hide():
    # Stored legacy values are still readable through either path.
    assert parse_publish_state("queued") is PublishState.READY
    assert normalize_publish_state("queued") is PublishState.READY
    # A typo is where the two part company: a filter must not silently select drafts.
    assert normalize_publish_state("half-published") is PublishState.DRAFT
    with pytest.raises(ValueError):
        parse_publish_state("half-published")
