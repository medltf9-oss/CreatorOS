"""Enumerations shared by models, services and API schemas.

Values are lower_snake_case strings so they can be stored, compared and shown
verbatim in the UI without translation tables.
"""
from __future__ import annotations

from enum import StrEnum


class Platform(StrEnum):
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"


class AccountType(StrEnum):
    FACEBOOK_PAGE = "facebook_page"
    INSTAGRAM_BUSINESS = "instagram_business"
    INSTAGRAM_CREATOR = "instagram_creator"
    TIKTOK_CREATOR = "tiktok_creator"
    PERSONAL_UNSUPPORTED = "personal_unsupported"


class ConnectionStatus(StrEnum):
    CONNECTED = "connected"
    MISSING_PERMISSION = "missing_permission"
    NEEDS_APP_REVIEW = "needs_app_review"
    UNSUPPORTED_ACCOUNT_TYPE = "unsupported_account_type"
    TOKEN_EXPIRED = "token_expired"
    RATE_LIMITED = "rate_limited"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"


class CapabilityState(StrEnum):
    """Normalized capability states (ADR-24).

    The engine emits only ``CANONICAL_CAPABILITY_STATES``. The remaining members are retained
    because older rows and Phase 4/5 code mention them; ``normalize_capability_state`` maps them
    onto the canonical set so two vocabularies can never reach the UI.
    """

    AVAILABLE = "available"
    NEEDS_USER_CONSENT = "needs_user_consent"
    REQUIRES_DEVELOPER_APPROVAL = "requires_developer_approval"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    EXPIRED = "expired"
    REVOKED = "revoked"
    MANUAL_ONLY = "manual_only"
    # Legacy synonyms kept for stored data compatibility.
    ELIGIBLE = "eligible"
    MISSING_PERMISSION = "missing_permission"
    NEEDS_APP_REVIEW = "needs_app_review"
    UNSUPPORTED_ACCOUNT_TYPE = "unsupported_account_type"
    TOKEN_EXPIRED = "token_expired"
    RATE_LIMITED = "rate_limited"


CANONICAL_CAPABILITY_STATES: frozenset[CapabilityState] = frozenset(
    {
        CapabilityState.AVAILABLE,
        CapabilityState.NEEDS_USER_CONSENT,
        CapabilityState.REQUIRES_DEVELOPER_APPROVAL,
        CapabilityState.UNSUPPORTED,
        CapabilityState.UNAVAILABLE,
        CapabilityState.UNKNOWN,
        CapabilityState.EXPIRED,
        CapabilityState.REVOKED,
        CapabilityState.MANUAL_ONLY,
    }
)

LEGACY_CAPABILITY_STATES: dict[str, CapabilityState] = {
    "eligible": CapabilityState.AVAILABLE,
    "missing_permission": CapabilityState.NEEDS_USER_CONSENT,
    "needs_app_review": CapabilityState.REQUIRES_DEVELOPER_APPROVAL,
    "unsupported_account_type": CapabilityState.UNSUPPORTED,
    "token_expired": CapabilityState.EXPIRED,
    "rate_limited": CapabilityState.UNAVAILABLE,
}


def normalize_capability_state(value: str | CapabilityState | None) -> CapabilityState:
    """Map any stored or legacy state string onto the canonical vocabulary."""
    if value is None:
        return CapabilityState.UNKNOWN
    text = value.value if isinstance(value, CapabilityState) else str(value).strip().lower()
    if text in LEGACY_CAPABILITY_STATES:
        return LEGACY_CAPABILITY_STATES[text]
    try:
        state = CapabilityState(text)
    except ValueError:
        return CapabilityState.UNKNOWN
    return LEGACY_CAPABILITY_STATES.get(state.value, state)


