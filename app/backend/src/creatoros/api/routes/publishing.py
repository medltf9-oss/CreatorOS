"""The publishing API.

Shape of the surface, and why it is split this way:

* **composer** routes describe intent (campaign → intent → destination) and never publish;
* **approval** routes are the only way consent is granted, and they are separate from publishing so
  the GUI can show "approved" without implying "sent";
* **action** routes (`publish-now`, `retry`, `cancel`, `reconcile`) go through `PublishActions`, which
  enforces the rules that must hold for every caller - including a future agent tool;
* **history** routes are read-only projections from `PublishHistory`.

Nothing here starts an upload inline: publishing hands a job to the durable queue, which the publish
worker owns. The one exception is reconciliation, which reads remote state and therefore cannot
change what exists.

`/publishing/limits` and `/publishing/capabilities` are the honest-source answers a form needs. A
route that cannot work returns a reason (`enabled: false` plus `reason`), never a fake success.
"""
from __future__ import annotations

import dataclasses
import logging
from datetime import UTC, datetime
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, status

from ...db.enums import (
    Platform,
    PublishMode,
    PublishState,
    ReconciliationDecision,
    parse_publish_state,
)
from ...db.models import PlatformVariant, PublishJob, SocialAccount
from ...integrations.social.publishing.media_delivery.cloud import S3_COMPATIBLE_PROVIDERS
from ...integrations.social.publishing.metadata.limits import rules_for
from ...integrations.social.publishing.metadata.validators import validate_variant
from ...schemas.publishing import (
    ActionAffordances,
    ActionResponse,
    ApprovalDecision,
    ApprovalRequestCreate,
    ApprovalStatusResponse,
    AttemptListResponse,
    AuditTrailResponse,
    CampaignCreate,
    CampaignListResponse,
    CampaignUpdate,
    CancelRequest,
    DeliveryCheckResponse,
    DeliveryConfigUpdate,
    DeliveryResponse,
    DestinationCreate,
    DestinationResponse,
    DestinationUpdate,
    IntentCreate,
    IntentDetailResponse,
    IntentDuplicate,
    IntentListResponse,
    IntentUpdate,
    JobDetailResponse,
    JobListResponse,
    LatePolicyUpdate,
    LimitsResponse,
    OverviewResponse,
    QueueResponse,
    ReconcileResolveRequest,
    ReconciliationListResponse,
    RevalidateRequest,
    ScheduleRequest,
    ScheduleResponse,
    ValidateRequest,
    ValidationResponse,
)
from ...services.publishing.actions import PublishActions
from ...services.publishing.approvals import CONSENT_TEXT_VERSION, PublishApprovalService
from ...services.publishing.composer import PUBLISH_PROFILES, ComposerService
from ...services.publishing.delivery import DeliveryService
from ...services.publishing.errors import PublishingError
from ...services.publishing.executor import PublishExecutor
from ...services.publishing.history import JobQuery, PublishHistory
from ...services.publishing.reconciliation import ReconciliationService
from ...services.publishing.scheduler import PublishScheduler
from ...services.social.vault import VaultError
from ..deps import DbSession, Paging
from .social import get_vault

router = APIRouter(prefix="/publishing", tags=["publishing"])
log = logging.getLogger(__name__)


# --------------------------------------------------------------------- helpers
def _raise(error: PublishingError) -> None:
    raise HTTPException(
        status_code=error.http_status, detail={"code": error.code, "message": error.message, **(error.detail or {})}
    )


def _state(value: str | None) -> PublishState | None:
    if not value:
        return None
    try:
        return parse_publish_state(value)
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"code": "UNKNOWN_STATE", "message": f"{value!r} is not a publish state"},
        )


def _states(values: list[str] | None) -> tuple[PublishState, ...]:
    if not values:
        return ()
    resolved: list[PublishState] = []
    for value in values:
        for part in value.split(","):
            state = _state(part.strip())
            if state is not None:
                resolved.append(state)
    return tuple(resolved)


def _platform(value: str | None) -> Platform | None:
    if not value:
        return None
    try:
        return Platform(value)
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"code": "UNKNOWN_PLATFORM", "message": f"{value!r} is not a known platform"},
        )


