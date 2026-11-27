"""The single writer for the observation ledger.

Rules this module enforces, in order of how easily they are broken by accident:

1. **One writer.** Only `SnapshotWriter.write()` inserts into ``analytics_snapshot``.
2. **Run-scoped idempotency.** The unique key is
   ``(run_id, provider_object_id, metric_key, period_kind, period_start, period_end, breakdown_key)``,
   so replaying the same run updates its own rows instead of duplicating them.
3. **No downgrade.** A retry that returns ``missing`` after an earlier ``observed`` does not erase the
   observation; the observed value wins for the life of the run.
4. **Shapes stay apart.** ``value_kind`` from the catalogue decides the column; a structured value is
   stored whole and never truncated into a float. The database CHECK constraints enforce both.
5. **Attribution is captured now.** ``content_project_id``/``campaign_id``/``asset_id`` are strings
   copied at write time, so deleting the draft project later cannot erase what the video did.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ...db.enums import MetricAvailability, MetricCompleteness, Platform
from ...db.models import AnalyticsSnapshot, PlatformVariant, PublishJob
from ...integrations.analytics.base import ObservationDraft

#: Availability states that carry a measurement. One of them, by design.
_VALUED = {MetricAvailability.OBSERVED}

#: The database-side backstop, matching `uq_analytics_observation`.
_CONFLICT_KEY = [
    "run_id",
    "provider_object_id",
    "metric_key",
    "period_kind",
    "period_start",
    "period_end",
    "breakdown_key",
]


@dataclass(slots=True)
class WriteResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    kept_observation: int = 0
    metric_keys: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "kept_observation": self.kept_observation,
            "total": self.inserted + self.updated + self.unchanged + self.kept_observation,
        }


@dataclass(slots=True)
class Attribution:
    """Who published the object, captured as strings so it survives deletion."""

    publication_id: str | None = None
    content_project_id: str | None = None
    campaign_id: str | None = None
    asset_id: str | None = None


def attribute(
    session: Session,
    *,
    platform: Platform,
    provider_object_id: str,
    account_id: str | None = None,
) -> Attribution:
    """Find the publication behind a provider object id, if Phase 7 published it."""
    stmt = sa.select(PublishJob).where(
        PublishJob.platform == platform,
        sa.or_(
            PublishJob.remote_post_id == provider_object_id,
            PublishJob.provider_media_id == provider_object_id,
            PublishJob.remote_container_id == provider_object_id,
        ),
    )
    if account_id:
        stmt = stmt.where(PublishJob.account_id == account_id)
    job = session.scalar(stmt.order_by(PublishJob.created_at.desc()))
    if job is None:
        return Attribution()
    return _attribution_from_job(session, job)


def _attribution_from_job(session: Session, job: PublishJob) -> Attribution:
    """Job → variant → project. The campaign is on the project, not on the variant."""
    from ...db.models import ContentProject

    variant = session.get(PlatformVariant, job.variant_id)
    project = session.get(ContentProject, variant.project_id) if variant is not None else None
    return Attribution(
        publication_id=job.id,
        content_project_id=project.id if project is not None else None,
        campaign_id=project.campaign_id if project is not None else None,
        asset_id=job.asset_id,
    )


class SnapshotWriter:
    """Write drafts for one run. Never touches a row belonging to another run."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def write(
        self,
        *,
        run_id: str,
        account_id: str,
        platform: Platform,
        object_type,
        provider_object_id: str,
        drafts: list[ObservationDraft],
        api_version: str,
        api_host: str,
        source_ref: str,
        attribution: Attribution | None = None,
        collected_at: datetime | None = None,
        data_as_of: datetime | None = None,
        sample_size: int | None = None,
    ) -> WriteResult:
        collected = collected_at or datetime.now(UTC)
        attr = attribution or Attribution()
        result = WriteResult()
        for draft in drafts:
            row = self._values(
                run_id=run_id,
                account_id=account_id,
                platform=platform,
                object_type=object_type,
                provider_object_id=provider_object_id,
                draft=draft,
                api_version=api_version,
                api_host=api_host,
                source_ref=source_ref,
                attribution=attr,
                collected_at=collected,
                data_as_of=data_as_of,
                sample_size=sample_size,
            )
            outcome = self._upsert(row)
            setattr(result, outcome, getattr(result, outcome) + 1)
            result.metric_keys.append(draft.metric_key)
        self.session.flush()
        return result

    # ------------------------------------------------------------------ internals
    def _values(
        self,
        *,
        run_id: str,
        account_id: str,
        platform: Platform,
        object_type,
        provider_object_id: str,
        draft: ObservationDraft,
        api_version: str,
        api_host: str,
        source_ref: str,
        attribution: Attribution,
        collected_at: datetime,
        data_as_of: datetime | None,
        sample_size: int | None,
    ) -> dict:
        return {
            "run_id": run_id,
            "account_id": account_id,
            "platform": platform,
            "object_type": object_type,
            "provider_object_id": provider_object_id,
            "publication_id": attribution.publication_id,
            "content_project_id": attribution.content_project_id,
            "campaign_id": attribution.campaign_id,
            "asset_id": attribution.asset_id,
            "metric_key": draft.metric_key,
            "value_kind": draft.value_kind,
            "period_kind": draft.period_kind,
            "period_start": draft.period_start,
            "period_end": draft.period_end,
            "value_num": draft.value_num,
            "value_json": draft.value_json,
            "unit": draft.unit,
            "availability": draft.availability,
            "availability_reason": (draft.note or "")[:300] or None,
            "breakdown_key": "",
            "api_version": api_version,
            "api_host": api_host[:60],
            "source_ref": source_ref[:300],
            "collected_at": collected_at,
            "data_as_of": data_as_of,
            "completeness": MetricCompleteness.COMPLETE,
            "sample_size": sample_size,
        }

    def _upsert(self, values: dict) -> str:
        """Insert, or reconcile an existing row of the *same run* without ever downgrading it."""
        existing = self._find(values)
        if existing is not None:
            if existing.availability is MetricAvailability.OBSERVED and values["availability"] not in _VALUED:
                # A retry must not replace a measurement with "we did not get it".
                return "kept_observation"
            if (
                existing.availability is values["availability"]
                and existing.value_num == values["value_num"]
                and existing.value_json == values["value_json"]
            ):
                return "unchanged"
            self.session.execute(
                sa.update(AnalyticsSnapshot)
                .where(AnalyticsSnapshot.id == existing.id)
                .values(
                    value_num=values["value_num"],
                    value_json=values["value_json"],
                    availability=values["availability"],
                    availability_reason=values["availability_reason"],
                    value_kind=values["value_kind"],
                    unit=values["unit"],
                    collected_at=values["collected_at"],
                    api_version=values["api_version"],
                    source_ref=values["source_ref"],
                    publication_id=values["publication_id"],
                )
            )
            return "updated"

        stmt = (
            sqlite_insert(AnalyticsSnapshot)
            .values(**values)
            .on_conflict_do_nothing(index_elements=_CONFLICT_KEY)
        )
        inserted = self.session.execute(stmt)
        return "inserted" if inserted.rowcount else "unchanged"

    def _find(self, values: dict) -> AnalyticsSnapshot | None:
        """The row this draft would occupy, matched exactly (NULLs included).

        SQLite treats NULLs in a unique index as distinct, so `period_start IS NULL` rows are not
        deduplicated by the database alone. Looking the row up explicitly is what makes replayed
        runs idempotent for lifetime metrics; the unique index still protects every bounded bucket,
        which is where a duplicated day would be invisible in an average.
        """
        conditions = [
            AnalyticsSnapshot.run_id == values["run_id"],
            AnalyticsSnapshot.provider_object_id == values["provider_object_id"],
            AnalyticsSnapshot.metric_key == values["metric_key"],
            AnalyticsSnapshot.period_kind == values["period_kind"],
            AnalyticsSnapshot.breakdown_key == values["breakdown_key"],
        ]
        for column, key in ((AnalyticsSnapshot.period_start, "period_start"), (AnalyticsSnapshot.period_end, "period_end")):
            conditions.append(
                column.is_(None) if values[key] is None else column == values[key]
            )
        return self.session.scalar(sa.select(AnalyticsSnapshot).where(*conditions))


def observations_for_publication(session: Session, publication_id: str) -> list[AnalyticsSnapshot]:
    return list(
        session.scalars(
            sa.select(AnalyticsSnapshot)
            .where(AnalyticsSnapshot.publication_id == publication_id)
            .order_by(AnalyticsSnapshot.collected_at.desc())
        )
    )
