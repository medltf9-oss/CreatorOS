"""Approval tests: hash binding, single use, expiry, wrong-state refusal."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from creatoros.db.models import ApprovalRequest
from creatoros.services.approvals import (
    ApprovalExpired,
    ApprovalHashMismatch,
    ApprovalNotFound,
    ApprovalService,
    ApprovalStateError,
    payload_hash,
)


@pytest.fixture()
def approvals(session):
    return ApprovalService(session)


def _create(service, **overrides):
    payload = {"caption": "hello world", "platform": "instagram"}
    kwargs = {
        "action": "publish.post",
        "target_type": "platform_variant",
        "target_id": "01AAAAAAAAAAAAAAAAAAAAAAAA",
        "summary": "Publish the instagram variant",
        "payload": payload,
    }
    kwargs.update(overrides)
    return service.create(**kwargs)


def test_hash_is_stable_and_key_order_independent():
    assert payload_hash({"a": 1, "b": 2}) == payload_hash({"b": 2, "a": 1})
    assert payload_hash({"a": 1}) != payload_hash({"a": 2})


def test_create_records_pending_with_expiry(approvals):
    row = _create(approvals)
    assert row.state.value == "pending"
    assert row.payload_hash == payload_hash({"caption": "hello world", "platform": "instagram"})
    assert row.expires_at is not None
    assert row.consumed_at is None


def test_identical_pending_request_is_reused_not_duplicated(approvals, session):
    first = _create(approvals)
    second = _create(approvals)
    assert first.id == second.id
    assert session.query(ApprovalRequest).count() == 1


def test_approve_then_consume_once(approvals):
    row = _create(approvals)
    decided = approvals.decide(row.id, approve=True, note="looks right")
    assert decided.state.value == "approved"
    assert decided.decided_by == "local-user"
    approvals.consume(row.id, payload={"caption": "hello world", "platform": "instagram"})
    with pytest.raises(ApprovalStateError):
        approvals.consume(row.id, payload={"caption": "hello world", "platform": "instagram"})


def test_rejected_cannot_be_consumed(approvals):
    row = _create(approvals)
    approvals.decide(row.id, approve=False, note="no")
    with pytest.raises(ApprovalStateError):
        approvals.verify_for_use(row.id)


def test_mutated_payload_is_refused(approvals):
    row = _create(approvals)
    approvals.decide(row.id, approve=True)
    with pytest.raises(ApprovalHashMismatch):
        approvals.verify_for_use(row.id, payload={"caption": "different words", "platform": "instagram"})


def test_expired_request_cannot_be_approved(approvals, session):
    row = _create(approvals)
    session.get(ApprovalRequest, row.id).expires_at = datetime.now(UTC) - timedelta(minutes=1)
    session.flush()
    with pytest.raises(ApprovalExpired):
        approvals.decide(row.id, approve=True)
    assert session.get(ApprovalRequest, row.id).state.value == "expired"


def test_expired_request_cannot_be_consumed(approvals, session):
    row = _create(approvals)
    approvals.decide(row.id, approve=True)
    session.get(ApprovalRequest, row.id).expires_at = datetime.now(UTC) - timedelta(minutes=1)
    session.flush()
    with pytest.raises(ApprovalExpired):
        approvals.consume(row.id)


def test_double_decision_is_refused(approvals):
    row = _create(approvals)
    approvals.decide(row.id, approve=True)
    with pytest.raises(ApprovalStateError):
        approvals.decide(row.id, approve=False)


def test_cancel_only_from_pending(approvals):
    row = _create(approvals)
    approvals.cancel(row.id)
    with pytest.raises(ApprovalStateError):
        approvals.cancel(row.id)


def test_unknown_request(approvals):
    with pytest.raises(ApprovalNotFound):
        approvals.get("01ZZZZZZZZZZZZZZZZZZZZZZZZ")


def test_expire_stale_sweeps_only_pending(approvals, session):
    stale = _create(approvals)
    approved = _create(
        approvals,
        target_id="01BBBBBBBBBBBBBBBBBBBBBBBB",
        payload={"caption": "second", "platform": "instagram"},
    )
    approvals.decide(approved.id, approve=True)
    session.get(ApprovalRequest, stale.id).expires_at = datetime.now(UTC) - timedelta(hours=1)
    session.flush()
    assert approvals.expire_stale() == 1
    assert session.get(ApprovalRequest, stale.id).state.value == "expired"
    assert session.get(ApprovalRequest, approved.id).state.value == "approved"


def test_list_filters_by_state(approvals):
    _create(approvals)
    other = _create(approvals, target_id="01CCCCCCCCCCCCCCCCCCCCCCCC", payload={"x": 1})
    approvals.decide(other.id, approve=True)
    assert len(approvals.list(state="pending")) == 1
    assert len(approvals.list(state="approved")) == 1
    assert len(approvals.list()) == 2


def test_decisions_are_audited(approvals, session):
    from creatoros.db.models import AuditEvent

    row = _create(approvals)
    approvals.decide(row.id, approve=True)
    actions = {event.action for event in session.query(AuditEvent).all()}
    assert {"approval.request", "approval.decide"} <= actions


def test_ttl_is_bounded(approvals):
    row = _create(approvals, ttl_hours=100000)
    delta = row.expires_at.replace(tzinfo=UTC) - datetime.now(UTC)
    assert delta <= timedelta(hours=720, minutes=1)


def test_approval_is_bound_to_a_specific_objective(approvals):
    row = _create(approvals, objective_id="01DDDDDDDDDDDDDDDDDDDDDDDD")
    assert row.objective_id == "01DDDDDDDDDDDDDDDDDDDDDDDD"
    assert approvals.list(objective_id="01DDDDDDDDDDDDDDDDDDDDDDDD") == [row]
    assert approvals.list(objective_id="01EEEEEEEEEEEEEEEEEEEEEEEE") == []