def _mode(value: str | None) -> PublishMode | None:
    if not value:
        return None
    try:
        return PublishMode(value)
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"code": "UNKNOWN_MODE", "message": f"{value!r} is not a publish mode"},
        )


def _job(session, job_id: str) -> PublishJob:
    job = session.get(PublishJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "JOB_NOT_FOUND", "message": "no such destination"})
    return job


def _variant_job(session, variant_id: str) -> tuple[PlatformVariant, PublishJob]:
    variant = session.get(PlatformVariant, variant_id)
    if variant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "DESTINATION_NOT_FOUND", "message": "no such destination"})
    job = session.scalar(sa.select(PublishJob).where(PublishJob.variant_id == variant_id))
    if job is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "DESTINATION_WITHOUT_JOB", "message": "this destination has no publish job"},
        )
    return variant, job


def _actions(session, executor: PublishExecutor | None = None) -> PublishActions:
    return PublishActions(session, executor=executor)


def _executor(session) -> PublishExecutor:
    # The vault is the Electron-side broker: the backend never holds the key (ADR-22).
    return PublishExecutor(session, vault=get_vault())


def _variant_summary(variant: PlatformVariant, job: PublishJob) -> dict[str, Any]:
    return {
        "id": variant.id,
        "project_id": variant.project_id,
        "platform": str(variant.platform),
        "mode": str(job.publish_mode),
        "account_id": variant.target_account_id,
        "job_id": job.id,
        "state": str(job.job_state),
        "state_reason": job.state_reason,
        "title": variant.title,
        "caption": variant.caption,
        "hashtags": list(variant.hashtags_json or []),
        "privacy_setting": variant.privacy_setting,
        "aspect_ratio": variant.aspect_ratio,
        "duration_ms": variant.duration_ms,
        "scheduled_at_utc": job.scheduled_at_utc,
        "display_timezone": job.display_timezone,
        "validation": dict(variant.validation_report_json or {}),
        "approval_status": str(variant.approval_status),
        "provider_permalink": job.provider_permalink,
    }


def _project_summary(project, destinations: list[dict[str, Any]]) -> dict[str, Any]:
    states = [destination["state"] for destination in destinations]
    return {
        "id": project.id,
        "title": project.title,
        "brief": project.brief,
        "campaign_id": project.campaign_id,
        "source_asset_id": project.source_asset_id,
        "status": str(project.status),
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "destinations": len(destinations),
        "states": states,
    }


# --------------------------------------------------------------------- overview
@router.get("/overview", response_model=OverviewResponse)
async def overview(session: DbSession) -> OverviewResponse:
    history = PublishHistory(session)
    scheduler = PublishScheduler(session, executor=None)
    policy = scheduler.load_policy()
    delivery = DeliveryService(session, vault=get_vault())
    delivery_status = await delivery.status()
    accounts = session.scalars(sa.select(SocialAccount).order_by(SocialAccount.display_name)).all()
    capabilities = [
        {
            "id": account.id,
            "platform": str(account.platform),
            "display_name": account.display_name,
            "connection_status": str(account.connection_status),
            "token_state": str(account.token_state),
        }
        for account in accounts
    ]
    return OverviewResponse(
        summary=history.summary(),
        delivery={
            "configured": delivery_status["configured"],
            "missing": delivery_status["missing"],
            "provider": delivery_status["provider"],
            "bucket": delivery_status["bucket"],
            "health": delivery_status["health"],
        },
        schedule={"policy": policy.as_dict(), "now": datetime.now(UTC)},
        composer={"profiles": list(PUBLISH_PROFILES)},
        capabilities=capabilities,
    )


# -------------------------------------------------------------------- campaigns
@router.get("/campaigns", response_model=CampaignListResponse)
def list_campaigns(session: DbSession, paging: Paging) -> CampaignListResponse:
    from ...db.models import Campaign

    rows = session.scalars(
        sa.select(Campaign).order_by(Campaign.created_at.desc()).limit(paging.limit).offset(paging.offset)
    )
    items = [
        {
            "id": row.id,
            "name": row.name,
            "goal": row.goal,
            "starts_on": row.starts_on,
            "ends_on": row.ends_on,
            "created_at": row.created_at,
        }
        for row in rows
    ]
    total = session.scalar(sa.select(sa.func.count()).select_from(Campaign)) or 0
    return CampaignListResponse(items=items, total=int(total))