class TokenState(StrEnum):
    """Unified token lifecycle (Part 10)."""

    VALID = "valid"
    EXPIRING_SOON = "expiring_soon"
    EXPIRED = "expired"
    REFRESH_REQUIRED = "refresh_required"
    REFRESH_IN_PROGRESS = "refresh_in_progress"
    REFRESH_FAILED = "refresh_failed"
    REVOKED = "revoked"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    UNKNOWN = "unknown"


TOKEN_NEEDS_ATTENTION: frozenset[TokenState] = frozenset(
    {
        TokenState.EXPIRED,
        TokenState.REFRESH_REQUIRED,
        TokenState.REFRESH_FAILED,
        TokenState.REVOKED,
        TokenState.REAUTHORIZATION_REQUIRED,
        TokenState.CREDENTIAL_UNAVAILABLE,
    }
)


class SecretStatus(StrEnum):
    ACTIVE = "active"
    UNAVAILABLE = "unavailable"
    REVOKED = "revoked"
    DELETED = "deleted"


class OAuthRoute(StrEnum):
    """Login path. A provider can have more than one, and they are never merged."""

    FACEBOOK_LOGIN = "facebook_login"
    INSTAGRAM_LOGIN = "instagram_login"
    TIKTOK_LOGIN = "tiktok_login"


