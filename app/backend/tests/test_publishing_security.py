"""Publishing security tests: SSRF, upload-URL sealing, redaction and a leak scan.

The publishing engine is the first part of this project that sends bytes to a third party, so the
tests here are about what must *not* happen:

* no URL the application did not choose may be fetched (Instagram's media URL, TikTok's upload URL);
* an upload URL is a secret: it goes to the vault, never to the journal, an error message or a log;
* no API response and no agent tool can contain a token, an upload URL or a signed URL;
* no schema accepts a credential in the first place.
"""
from __future__ import annotations

import inspect
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from publishing_fixtures import bind_token, make_account, make_asset, make_composer
from social_fixtures import InMemoryVault

from creatoros.db.models import PublishJob, QueueJob, SecretRef
from creatoros.integrations.social.publishing.media_delivery.base import SignedUrl
from creatoros.integrations.social.publishing.publishers.base import PublishContext
from creatoros.integrations.social.publishing.security import (
    PROVIDER_HOSTS,
    host_of,
    validate_provider_url,
)
from creatoros.schemas import publishing as publishing_schemas
from creatoros.services.publishing.executor import UPLOAD_URL_PURPOSE, PublishExecutor
from creatoros.services.publishing.history import REDACTED_KEYS, redact

SECRET_URL = "https://upload.tiktokapis.com/video/?upload_id=super-secret-id&sig=NEEDLE"


# ------------------------------------------------------------------------- SSRF
@pytest.mark.parametrize(
    "url",
    [
        "http://graph.facebook.com/v23.0/me",
        "https://user:pass@graph.facebook.com/v23.0/me",
        "https://graph.facebook.com:8443/v23.0/me",
        "https://127.0.0.1/v23.0/me",
        "https://[::1]/v23.0/me",
        "https://localhost/upload",
        "https://metadata.google.internal/latest",
        "https://169.254.169.254/latest",
        "https://192.0.2.5/upload",
        "https://evil.example.com/upload",
        "",  # empty
        None,
    ],
)
def test_urls_the_application_does_not_control_are_refused(url):
    verdict = validate_provider_url(url)
    assert verdict.allowed is False
    assert verdict.reason


@pytest.mark.parametrize(
    "url",
    [
        "https://graph.facebook.com/v23.0/me/videos",
        "https://graph.instagram.com/v23.0/media",
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        "https://upload.tiktokapis.com/video/",
        "https://cdn-us.tiktokcdn.com/obj/x",
    ],
)
def test_the_providers_own_hosts_are_allowed(url):
    assert validate_provider_url(url).allowed is True


def test_a_configured_bucket_host_is_allowed_only_when_it_is_passed_in():
    url = "https://media.example.com/objects/ab/x.mp4"
    assert validate_provider_url(url).allowed is False
    assert validate_provider_url(url, extra_hosts=("media.example.com",)).allowed is True


def test_a_subdomain_of_an_allowed_host_counts_and_a_lookalike_does_not():
    assert validate_provider_url("https://graph.facebook.com/v23.0/me").allowed is True
    assert validate_provider_url("https://evilgraph.facebook.com/x").allowed is False
    assert validate_provider_url("https://graph.facebook.com.evil.example/x").allowed is False


def test_the_provider_host_list_contains_no_wildcard_or_localhost():
    for host in PROVIDER_HOSTS:
        assert "*" not in host
        assert "localhost" not in host


def test_host_of_never_raises_on_garbage():
    assert host_of("not a url") is None
    assert host_of(None) is None
    assert host_of("https://Graph.Facebook.com/x") == "graph.facebook.com"


# ------------------------------------------------------- upload URL is a secret
def make_destination(session, vault, *, platform="tiktok"):
    composer = make_composer(session)
    asset = make_asset(session)
    account = make_account(session, platform=platform)
    bind_token(session, account, vault)
    project = composer.create_intent(title="TikTok run", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id, platform=platform, account_id=account.id, caption="Morning routine"
    )
    return destination.job


def test_an_upload_url_is_sealed_in_the_vault_and_never_journaled(session):
    vault = InMemoryVault()
    job = make_destination(session, vault)
    executor = PublishExecutor(session, vault=vault, publishers={})

    executor._seal_upload_url(job, "publish-1", SECRET_URL)
    assert job.provider_upload_session_id == "publish-1"
    assert "NEEDLE" not in json.dumps(job.remote_status_json)
    assert SECRET_URL not in json.dumps(_job_snapshot(job))

    import asyncio

    asyncio.run(executor._seal_pending_upload())
    sealed = session.scalars(sa.select(SecretRef).where(SecretRef.purpose == UPLOAD_URL_PURPOSE)).all()
    assert len(sealed) == 1
    assert "NEEDLE" in json.dumps(vault.records, default=str)  # in the vault: where it belongs
    assert all("NEEDLE" not in json.dumps(_job_snapshot(job)) for _ in [0])