@router.post("/campaigns", response_model=CampaignListResponse, status_code=status.HTTP_201_CREATED)
def create_campaign(body: CampaignCreate, session: DbSession) -> CampaignListResponse:
    try:
        campaign = ComposerService(session).create_campaign(
            name=body.name,
            goal=body.objective or body.description,
            starts_on=body.starts_at,
            ends_on=body.ends_at,
        )
    except PublishingError as error:
        _raise(error)
    return CampaignListResponse(
        items=[
            {
                "id": campaign.id,
                "name": campaign.name,
                "goal": campaign.goal,
                "starts_on": campaign.starts_on,
                "ends_on": campaign.ends_on,
                "created_at": campaign.created_at,
            }
        ],
        total=1,
    )


@router.patch("/campaigns/{campaign_id}", response_model=CampaignListResponse)
def update_campaign(campaign_id: str, body: CampaignUpdate, session: DbSession) -> CampaignListResponse:
    fields = body.model_dump(exclude_unset=True)
    mapping = {"objective": "goal", "description": "goal", "starts_at": "starts_on", "ends_at": "ends_on"}
    payload = {mapping.get(key, key): value for key, value in fields.items()}
    try:
        campaign = ComposerService(session).update_campaign(campaign_id, **payload)
    except PublishingError as error:
        _raise(error)
    return CampaignListResponse(
        items=[
            {
                "id": campaign.id,
                "name": campaign.name,
                "goal": campaign.goal,
                "starts_on": campaign.starts_on,
                "ends_on": campaign.ends_on,
                "created_at": campaign.created_at,
            }
        ],
        total=1,
    )


# ---------------------------------------------------------------------- intents
@router.get("/intents", response_model=IntentListResponse)
def list_intents(
    session: DbSession,
    paging: Paging,
    campaign_id: str | None = None,
    state: Annotated[list[str] | None, Query()] = None,
    platform: str | None = None,
    search: str | None = None,
) -> IntentListResponse:
    from ...db.models import ContentProject

    stmt = sa.select(ContentProject).order_by(ContentProject.created_at.desc())
    count = sa.select(sa.func.count()).select_from(ContentProject)
    if campaign_id:
        stmt = stmt.where(ContentProject.campaign_id == campaign_id)
        count = count.where(ContentProject.campaign_id == campaign_id)
    if search:
        needle = f"%{search.strip()}%"
        stmt = stmt.where(ContentProject.title.ilike(needle))
        count = count.where(ContentProject.title.ilike(needle))

    projects = session.scalars(stmt).all()
    items: list[dict[str, Any]] = []
    wanted_states = {str(state_value) for state_value in _states(state)}
    for project in projects:
        destinations = session.execute(
            sa.select(PlatformVariant, PublishJob)
            .join(PublishJob, PublishJob.variant_id == PlatformVariant.id)
            .where(PlatformVariant.project_id == project.id)
        ).all()
        summaries = [_variant_summary(variant, job) for variant, job in destinations]
        if platform:
            summaries = [item for item in summaries if item["platform"] == platform]
        if wanted_states:
            summaries = [item for item in summaries if item["state"] in wanted_states]
        if (platform or wanted_states) and not summaries:
            continue
        items.append(_project_summary(project, summaries))
    total = len(items) if (platform or wanted_states) else int(session.scalar(count) or 0)
    return IntentListResponse(
        items=items[paging.offset : paging.offset + paging.limit], total=total, limit=paging.limit, offset=paging.offset
    )


@router.post("/intents", response_model=IntentDetailResponse, status_code=status.HTTP_201_CREATED)
def create_intent(body: IntentCreate, session: DbSession) -> IntentDetailResponse:
    try:
        project = ComposerService(session).create_intent(
            title=body.title, campaign_id=body.campaign_id, source_asset_id=body.source_asset_id, brief=body.notes
        )
    except PublishingError as error:
        _raise(error)
    return IntentDetailResponse(project=_project_summary(project, []), destinations=[])