class RotationState(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class MediaKind(StrEnum):
    """What a file actually is, decided by ffprobe rather than by its extension."""

    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    UNKNOWN = "unknown"


class IntakeState(StrEnum):
    """How far an import got. Only READY assets are library-usable."""

    PENDING = "pending"
    COPYING = "copying"
    PROBING = "probing"
    READY = "ready"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"


class DerivativeKind(StrEnum):
    THUMBNAIL = "thumbnail"
    TIMELINE = "timeline"
    PREVIEW = "preview"
    PROXY = "proxy"
    TRANSCODE = "transcode"
    AUDIO = "audio"
    WAVEFORM = "waveform"
    WAVEFORM_IMAGE = "waveform_image"


class DerivativeState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class ArchiveState(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class RightsStatus(StrEnum):
    OWNED = "owned"
    LICENSED = "licensed"
    UNCERTAIN = "uncertain"
    THIRD_PARTY = "third_party"


class ProjectStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    PARTIALLY_PUBLISHED = "partially_published"
    PUBLISHED = "published"
    FAILED = "failed"
    ARCHIVED = "archived"


class ApprovalStatus(StrEnum):
    DRAFT = "draft"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    STALE = "stale"


class PublishState(StrEnum):
    """Destination lifecycle (ADR-25).

    The canonical members are the vocabulary the API, the queue and the GUI speak.
    ``LEGACY_PUBLISH_STATES`` holds the Phase 2 strings that may still be in the database;
    ``normalize_publish_state`` maps them onto the canonical set at every boundary, so a stored
    ``queued`` can never reach a caller.

    ``PARTIALLY_PUBLISHED`` is a **campaign rollup**, never a destination state: one destination is
    either published or it is not, and calling it "partially" would be the lie this project forbids.
    """

    DRAFT = "draft"
    PREPARING = "preparing"
    VALIDATION_PENDING = "validation_pending"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    READY = "ready"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PUBLISHED = "published"
    PARTIALLY_PUBLISHED = "partially_published"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    REQUIRES_USER_ACTION = "requires_user_action"
    UNKNOWN_REMOTE_STATE = "unknown_remote_state"
    NEEDS_RECONCILIATION = "needs_reconciliation"

    # Legacy Phase 2 members, kept so an old row still parses.
    VALIDATION = "validation"
    QUEUED = "queued"
    NEEDS_ATTENTION = "needs_attention"
    MANUAL_PUBLISH_REQUIRED = "manual_publish_required"


LEGACY_PUBLISH_STATES: dict[str, PublishState] = {
    "validation": PublishState.VALIDATION_PENDING,
    "queued": PublishState.READY,
    "needs_attention": PublishState.REQUIRES_USER_ACTION,
    "manual_publish_required": PublishState.REQUIRES_USER_ACTION,
}

# Every state a *destination* can hold.
DESTINATION_PUBLISH_STATES: frozenset[PublishState] = frozenset(
    {
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
        PublishState.FAILED,
        PublishState.CANCELLED,
        PublishState.BLOCKED,
        PublishState.REQUIRES_USER_ACTION,
        PublishState.UNKNOWN_REMOTE_STATE,
        PublishState.NEEDS_RECONCILIATION,
    }
)

# Truly terminal: nothing may leave these, automatically or otherwise.
TERMINAL_PUBLISH_STATES: frozenset[PublishState] = frozenset(
    {PublishState.PUBLISHED, PublishState.CANCELLED}
)
# Not terminal, but only a human may move a destination out of them. `failed` is here rather than
# in TERMINAL because "retry" is a real, explicit action - while a queue sweeper must never
# resurrect a failure on its own.
HUMAN_RETRY_STATES: frozenset[PublishState] = frozenset(
    {
        PublishState.FAILED,
        PublishState.BLOCKED,
        PublishState.REQUIRES_USER_ACTION,
        PublishState.UNKNOWN_REMOTE_STATE,
        PublishState.NEEDS_RECONCILIATION,
    }
)
NEEDS_HUMAN_PUBLISH_STATES: frozenset[PublishState] = frozenset(
    {
        PublishState.BLOCKED,
        PublishState.REQUIRES_USER_ACTION,
        PublishState.UNKNOWN_REMOTE_STATE,
        PublishState.NEEDS_RECONCILIATION,
    }
)

# The one table that says which transitions exist. No service writes `job_state` without it.
PUBLISH_TRANSITIONS: dict[PublishState, frozenset[PublishState]] = {
    PublishState.DRAFT: frozenset(
        {PublishState.PREPARING, PublishState.VALIDATION_PENDING, PublishState.CANCELLED}
    ),
    PublishState.PREPARING: frozenset(
        {PublishState.VALIDATION_PENDING, PublishState.BLOCKED, PublishState.CANCELLED}
    ),
    PublishState.VALIDATION_PENDING: frozenset(
        {
            PublishState.AWAITING_APPROVAL,
            PublishState.BLOCKED,
            PublishState.REQUIRES_USER_ACTION,
            PublishState.CANCELLED,
        }
    ),
    PublishState.AWAITING_APPROVAL: frozenset(
        {PublishState.APPROVED, PublishState.DRAFT, PublishState.BLOCKED, PublishState.CANCELLED}
    ),
    # approved/scheduled -> validation_pending: editing approved content revokes the approval and
    # sends the destination back through validation, so nothing is published on a stale decision.
    PublishState.APPROVED: frozenset(
        {
            PublishState.SCHEDULED,
            PublishState.READY,
            PublishState.VALIDATION_PENDING,
            PublishState.BLOCKED,
            PublishState.CANCELLED,
        }
    ),
    PublishState.SCHEDULED: frozenset(
        {
            PublishState.READY,
            PublishState.REQUIRES_USER_ACTION,
            PublishState.VALIDATION_PENDING,
            PublishState.BLOCKED,
            PublishState.CANCELLED,
        }
    ),
    PublishState.READY: frozenset(
        {
            # An approval can be revoked (or expire) between queueing and uploading; the destination
            # then has to stop and ask rather than upload something nobody still consents to.
            PublishState.REQUIRES_USER_ACTION,
            PublishState.UPLOADING,
            PublishState.BLOCKED,
            PublishState.UNKNOWN_REMOTE_STATE,
            PublishState.CANCELLED,
            PublishState.FAILED,
        }
    ),
    PublishState.UPLOADING: frozenset(
        {
            PublishState.UPLOADED,
            PublishState.PROCESSING,
            PublishState.FAILED,
            PublishState.UNKNOWN_REMOTE_STATE,
            PublishState.REQUIRES_USER_ACTION,
            PublishState.CANCELLED,
        }
    ),
    PublishState.UPLOADED: frozenset(
        {
            PublishState.PROCESSING,
            PublishState.PUBLISHED,
            PublishState.FAILED,
            PublishState.REQUIRES_USER_ACTION,
            PublishState.UNKNOWN_REMOTE_STATE,
        }
    ),
    PublishState.PROCESSING: frozenset(
        {
            PublishState.PUBLISHED,
            PublishState.FAILED,
            PublishState.REQUIRES_USER_ACTION,
            PublishState.UNKNOWN_REMOTE_STATE,
        }
    ),
    PublishState.UNKNOWN_REMOTE_STATE: frozenset(
        {
            PublishState.NEEDS_RECONCILIATION,
            PublishState.PUBLISHED,
            PublishState.READY,
            PublishState.FAILED,
            PublishState.REQUIRES_USER_ACTION,
        }
    ),
    PublishState.NEEDS_RECONCILIATION: frozenset(
        {
            PublishState.PUBLISHED,
            PublishState.READY,
            PublishState.FAILED,
            PublishState.REQUIRES_USER_ACTION,
        }
    ),
    PublishState.BLOCKED: frozenset(
        {
            PublishState.DRAFT,
            PublishState.VALIDATION_PENDING,
            PublishState.READY,
            PublishState.CANCELLED,
        }
    ),
    PublishState.REQUIRES_USER_ACTION: frozenset(
        {
            PublishState.DRAFT,
            PublishState.AWAITING_APPROVAL,
            PublishState.READY,
            PublishState.SCHEDULED,
            PublishState.PUBLISHED,
            PublishState.CANCELLED,
        }
    ),
    PublishState.PUBLISHED: frozenset(),
    PublishState.FAILED: frozenset(
        {
            PublishState.READY,
            # A failed attempt spends the approval it used, so consent for the current content has to
            # be granted again: draft is where that happens.
            PublishState.DRAFT,
            PublishState.CANCELLED,
        }
    ),
    PublishState.CANCELLED: frozenset(),
    PublishState.PARTIALLY_PUBLISHED: frozenset(),
    # Legacy members are readable, never written as a target.
    PublishState.VALIDATION: frozenset({PublishState.VALIDATION_PENDING}),
    PublishState.QUEUED: frozenset({PublishState.READY}),
    PublishState.NEEDS_ATTENTION: frozenset({PublishState.REQUIRES_USER_ACTION}),
    PublishState.MANUAL_PUBLISH_REQUIRED: frozenset({PublishState.REQUIRES_USER_ACTION}),
}


def normalize_publish_state(value: str | PublishState | None) -> PublishState:
    """Map any stored string onto the canonical destination vocabulary."""
    if value is None:
        return PublishState.DRAFT
    text = value.value if isinstance(value, PublishState) else str(value).strip().lower()
    if text in LEGACY_PUBLISH_STATES:
        return LEGACY_PUBLISH_STATES[text]
    try:
        state = PublishState(text)
    except ValueError:
        return PublishState.DRAFT
    return LEGACY_PUBLISH_STATES.get(state.value, state)


def parse_publish_state(value: str | PublishState) -> PublishState:
    """Strict parse for input that came from a caller.

    `normalize_publish_state` is a boundary normaliser for *stored* values and falls back to draft,
    which is right for a legacy row and wrong for a filter: a typo would silently select drafts.
    This one raises, so the API can answer "that is not a state" instead of returning the wrong rows.
    """
    if isinstance(value, PublishState):
        return LEGACY_PUBLISH_STATES.get(value.value, value)
    text = str(value).strip().lower()
    mapped = LEGACY_PUBLISH_STATES.get(text)
    if mapped is not None:
        return mapped
    return PublishState(text)


def can_transition(current: PublishState | str, target: PublishState | str) -> bool:
    """Whether an automatic transition is legal. Services decide how to fail, this only answers."""
    source = normalize_publish_state(current)
    destination = normalize_publish_state(target)
    if source == destination:
        return True
    return destination in PUBLISH_TRANSITIONS.get(source, frozenset())


class PublishMode(StrEnum):
    """What is being published. TikTok's two paths are distinct operations, not a preference."""

    VIDEO = "video"
    REELS = "reels"
    DIRECT_POST = "direct_post"
    INBOX = "inbox"


class PublishErrorCategory(StrEnum):
    """Error classes that decide whether a retry is even thinkable."""

    PERMANENT = "permanent"
    TRANSIENT = "transient"
    AUTHORIZATION = "authorization"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    VALIDATION = "validation"
    MEDIA = "media"
    PROVIDER_PROCESSING = "provider_processing"
    AMBIGUOUS = "ambiguous"


RETRYABLE_ERROR_CATEGORIES: frozenset[PublishErrorCategory] = frozenset(
    {PublishErrorCategory.TRANSIENT, PublishErrorCategory.RATE_LIMIT}
)


class DeliveryAssetState(StrEnum):
    """Lifecycle of a temporary cloud copy used by URL-based platforms."""

    PENDING = "pending"
    UPLOADED = "uploaded"
    VERIFIED = "verified"
    DELETED = "deleted"
    FAILED = "failed"


class ReconciliationDecision(StrEnum):
    NO_REMOTE_OBJECT = "no_remote_object"
    REMOTE_EXISTS = "remote_exists"
    UNDETERMINED = "undetermined"


class ValidationOutcome(StrEnum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    REQUIRES_ATTENTION = "requires_attention"


class TaskState(StrEnum):
    """Task lifecycle.

    `PENDING -> READY -> ASSIGNED -> RUNNING -> REVIEW -> SUCCEEDED` is the normal
    path. `WAITING` means dependencies are satisfied but a budget/slot is not, and
    `BLOCKED` means a dependency failed, so the task can never run.
    """

    DRAFT = "draft"
    PENDING = "pending"
    PLANNED = "planned"
    READY = "ready"
    ASSIGNED = "assigned"
    AWAITING_APPROVAL = "awaiting_approval"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    REVIEW = "review"
    BLOCKED = "blocked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


#: `NEEDS_HUMAN_REVIEW` is terminal for the graph (no agent will run it again) but not a success:
#: `_finalize` counts it as incomplete so the objective reports as partial.
TASK_TERMINAL_STATES = frozenset(
    {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.NEEDS_HUMAN_REVIEW}
)


class ObjectiveState(StrEnum):
    """Lifecycle of one user objective handled by the chief orchestrator."""

    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    REVIEW = "review"
    COMPILING = "compiling"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class ApprovalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class QueueState(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalRequirement(StrEnum):
    NONE = "none"
    ON_WRITE = "on_write"
    ALWAYS_HUMAN = "always_human"


class ToolRisk(StrEnum):
    READ = "read"
    WRITE = "write"
    COMPUTE = "compute"
    EXTERNAL = "external"


class MemoryScope(StrEnum):
    """Memory reach. Narrower scopes are included when a broader one is queried."""

    TASK = "task"
    AGENT = "agent"
    DEPARTMENT = "department"
    PROJECT = "project"
    GLOBAL = "global"
    LONG_TERM = "long_term"


class MetricCompleteness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    SMALL_SAMPLE = "small_sample"
    UNAVAILABLE = "unavailable"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# --------------------------------------------------------------------------- Phase 8: analytics


class AnalyticsObjectType(StrEnum):
    """What a measurement is *about*.

    The provider object type decides which endpoint answers and which metrics may exist, so it is
    stored next to the provider id rather than inferred from a platform name.
    """

    ACCOUNT = "account"
    POST = "post"
    VIDEO = "video"
    REEL = "reel"
    STORY = "story"


class MetricPeriod(StrEnum):
    """The provider's own aggregation window (Graph `period`, IG `period`, TikTok has none)."""

    DAY = "day"
    WEEK = "week"
    DAYS_28 = "days_28"
    MONTH = "month"
    LIFETIME = "lifetime"
    TOTAL_OVER_RANGE = "total_over_range"
    INSTANT = "instant"


class MetricClass(StrEnum):
    """How a value may legally be compared (approval requirement 5).

    ``CUMULATIVE`` counters only grow for the life of one object; ``PERIOD`` values are already an
    aggregation over a fixed window; ``POINT_IN_TIME`` values describe the object at collection
    time. A change is only meaningful between observations of the *same* object and class.
    """

    CUMULATIVE = "cumulative"
    PERIOD = "period"
    POINT_IN_TIME = "point_in_time"
    RATIO = "ratio"
    SERIES = "series"


class MetricValueKind(StrEnum):
    """The provider's payload shape, preserved rather than coerced (approval requirement 4)."""

    SCALAR = "scalar"
    BREAKDOWN = "breakdown"
    SERIES = "series"
    OBJECT = "object"


class MetricAvailability(StrEnum):
    """Why a measurement is or is not a number.

    Only ``OBSERVED`` carries a value. Everything else is a reason the UI must show verbatim
    instead of rendering a zero (the provider itself says an empty result set is not a zero).
    """

    OBSERVED = "observed"
    MISSING = "missing"
    BELOW_THRESHOLD = "below_threshold"
    NOT_SUPPORTED = "not_supported"
    NOT_AUTHORIZED = "not_authorized"
    NOT_COLLECTED = "not_collected"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    EXPIRED = "expired"
    REDACTED_BY_POLICY = "redacted_by_policy"
    UNKNOWN = "unknown"


#: States that may carry a value. Deliberately a set of one, so "add a state" cannot quietly make
#: an unavailable metric storable.
VALUED_AVAILABILITY_STATES: frozenset[MetricAvailability] = frozenset({MetricAvailability.OBSERVED})

LEGACY_AVAILABILITY_STATES: dict[str, MetricAvailability] = {
    # Phase 2 wrote `MetricCompleteness`; treat its words honestly rather than guessing a value.
    "complete": MetricAvailability.OBSERVED,
    "partial": MetricAvailability.OBSERVED,
    "small_sample": MetricAvailability.OBSERVED,
    "unavailable": MetricAvailability.MISSING,
}


def normalize_availability(value: str | MetricAvailability | None) -> MetricAvailability:
    """Total mapping onto the canonical availability vocabulary (never raises)."""
    if value is None:
        return MetricAvailability.UNKNOWN
    text = value.value if isinstance(value, MetricAvailability) else str(value).strip().lower()
    if text in LEGACY_AVAILABILITY_STATES:
        return LEGACY_AVAILABILITY_STATES[text]
    try:
        return MetricAvailability(text)
    except ValueError:
        return MetricAvailability.UNKNOWN


class AnalyticsRunTarget(StrEnum):
    ACCOUNT_SYNC = "account_sync"
    DISCOVERY = "discovery"
    METRICS = "metrics"
    BACKFILL = "backfill"
    MANUAL = "manual"


class AnalyticsRunOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    RATE_LIMITED = "rate_limited"
    NOT_AUTHORIZED = "not_authorized"
    FAILED = "failed"
    SKIPPED = "skipped"


class AnalyticsErrorClass(StrEnum):
    PERMANENT = "permanent"
    TRANSIENT = "transient"
    AUTHORIZATION = "authorization"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    THRESHOLD = "threshold"
    VALIDATION = "validation"
    PROVIDER_PROCESSING = "provider_processing"


class AnalyticsSyncStatus(StrEnum):
    NOT_STARTED = "not_started"
    DISCOVERING = "discovering"
    BACKFILLING = "backfilling"
    CURRENT = "current"
    STALLED = "stalled"
    BLOCKED = "blocked"


class AnalyticsAggregationScope(StrEnum):
    PUBLICATION = "publication"
    ACCOUNT = "account"
    CAMPAIGN = "campaign"
    PLATFORM = "platform"
    CONTENT_GROUP = "content_group"


class AnalyticsWindowKind(StrEnum):
    INITIAL = "initial"
    H24 = "h24"
    H48 = "h48"
    D7 = "d7"
    D28 = "d28"
    LIFETIME = "lifetime"
    CUSTOM = "custom"


#: Target offsets for the performance windows, in hours. ``None`` means "the latest observation".
WINDOW_OFFSET_HOURS: dict[AnalyticsWindowKind, int | None] = {
    AnalyticsWindowKind.INITIAL: 1,
    AnalyticsWindowKind.H24: 24,
    AnalyticsWindowKind.H48: 48,
    AnalyticsWindowKind.D7: 24 * 7,
    AnalyticsWindowKind.D28: 24 * 28,
    AnalyticsWindowKind.LIFETIME: None,
    AnalyticsWindowKind.CUSTOM: None,
}


class AnalyticsAggregationMethod(StrEnum):
    DIRECT = "direct"
    SUM = "sum"
    RATE = "rate"
    MEDIAN = "median"
    DELTA = "delta"
    RATIO = "ratio"


class RecommendationKind(StrEnum):
    TOPIC = "topic"
    FORMAT = "format"
    HOOK = "hook"
    CAPTION = "caption"
    HASHTAG = "hashtag"
    TIMING = "timing"
    CAMPAIGN = "campaign"
    GROWTH = "growth"
    RETENTION = "retention"
    EXPERIMENT = "experiment"


class RecommendationStatus(StrEnum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class AnalyticsReportKind(StrEnum):
    OVERVIEW = "overview"
    PLATFORM_COMPARISON = "platform_comparison"
    ACCOUNT = "account"
    CAMPAIGN = "campaign"
    CONTENT_COMPARISON = "content_comparison"
    RECOMMENDATIONS = "recommendations"
    EXPERIMENT = "experiment"


class AnalyticsReportStatus(StrEnum):
    DRAFT = "draft"
    FINAL = "final"
    ARCHIVED = "archived"


class AnalyticsReportFormat(StrEnum):
    """Report file formats that are genuinely implemented (P8.24).

    Stored in the report's filters, not in a new column: the format is a property of the file that was
    written, and every reader of a report already communicates through ``filters_json``. Declaring a
    format here is a claim that the service can write it, so this enum stays in step with the renderers.
    """

    JSON = "json"
    CSV = "csv"
    PDF = "pdf"

    @property
    def media_type(self) -> str:
        return {
            self.JSON: "application/json",
            self.CSV: "text/csv",
            self.PDF: "application/pdf",
        }[self]

    @property
    def extension(self) -> str:
        return f".{self.value}"


class ConsentStatus(StrEnum):
    """The outcome of an analytics permission-upgrade request (P8.20).

    The vocabulary deliberately separates *what the user can fix* (consent, Page task) from *what
    only the app owner can fix* (developer review) and from *what cannot work at all*
    (unsupported). A single "failed" would tell the user nothing about who must act.
    """

    CONSENT_NOT_NEEDED = "consent_not_needed"
    CONSENT_REQUIRED = "consent_required"
    DEVELOPER_APPROVAL_REQUIRED = "developer_approval_required"
    PAGE_TASK_REQUIRED = "page_task_required"
    UNSUPPORTED = "unsupported"
    # The authorization attempt itself.
    AUTHORIZATION_STARTED = "authorization_started"
    AUTHORIZATION_PENDING = "authorization_pending"
    AUTHORIZATION_DENIED = "authorization_denied"
    INSUFFICIENT_APP_ACCESS = "insufficient_app_access"
    CAPABILITY_AVAILABLE = "capability_available"
    CAPABILITY_STILL_BLOCKED = "capability_still_blocked"
    ACCOUNT_IDENTITY_MISMATCH = "account_identity_mismatch"
    AUTHORIZATION_FAILED = "authorization_failed"