def test_an_upload_url_is_not_written_to_the_queue_payload(session):
    vault = InMemoryVault()
    job = make_destination(session, vault)
    executor = PublishExecutor(session, vault=vault, publishers={})
    executor._seal_upload_url(job, "publish-1", SECRET_URL)
    executor.enqueue(job)
    rows = session.scalars(sa.select(QueueJob)).all()
    assert rows, "the job should have been queued"
    for row in rows:
        assert "NEEDLE" not in json.dumps(row.payload_json)


def test_an_upload_url_never_appears_in_repr(session):
    context = PublishContext(
        job_id="job-1",
        account_provider_id="acct-1",
        access_token="token-NEEDLE",
        mode="direct_post",  # type: ignore[arg-type]
        upload_url=SECRET_URL,
    )
    # `repr=False` means the field is not printed at all, which is stronger than masking it.
    text = repr(context)
    assert "NEEDLE" not in text
    assert "upload_url" not in text
    assert context.upload_url == SECRET_URL


def _job_snapshot(job: PublishJob) -> dict:
    return {
        "state": str(job.job_state),
        "reason": job.state_reason,
        "remote_post_id": job.remote_post_id,
        "provider_media_id": job.provider_media_id,
        "upload_session": job.provider_upload_session_id,
        "remote_status": job.remote_status_json,
        "error": job.last_error_detail,
        "metadata": job.metadata_json,
        "options": job.options_json,
    }


# ------------------------------------------------------------------- redaction
def test_every_credential_shaped_key_is_removed_at_any_depth():
    payload = {
        "access_token": "x",
        "token": "x",
        "secret": "x",
        "client_secret": "x",
        "upload_url": "x",
        "signed_url": "x",
        "url": "x",
        "presigned": "x",
        "authorization": "x",
        "password": "x",
        "refresh_token": "x",
        "code_verifier": "x",
        "keep": {"nested": [{"signed_url": "x", "value": 1}]},
    }
    cleaned = redact(payload)
    assert cleaned == {"keep": {"nested": [{"value": 1}]}}
    for key in REDACTED_KEYS:
        assert key not in cleaned


def test_a_signed_url_cannot_be_printed_by_accident():
    signed = SignedUrl(url=SECRET_URL, expires_at=datetime.now(UTC), object_key="objects/ab/x.mp4")
    assert "NEEDLE" not in repr(signed)
    assert signed.url.endswith("NEEDLE"), "the value is still available to the one caller that needs it"


# --------------------------------------------------------------- request models
def test_no_request_schema_accepts_a_credential():
    forbidden = {
        "token",
        "access_token",
        "secret",
        "secret_key",
        "secret_access_key",
        "access_key_id",
        "password",
        "upload_url",
        "signed_url",
    }
    for name, model in inspect.getmembers(publishing_schemas, inspect.isclass):
        if not issubclass(model, publishing_schemas.BaseModel):
            continue
        fields = set(model.model_fields)
        assert not (fields & forbidden), f"{name} accepts {sorted(fields & forbidden)}"


def test_no_request_schema_accepts_a_local_path():
    forbidden = {"media_path", "source_path", "output_path", "path", "file", "filename"}
    for name, model in inspect.getmembers(publishing_schemas, inspect.isclass):
        if not issubclass(model, publishing_schemas.BaseModel):
            continue
        assert not (set(model.model_fields) & forbidden), name


# -------------------------------------------------------------------- leak scan
def test_the_publishing_sources_do_not_log_or_store_a_token():
    """A blunt scan: no module under services/publishing may f-string a token into a message."""
    root = Path(__file__).resolve().parents[1] / "src" / "creatoros"
    suspects = list((root / "services" / "publishing").rglob("*.py")) + list(
        (root / "integrations" / "social" / "publishing").rglob("*.py")
    )
    patterns = ("log.info(f\"", "log.warning(f\"", "log.error(f\"", "print(")
    logging_a_secret = re.compile(r"log\.(debug|info|warning|error|exception)\([^)]*(token|upload_url|signed_url)")
    for path in suspects:
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            assert pattern not in text, f"{path.name} uses {pattern}; a token could end up in the log"
        # An `Authorization: Bearer {token}` header is required and fine; putting the token in a log
        # call is not, and that is exactly what this catches.
        assert logging_a_secret.search(text) is None, f"{path.name} logs a credential"


def test_the_executor_is_the_only_module_that_calls_a_publisher():
    """Services outside the executor cannot reach a provider by importing a publisher."""
    root = Path(__file__).resolve().parents[1] / "src" / "creatoros"
    for path in (root / "services").rglob("*.py"):
        if path.name in {"executor.py", "reconciliation.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        for publisher in ("FacebookVideoPublisher", "InstagramReelsPublisher", "TikTokPublisher"):
            assert publisher not in text, f"{path.name} imports {publisher}"