@router.get("/intents/{project_id}", response_model=IntentDetailResponse)
def get_intent(project_id: str, session: DbSession) -> IntentDetailResponse:
    from ...db.models import ContentProject

    project = session.get(ContentProject, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "INTENT_NOT_FOUND", "message": "no such intent"})
    rows = session.execute(
        sa.select(PlatformVariant, PublishJob)
        .join(PublishJob, PublishJob.variant_id == PlatformVariant.id)
        .where(PlatformVariant.project_id == project_id)
    ).all()
    destinations = [_variant_summary(variant, job) for variant, job in rows]
    return IntentDetailResponse(
        project=_project_summary(project, destinations), destinations=destinations
    )


@router.patch("/intents/{project_id}", response_model=IntentDetailResponse)
def update_intent(project_id: str, body: IntentUpdate, session: DbSession) -> IntentDetailResponse:
    fields = body.model_dump(exclude_unset=True)
    if "notes" in fields:
        fields["brief"] = fields.pop("notes")
    try:
        ComposerService(session).update_intent(project_id, **fields)
    except PublishingError as error:
        _raise(error)
    return get_intent(project_id, session)


@router.delete("/intents/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_intent(project_id: str, session: DbSession) -> None:
    try:
        ComposerService(session).delete_intent(project_id)
    except PublishingError as error:
        _raise(error)


@router.post("/intents/{project_id}/duplicate", response_model=IntentDetailResponse, status_code=201)
def duplicate_intent(project_id: str, body: IntentDuplicate, session: DbSession) -> IntentDetailResponse:
    try:
        copy = ComposerService(session).duplicate_intent(project_id, title=body.title)
    except PublishingError as error:
        _raise(error)
    return get_intent(copy.id, session)


# ----------------------------------------------------------------- destinations
@router.post("/intents/{project_id}/destinations", response_model=DestinationResponse, status_code=201)
def add_destination(project_id: str, body: DestinationCreate, session: DbSession) -> DestinationResponse:
    try:
        destination = ComposerService(session).add_destination(
            project_id,
            platform=str(body.platform),
            account_id=body.account_id,
            mode=body.mode,
            title=body.title,
            caption=body.caption,
            hashtags=body.hashtags,
            options=body.options,
            privacy_setting=body.privacy_setting,
            scheduled_at_utc=body.scheduled_at_utc,
            display_timezone=body.display_timezone,
            asset_id=body.asset_id,
            field_sources=body.field_sources,
        )
    except PublishingError as error:
        _raise(error)
    return DestinationResponse(
        variant=_variant_summary(destination.variant, destination.job),
        job=PublishHistory(session).job(destination.job.id) or {},
    )


@router.get("/destinations/{variant_id}", response_model=DestinationResponse)
def get_destination(variant_id: str, session: DbSession) -> DestinationResponse:
    variant, job = _variant_job(session, variant_id)
    return DestinationResponse(
        variant=_variant_summary(variant, job), job=PublishHistory(session).job(job.id) or {}
    )


@router.get("/destinations/{variant_id}/actions", response_model=ActionAffordances)
def destination_actions(variant_id: str, session: DbSession) -> ActionAffordances:
    _, job = _variant_job(session, variant_id)
    describe = _actions(session).describe(job)
    return ActionAffordances(**describe)


@router.patch("/destinations/{variant_id}", response_model=DestinationResponse)
def update_destination(variant_id: str, body: DestinationUpdate, session: DbSession) -> DestinationResponse:
    try:
        destination = ComposerService(session).update_destination(
            variant_id, fields=body.fields, field_sources=body.field_sources
        )
    except PublishingError as error:
        _raise(error)
    return DestinationResponse(
        variant=_variant_summary(destination.variant, destination.job),
        job=PublishHistory(session).job(destination.job.id) or {},
    )


@router.delete("/destinations/{variant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_destination(variant_id: str, session: DbSession) -> None:
    try:
        ComposerService(session).delete_destination(variant_id)
    except PublishingError as error:
        _raise(error)


@router.post("/destinations/{variant_id}/revalidate", response_model=ValidationResponse)
def revalidate_destination(
    variant_id: str, body: RevalidateRequest | None = None, *, session: DbSession
) -> ValidationResponse:
    try:
        report = ComposerService(session).revalidate(
            variant_id, creator_info=(body.creator_info if body else None)
        )
    except PublishingError as error:
        _raise(error)
    return ValidationResponse(report=report.as_dict())


# -------------------------------------------------------------------- approvals
@router.get("/destinations/{variant_id}/approval", response_model=ApprovalStatusResponse)
def approval_status(variant_id: str, session: DbSession) -> ApprovalStatusResponse:
    _, job = _variant_job(session, variant_id)
    service = PublishApprovalService(session)
    state = str(job.job_state)
    payload: dict[str, Any] | None = None
    pending = job.approval_record_id is None and job.approval_request_id is not None
    can_request = state in {
        PublishState.DRAFT.value,
        PublishState.PREPARING.value,
        PublishState.VALIDATION_PENDING.value,
        PublishState.AWAITING_APPROVAL.value,
    }
    reason: str | None = None
    try:
        payload = service.canonical_payload(job)
    except PublishingError as error:
        reason = error.code
    if not can_request and job.approval_record_id is None:
        reason = reason or "this destination is past the approval stage"
    return ApprovalStatusResponse(
        pending=pending,
        request_id=job.approval_request_id,
        state=state,
        payload_hash=job.approval_payload_hash,
        approved_hash=job.payload_hash,
        payload=payload,
        consent_text_version=CONSENT_TEXT_VERSION,
        can_request=can_request,
        reason=reason,
    )


@router.post("/destinations/{variant_id}/approval", response_model=ApprovalStatusResponse)
def request_approval(
    variant_id: str, body: ApprovalRequestCreate, session: DbSession
) -> ApprovalStatusResponse:
    _, job = _variant_job(session, variant_id)
    try:
        PublishApprovalService(session).request(
            job, summary=body.summary, risks=body.risks, ttl_hours=body.ttl_hours or 72
        )
    except PublishingError as error:
        _raise(error)
    return approval_status(variant_id, session)


@router.post("/destinations/{variant_id}/approval/decide", response_model=ApprovalStatusResponse)
def decide_approval(variant_id: str, body: ApprovalDecision, session: DbSession) -> ApprovalStatusResponse:
    _, job = _variant_job(session, variant_id)
    try:
        PublishApprovalService(session).decide(job, approve=body.approve, note=body.note)
    except PublishingError as error:
        _raise(error)
    return approval_status(variant_id, session)


# --------------------------------------------------------------------- schedule
@router.post("/destinations/{variant_id}/schedule", response_model=ActionResponse)
def schedule_destination(variant_id: str, body: ScheduleRequest, session: DbSession) -> ActionResponse:
    _, job = _variant_job(session, variant_id)
    try:
        outcome = _actions(session, _executor(session)).reschedule(
            job, when=body.scheduled_at_utc, display_timezone=body.display_timezone
        )
    except PublishingError as error:
        _raise(error)
    return ActionResponse(**outcome.as_dict())


@router.get("/schedule", response_model=ScheduleResponse)
def schedule_view(session: DbSession, days: int = Query(default=14, ge=1, le=90)) -> ScheduleResponse:
    history = PublishHistory(session)
    view = history.schedule(days=days)
    policy = PublishScheduler(session, executor=None).load_policy()
    return ScheduleResponse(
        now=view["now"],
        scheduled=view["scheduled"],
        missed=view["missed"],
        missed_total=view["missed_total"],
        policy=policy.as_dict(),
    )


@router.get("/schedule/policy", response_model=dict)
def get_policy(session: DbSession) -> dict:
    return PublishScheduler(session, executor=None).load_policy().as_dict()


@router.put("/schedule/policy", response_model=dict)
def set_policy(body: LatePolicyUpdate, session: DbSession) -> dict:
    try:
        policy = PublishScheduler(session, executor=None).set_policy(
            policy=body.policy, grace_seconds=body.grace_seconds
        )
    except PublishingError as error:
        _raise(error)
    return policy.as_dict()


@router.post("/schedule/tick", response_model=dict)
def run_sweep(session: DbSession) -> dict:
    """Run one sweep now. This is what the sweeper thread does, exposed for the GUI's "check now"."""
    scheduler = PublishScheduler(session, executor=_executor(session))
    return scheduler.tick().as_dict()


@router.post("/destinations/{variant_id}/publish-now", response_model=ActionResponse)
def publish_now(variant_id: str, session: DbSession) -> ActionResponse:
    _, job = _variant_job(session, variant_id)
    try:
        outcome = _actions(session, _executor(session)).publish_now(job)
    except PublishingError as error:
        _raise(error)
    return ActionResponse(**outcome.as_dict())


@router.post("/destinations/{variant_id}/retry", response_model=ActionResponse)
def retry_destination(variant_id: str, session: DbSession) -> ActionResponse:
    _, job = _variant_job(session, variant_id)
    try:
        outcome = _actions(session, _executor(session)).retry(job)
    except PublishingError as error:
        _raise(error)
    return ActionResponse(**outcome.as_dict())


@router.post("/destinations/{variant_id}/cancel", response_model=ActionResponse)
def cancel_destination(variant_id: str, body: CancelRequest, session: DbSession) -> ActionResponse:
    _, job = _variant_job(session, variant_id)
    try:
        outcome = _actions(session, _executor(session)).cancel(job, reason=body.reason)
    except PublishingError as error:
        _raise(error)
    return ActionResponse(**outcome.as_dict())


@router.post("/destinations/{variant_id}/publish-missed-now", response_model=ActionResponse)
def publish_missed_now(variant_id: str, session: DbSession) -> ActionResponse:
    _, job = _variant_job(session, variant_id)
    try:
        PublishScheduler(session, executor=_executor(session)).publish_missed_now(job)
    except PublishingError as error:
        _raise(error)
    return ActionResponse(action="publish_missed_now", state=str(job.job_state), queued=True, reason="queued")


# ------------------------------------------------------------------ reconciliation
@router.post("/destinations/{variant_id}/reconcile", response_model=ActionResponse)
async def reconcile_destination(variant_id: str, session: DbSession) -> ActionResponse:
    _, job = _variant_job(session, variant_id)
    try:
        observation = await ReconciliationService(session).check(job)
    except PublishingError as error:
        _raise(error)
    except VaultError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, error.to_dict())
    return ActionResponse(
        action="reconcile",
        state=str(job.job_state),
        queued=False,
        reason=observation.decision.value,
        detail=observation.as_dict(),
    )


@router.post("/destinations/{variant_id}/reconcile/resolve", response_model=ActionResponse)
def resolve_reconciliation(
    variant_id: str, body: ReconcileResolveRequest, session: DbSession
) -> ActionResponse:
    _, job = _variant_job(session, variant_id)
    if body.decision is ReconciliationDecision.UNDETERMINED:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {
                "code": "UNDETERMINED_IS_NOT_A_RESOLUTION",
                "message": "A person cannot resolve a case as undetermined; leave it open and check again.",
            },
        )
    case = ReconciliationService(session).resolve_manually(
        job, decision=body.decision, note=body.note, permalink=body.permalink
    )
    return ActionResponse(
        action="reconcile.resolve",
        state=str(job.job_state),
        queued=False,
        reason=str(case.decision),
        detail={"case_id": case.id},
    )


