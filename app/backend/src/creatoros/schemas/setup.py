"""Response models for the first-run setup API.

These describe observation only. Nothing here is a permission: a component that reports `ok` is
present (or, after an explicit verify call, byte-verified), and the acquisition decision still belongs
to the user.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class SetupCheck(BaseModel):
    """One wizard-visible finding, with the state the UI colours it by."""

    id: str
    state: str  # ok | warn | fail | unknown
    detail: str


class SetupModelFact(BaseModel):
    name: str
    size_bytes: int | None = None
    modified_at: str | None = None


class SetupOllama(BaseModel):
    installed: bool
    install_path: str | None = None
    version: str | None = None
    endpoint: str
    reachable: bool
    models: list[SetupModelFact] = Field(default_factory=list)
    error: str | None = None


class SetupFfmpeg(BaseModel):
    present: bool
    ffprobe_present: bool
    path: str | None = None
    version: str | None = None
    error: str | None = None


class SetupDisk(BaseModel):
    root: str
    exists: bool
    writable: bool
    free_bytes: int
    total_bytes: int
    error: str | None = None


class SetupFirstRun(BaseModel):
    completed: bool = False
    completed_at: str | None = None
    # The version the wizard was completed for, not the version currently running.
    version: str | None = None
    skipped_ai: bool = False
    # True when the recorded completion version differs from the running app version. The recorded
    # decision is kept: an upgrade must not discard it and must not reopen the wizard.
    version_changed: bool = False


class SetupVerification(BaseModel):
    """What one recorded verification observed, plus whether that fingerprint still matches.

    `current` compares the recorded size and modified time with the file now; it is not a live
    re-hash, so `current` true means "unchanged since the recorded verification", not "verified now".
    """

    ok: bool
    sha256: str
    size_bytes: int
    path: str
    signer_subject: str | None = None
    timestamped: bool | None = None
    verified_at: str
    current: bool


class PrerequisiteState(BaseModel):
    """Presence-level state. `verified` is only true after an explicit verification call."""

    id: str
    kind: str
    display_name: str
    version: str
    required: bool
    state: str  # ok | verified | missing | unverified | unknown
    verified: bool = False
    verification: SetupVerification | None = None
    detail: str
    local_path: str | None = None
    redistribution_approved: bool = False
    redistribution_detail: str
    download_url: str | None = None
    release_page: str | None = None
    source_url: str | None = None
    consent_required: bool = False
    consent_summary: str = ""
    consent_accepted: bool = False
    consent_recorded_at: str | None = None


class SetupStatus(BaseModel):
    version: str
    data_root: str
    setup_complete: bool
    first_run: SetupFirstRun
    system: dict[str, object]
    disk: SetupDisk
    ollama: SetupOllama
    ffmpeg: SetupFfmpeg
    prerequisites: list[PrerequisiteState] = Field(default_factory=list)
    checks: list[SetupCheck] = Field(default_factory=list)
    manifest_updated: str | None = None
    manifest_error: str | None = None


class PrerequisiteCatalog(BaseModel):
    manifest_schema: str
    updated: str
    source: str
    components: list[PrerequisiteState] = Field(default_factory=list)
    manifest_error: str | None = None


class VerifyRequest(BaseModel):
    path: str | None = Field(default=None, description="Absolute path to the downloaded file.")


class ConsentRequest(BaseModel):
    accepted: bool = Field(description="The user's explicit decision for this component.")


class ConsentResponse(BaseModel):
    component_id: str
    accepted: bool
    recorded_at: str


class VerifyCheck(BaseModel):
    id: str
    state: str
    detail: str


class VerifyResponse(BaseModel):
    component_id: str
    path: str
    ok: bool
    sha256: str | None = None
    size_bytes: int | None = None
    signer_subject: str | None = None
    timestamped: bool | None = None
    verification: SetupVerification | None = None
    checks: list[VerifyCheck] = Field(default_factory=list)


class CompleteRequest(BaseModel):
    skipped_ai: bool = False
    notes: str | None = Field(default=None, max_length=500)
    override_required_checks: bool = False


class CompleteResponse(BaseModel):
    setup_complete: bool
    first_run: SetupFirstRun
    already_completed: bool