@router.post("/reconcile/sweep", response_model=dict)
async def reconcile_sweep(session: DbSession) -> dict:
    return await ReconciliationService(session).check_all()


@router.get("/reconciliations", response_model=ReconciliationListResponse)
def open_reconciliations(session: DbSession, paging: Paging) -> ReconciliationListResponse:
    service = ReconciliationService(session)
    cases = service.open_cases(limit=paging.limit)
    return ReconciliationListResponse(
        items=[
            {
                "id": case.id,
                "job_id": case.job_id,
                "checked_at": case.checked_at,
                "provider_state": case.provider_state,
                "decision": str(case.decision),
                "evidence": case.evidence_json,
            }
            for case in cases
        ],
        total=len(cases),
    )


# ----------------------------------------------------------------------- history
@router.get("/jobs", response_model=JobListResponse)
def list_jobs(
    session: DbSession,
    paging: Paging,
    platform: str | None = None,
    account_id: str | None = None,
    campaign_id: str | None = None,
    project_id: str | None = None,
    state: Annotated[list[str] | None, Query()] = None,
    mode: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    search: str | None = None,
) -> JobListResponse:
    query = JobQuery(
        platform=_platform(platform),
        account_id=account_id,
        campaign_id=campaign_id,
        project_id=project_id,
        state=_states(state),
        mode=_mode(mode),
        since=since,
        until=until,
        search=search,
        limit=paging.limit,
        offset=paging.offset,
    )
    rows, total = PublishHistory(session).jobs(query)
    return JobListResponse(items=rows, total=total, limit=paging.limit, offset=paging.offset)


@router.get("/jobs/{job_id}", response_model=JobDetailResponse)
def get_job(job_id: str, session: DbSession) -> JobDetailResponse:
    detail = PublishHistory(session).job(job_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "JOB_NOT_FOUND", "message": "no such destination"})
    return JobDetailResponse(job=detail)


@router.get("/jobs/{job_id}/attempts", response_model=AttemptListResponse)
def get_attempts(job_id: str, session: DbSession) -> AttemptListResponse:
    _job(session, job_id)
    items = PublishHistory(session).attempts(job_id)
    return AttemptListResponse(items=items, total=len(items))


@router.get("/jobs/{job_id}/deliveries", response_model=dict)
def get_deliveries(job_id: str, session: DbSession) -> dict:
    _job(session, job_id)
    items = PublishHistory(session).deliveries(job_id)
    return {"items": items, "total": len(items)}


@router.get("/jobs/{job_id}/reconciliations", response_model=ReconciliationListResponse)
def get_reconciliations(job_id: str, session: DbSession) -> ReconciliationListResponse:
    _job(session, job_id)
    items = PublishHistory(session).reconciliations(job_id)
    return ReconciliationListResponse(items=items, total=len(items))


@router.get("/jobs/{job_id}/audit", response_model=AuditTrailResponse)
def get_audit(job_id: str, session: DbSession, paging: Paging) -> AuditTrailResponse:
    _job(session, job_id)
    return AuditTrailResponse(items=PublishHistory(session).audit_trail(job_id, limit=paging.limit))


@router.get("/history", response_model=JobListResponse)
def published_history(
    session: DbSession,
    paging: Paging,
    platform: str | None = None,
    account_id: str | None = None,
    campaign_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    search: str | None = None,
) -> JobListResponse:
    query = JobQuery(
        platform=_platform(platform),
        account_id=account_id,
        campaign_id=campaign_id,
        since=since,
        until=until,
        search=search,
        limit=paging.limit,
        offset=paging.offset,
    )
    rows, total = PublishHistory(session).published(query)
    return JobListResponse(items=rows, total=total, limit=paging.limit, offset=paging.offset)


@router.get("/attention", response_model=JobListResponse)
def attention(session: DbSession, paging: Paging) -> JobListResponse:
    rows = PublishHistory(session).attention(limit=paging.limit)
    return JobListResponse(items=rows, total=len(rows), limit=paging.limit, offset=0)


@router.get("/queue", response_model=QueueResponse)
def queue_view(session: DbSession) -> QueueResponse:
    snapshot = PublishHistory(session).queue_snapshot()
    return QueueResponse(**snapshot)


# ---------------------------------------------------------------------- delivery
@router.get("/delivery", response_model=DeliveryResponse)
async def get_delivery(session: DbSession) -> DeliveryResponse:
    service = DeliveryService(session, vault=get_vault())
    state = await service.status()
    objects = PublishHistory(session).deliveries_for_all(limit=50)
    return DeliveryResponse(
        configured=state["configured"],
        provider=state["provider"],
        bucket=state["bucket"],
        region=state["region"],
        public_base_url_present=bool(state["public_base_url"]),
        prefix=(service.load_config().prefix if service.load_config() else None),
        ttl_seconds=state["url_ttl_s"],
        credentials_present=bool(state["credential_ref"]),
        reason=None if state["configured"] else ", ".join(state["missing"]),
        objects=objects,
        supported_providers=sorted(S3_COMPATIBLE_PROVIDERS),
    )


@router.put("/delivery", response_model=DeliveryResponse)
async def configure_delivery(body: DeliveryConfigUpdate, session: DbSession) -> DeliveryResponse:
    service = DeliveryService(session, vault=get_vault())
    fields = {
        "provider": body.provider,
        "bucket": body.bucket,
        "region": body.region,
        "public_base_url": body.public_base_url,
        "endpoint_url": body.endpoint_url,
        "prefix": body.prefix,
        "url_ttl_s": body.ttl_seconds,
    }
    try:
        await service.configure(fields=fields, secret_ref_id=body.credentials_secret_id)
    except PublishingError as error:
        _raise(error)
    except VaultError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, error.to_dict())
    return await get_delivery(session)


@router.delete("/delivery", status_code=status.HTTP_204_NO_CONTENT)
async def clear_delivery(session: DbSession) -> None:
    await DeliveryService(session, vault=get_vault()).clear()


@router.post("/delivery/check", response_model=DeliveryCheckResponse)
async def check_delivery(session: DbSession) -> DeliveryCheckResponse:
    service = DeliveryService(session, vault=get_vault())
    health = await service.health()
    return DeliveryCheckResponse(
        ok=health.available,
        reason=health.detail or ("available" if health.available else "unavailable"),
        detail={"code": health.reason_code},
    )


@router.post("/delivery/cleanup", response_model=dict)
async def cleanup_delivery(session: DbSession) -> dict:
    return await DeliveryService(session, vault=get_vault()).cleanup()


# ------------------------------------------------------------------- validation
def _rules_dict(rules) -> dict[str, Any]:
    """The published limits, as data, including the `None`s that mean "the provider sets no cap"."""
    text = dataclasses.asdict(rules.text)
    media = dataclasses.asdict(rules.media)
    for key in ("containers", "video_codecs", "audio_codecs", "pixel_formats"):
        media[key] = sorted(media[key])
    return {
        "platform": rules.platform,
        "mode": rules.mode.value,
        "docs_url": rules.docs_url,
        "requires_public_url": rules.requires_public_url,
        "known_options": sorted(rules.known_options),
        "text": text,
        "media": media,
    }


@router.get("/limits", response_model=LimitsResponse)
def get_limits(platform: str, mode: str | None = None) -> LimitsResponse:
    resolved_mode = _mode(mode) or _default_mode(platform)
    try:
        rules = rules_for(platform, resolved_mode)
    except KeyError as error:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "NO_RULES", "message": f"no publishing rules for {platform}/{resolved_mode.value}"},
        ) from error
    return LimitsResponse(platform=platform, mode=resolved_mode.value, rules=_rules_dict(rules))


@router.post("/validate", response_model=ValidationResponse)
def validate(body: ValidateRequest, session: DbSession) -> ValidationResponse:
    from ...db.models import MediaAsset
    from ...services.publishing.composer import _asset_probe

    mode = body.mode or _default_mode(str(body.platform))
    probe = None
    size = None
    if body.asset_id:
        asset = session.get(MediaAsset, body.asset_id)
        if asset is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "ASSET_NOT_FOUND", "message": "no such asset"})
        probe = _asset_probe(asset)
        size = asset.size_bytes
    report = validate_variant(
        str(body.platform),
        mode,
        title=body.title,
        caption=body.caption,
        hashtags=body.hashtags,
        options=body.options,
        probe=probe,
        size_bytes=size,
        creator_info=body.creator_info,
    )
    return ValidationResponse(report=report.as_dict())


@router.get("/capabilities", response_model=dict)
def publish_capabilities(session: DbSession) -> dict:
    """Which account can publish what, from the capability engine's rows - not from a guess."""
    from ...db.models import AccountCapability

    rows = session.execute(
        sa.select(SocialAccount, AccountCapability)
        .join(AccountCapability, AccountCapability.account_id == SocialAccount.id, isouter=True)
    ).all()
    accounts: dict[str, dict[str, Any]] = {}
    for account, capability in rows:
        entry = accounts.setdefault(
            account.id,
            {
                "id": account.id,
                "platform": str(account.platform),
                "display_name": account.display_name,
                "connection_status": str(account.connection_status),
                "token_state": str(account.token_state),
                "capabilities": {},
            },
        )
        if capability is not None:
            entry["capabilities"][capability.capability] = {
                "state": str(capability.state),
                "reason": capability.reason,
            }
    return {"accounts": list(accounts.values())}


def _default_mode(platform: str) -> PublishMode:
    from ...db.enums import Platform

    return {
        Platform.FACEBOOK.value: PublishMode.VIDEO,
        Platform.INSTAGRAM.value: PublishMode.REELS,
        Platform.TIKTOK.value: PublishMode.DIRECT_POST,
    }.get(platform, PublishMode.VIDEO)
