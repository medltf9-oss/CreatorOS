"""Media delivery tests: signing, upload verification, configuration and retention.

The SigV4 implementation is pinned to the published AWS test vector, because "it looked right" is not
a property of a signature. Everything else is tested the way it fails in practice: a bucket that
answers 403, an upload whose stored object is a different size, an interrupted multipart upload, and
a not-configured installation where the honest answer is a stated reason, not a crash.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from publishing_fixtures import make_account, make_asset, make_composer
from social_fixtures import InMemoryVault

from creatoros.db.enums import DeliveryAssetState, Platform, PublishState
from creatoros.db.models import AppSetting, PublishDelivery
from creatoros.integrations.social.publishing.media_delivery import (
    DeliveryConfig,
    DeliveryError,
    S3Credentials,
    S3DeliveryProvider,
    provider_from_config,
    unconfigured,
)
from creatoros.integrations.social.publishing.media_delivery.aws_sigv4 import (
    EMPTY_SHA256,
    presign_url,
    sign_headers,
)
from creatoros.services.publishing.delivery import CREDENTIAL_PURPOSE, SETTING_KEY, DeliveryService
from creatoros.services.publishing.errors import PublishingError

FIXED_NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- signature


def test_sigv4_matches_the_published_aws_test_vector():
    """AWS `get-vanilla`: the specification's own expected signature, byte for byte."""
    headers = sign_headers(
        method="GET",
        host="example.amazonaws.com",
        path="/",
        query=None,
        headers={},
        payload_hash=EMPTY_SHA256,
        access_key="AKIDEXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
        service="service",
        now=datetime(2015, 8, 30, 12, 36, 0, tzinfo=UTC),
        include_content_sha256=False,
    )
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, "
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


def test_presigned_urls_carry_a_signature_and_an_expiry():
    url = presign_url(
        scheme="https",
        host="bucket.s3.eu-west-3.amazonaws.com",
        path="/creatoros/publish/ab/abc.mp4",
        access_key="AKIDEXAMPLE",
        secret_key="secret",
        region="eu-west-3",
        expires_s=600,
        now=FIXED_NOW,
    )
    assert url.startswith("https://bucket.s3.eu-west-3.amazonaws.com/creatoros/publish/ab/abc.mp4?")
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
    assert "X-Amz-Expires=600" in url
    assert "X-Amz-Signature=" in url
    assert "AKIDEXAMPLE%2F" in url


def test_a_signed_url_is_never_printed_by_accident():
    from creatoros.integrations.social.publishing.media_delivery import SignedUrl

    url = SignedUrl(url="https://x/y?X-Amz-Signature=deadbeef", expires_at=FIXED_NOW, object_key="k")
    assert "deadbeef" not in repr(url)
    assert "deadbeef" in url.url


# --------------------------------------------------------------------------- configuration


def test_a_config_rejects_values_that_would_fail_later():
    with pytest.raises(DeliveryError) as raised:
        DeliveryConfig(bucket="Not A Bucket")
    assert raised.value.code == "DELIVERY_BAD_CONFIG"

    with pytest.raises(DeliveryError):
        DeliveryConfig(bucket="fine-bucket", region="paris")

    with pytest.raises(DeliveryError):
        DeliveryConfig(bucket="fine-bucket", endpoint_url="http://localhost:9000")

    with pytest.raises(DeliveryError):
        DeliveryConfig(bucket="fine-bucket", part_size=1024)


def test_object_keys_are_content_addressed_and_never_derived_from_filenames():
    config = DeliveryConfig(bucket="creatoros-media", prefix="creatoros/publish")
    digest = "ab" * 32
    key = config.object_key(digest)
    assert key == f"creatoros/publish/ab/{digest}.mp4"
    assert config.object_key(digest) == key
    with pytest.raises(DeliveryError):
        config.object_key("not-a-digest")


def test_the_unconfigured_provider_states_the_reason_instead_of_pretending():
    backend = unconfigured(["bucket configuration"])
    health = run(backend.health())
    assert health.available is False
    assert health.reason_code == "DELIVERY_NOT_CONFIGURED"
    assert "bucket" in health.detail.lower()
    with pytest.raises(DeliveryError) as raised:
        run(backend.upload("x.mp4", object_key="k", sha256="a" * 64, size_bytes=1))
    assert raised.value.code == "DELIVERY_NOT_CONFIGURED"
    assert raised.value.retryable is False
    assert not isinstance(backend, S3DeliveryProvider)


def test_the_factory_refuses_an_unknown_provider():
    with pytest.raises(DeliveryError) as raised:
        provider_from_config({"provider": "ftp", "bucket": "creatoros-media"}, S3Credentials("a", "b"))
    assert raised.value.code == "DELIVERY_BAD_CONFIG"

    with pytest.raises(DeliveryError):
        provider_from_config({"bucket": "creatoros-media"}, None)


# --------------------------------------------------------------------------- uploads


#: One loop for the whole module: an httpx client created in one `asyncio.run` call cannot be used
#: in the next one, and these tests build clients outside the coroutine on purpose.
_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


def _provider(handler, *, config: DeliveryConfig | None = None, clock=FIXED_NOW) -> S3DeliveryProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return S3DeliveryProvider(
        config or DeliveryConfig(bucket="creatoros-media", region="eu-west-3"),
        S3Credentials("AKIDEXAMPLE", "secret"),
        client=client,
        clock=lambda: clock,
    )


def _video(tmp_path, size: int = 1024) -> tuple[str, str, int]:
    path = tmp_path / "clip.mp4"
    payload = b"v" * size
    path.write_bytes(payload)
    return str(path), hashlib.sha256(payload).hexdigest(), size


def test_a_small_upload_is_signed_verified_and_carries_its_checksum(tmp_path):
    path, digest, size = _video(tmp_path)
    seen: list[httpx.Request] = []
    uploaded = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal uploaded
        seen.append(request)
        if request.method == "HEAD":
            if not uploaded:
                return httpx.Response(404)
            return httpx.Response(200, headers={"content-length": str(size), "x-amz-meta-sha256": digest})
        uploaded = True
        return httpx.Response(200, headers={"etag": '"abc"'})

    provider = _provider(handler)
    receipt = run(
        provider.upload(path, object_key=f"k/{digest}.mp4", sha256=digest, size_bytes=size)
    )

    put = next(request for request in seen if request.method == "PUT")
    assert put.headers["x-amz-meta-sha256"] == digest
    assert put.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "x-amz-content-sha256" in {key.lower() for key in put.headers}
    assert receipt.bytes == size and receipt.sha256 == digest


def test_an_upload_whose_stored_object_differs_in_size_is_refused(tmp_path):
    path, digest, size = _video(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(size - 1), "x-amz-meta-sha256": digest})
        return httpx.Response(200, headers={"etag": '"abc"'})

    provider = _provider(handler)
    with pytest.raises(DeliveryError) as raised:
        run(provider.upload(path, object_key="k", sha256=digest, size_bytes=size))
    assert raised.value.code == "DELIVERY_VERIFY_FAILED"
    assert raised.value.retryable is False


def test_an_upload_whose_stored_checksum_differs_is_refused(tmp_path):
    path, digest, size = _video(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(size), "x-amz-meta-sha256": "b" * 64})
        return httpx.Response(200, headers={"etag": '"abc"'})

    provider = _provider(handler)
    with pytest.raises(DeliveryError) as raised:
        run(provider.upload(path, object_key="k", sha256=digest, size_bytes=size))
    assert raised.value.code == "DELIVERY_VERIFY_FAILED"


def test_an_object_that_is_already_there_is_not_uploaded_again(tmp_path):
    path, digest, size = _video(tmp_path)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, headers={"content-length": str(size), "x-amz-meta-sha256": digest})

    provider = _provider(handler)
    receipt = run(provider.upload(path, object_key="k", sha256=digest, size_bytes=size))
    assert calls == ["HEAD"]
    assert receipt.parts == 0


def test_a_large_upload_uses_multipart_and_aborts_when_a_part_fails(tmp_path):
    path, digest, size = _video(tmp_path, size=5 * 1024 * 1024 + 10)
    config = DeliveryConfig(bucket="creatoros-media", region="eu-west-3", part_size=5 * 1024 * 1024)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "HEAD":
            return httpx.Response(404)
        if request.method == "POST" and "uploads" in request.url.params:
            return httpx.Response(
                200, text="<InitiateMultipartUploadResult><UploadId>UPLOAD1</UploadId></InitiateMultipartUploadResult>"
            )
        if request.method == "PUT" and "partNumber" in request.url.params:
            return httpx.Response(500)
        return httpx.Response(200, headers={"etag": '"x"'})

    provider = _provider(handler, config=config)
    with pytest.raises(DeliveryError):
        run(provider.upload(path, object_key="k", sha256=digest, size_bytes=size))
    # The failed part must be followed by an abort of the same upload id.
    aborts = [
        request
        for request in calls
        if request.method == "DELETE" and request.url.params.get("uploadId") == "UPLOAD1"
    ]
    assert aborts, "an interrupted multipart upload must be aborted, not left open"


def test_multipart_completes_with_the_parts_it_uploaded(tmp_path):
    part_size = 5 * 1024 * 1024
    path, digest, size = _video(tmp_path, size=part_size + 10)
    config = DeliveryConfig(bucket="creatoros-media", region="eu-west-3", part_size=part_size)
    completed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            # Absent before the multipart upload, present and correct afterwards.
            if not completed:
                return httpx.Response(404)
            return httpx.Response(200, headers={"content-length": str(size), "x-amz-meta-sha256": digest})
        if request.method == "POST" and "uploads" in request.url.params:
            return httpx.Response(200, text="<InitiateMultipartUploadResult><UploadId>U2</UploadId></InitiateMultipartUploadResult>")
        if request.method == "PUT" and "partNumber" in request.url.params:
            number = request.url.params["partNumber"]
            return httpx.Response(200, headers={"etag": f'"etag-{number}"'})
        if request.method == "POST" and "uploadId" in request.url.params:
            completed.append(request.content.decode("utf-8"))
            return httpx.Response(200, headers={"etag": '"final"'})
        return httpx.Response(200, headers={"content-length": str(size), "x-amz-meta-sha256": digest})

    provider = _provider(handler, config=config)
    receipt = run(provider.upload(path, object_key="k", sha256=digest, size_bytes=size))
    assert receipt.parts == 2
    assert len(completed) == 1
    assert "<PartNumber>1</PartNumber>" in completed[0]
    assert "<PartNumber>2</PartNumber>" in completed[0]
    assert '"etag-2"' in completed[0]


def test_a_forbidden_bucket_is_reported_as_forbidden_not_as_a_generic_failure(tmp_path):
    path, digest, size = _video(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="<Error><Code>AccessDenied</Code></Error>")

    provider = _provider(handler)
    with pytest.raises(DeliveryError) as raised:
        run(provider.upload(path, object_key="k", sha256=digest, size_bytes=size))
    assert raised.value.code == "DELIVERY_FORBIDDEN"
    assert raised.value.retryable is False


def test_health_distinguishes_bad_credentials_from_an_unreachable_host():
    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    forbidden_health = run(_provider(forbidden).health())
    assert forbidden_health.available is False
    assert forbidden_health.reason_code == "DELIVERY_FORBIDDEN"

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    broken = run(_provider(boom).health())
    assert broken.available is False
    assert broken.reason_code == "DELIVERY_UNAVAILABLE"

    def healthy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    assert run(_provider(healthy).health()).available is True


def test_signing_can_be_pointed_at_a_cdn_instead_of_presigning():
    config = DeliveryConfig(bucket="creatoros-media", public_base_url="https://cdn.example.com")
    provider = _provider(lambda request: httpx.Response(200), config=config)
    url = run(provider.sign_get("creatoros/publish/ab/x.mp4"))
    assert url.url == "https://cdn.example.com/creatoros/publish/ab/x.mp4"
    assert "X-Amz-Signature" not in url.url


def test_delete_reports_whether_the_object_existed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204 if "present" in str(request.url) else 404)

    provider = _provider(handler)
    assert run(provider.delete("present")) is True
    assert run(provider.delete("absent")) is False


# --------------------------------------------------------------------------- service


def _delivery_service(session, vault=None) -> DeliveryService:
    return DeliveryService(session, vault or InMemoryVault(), clock=lambda: FIXED_NOW)


def test_status_explains_what_is_missing_before_anything_is_configured(session):
    service = _delivery_service(session)
    status = run(service.status())
    assert status["configured"] is False
    assert status["missing"] == ["bucket configuration", "credentials"]
    assert status["health"]["reason_code"] == "DELIVERY_NOT_CONFIGURED"
    assert "Instagram" in status["health"]["detail"]


def test_configuring_stores_the_secret_in_the_vault_and_never_the_key(session):
    vault = InMemoryVault()
    service = _delivery_service(session, vault)
    status = run(
        service.configure(
            fields={"bucket": "creatoros-media", "region": "eu-west-3", "prefix": "creatoros/publish"},
            access_key_id="AKIAEXAMPLE",
            secret_access_key="very-secret",
        )
    )
    assert status["configured"] is True
    assert status["bucket"] == "creatoros-media"

    stored = session.get(AppSetting, SETTING_KEY)
    serialized = json.dumps(stored.value_json)
    assert "very-secret" not in serialized
    assert "AKIAEXAMPLE" not in serialized
    ref_id = stored.value_json["secret_ref_id"]
    assert vault.records[ref_id]["purpose"] == CREDENTIAL_PURPOSE
    assert vault.calls[-1][2] == CREDENTIAL_PURPOSE


def test_configuring_without_credentials_is_refused_the_first_time(session):
    service = _delivery_service(session)
    with pytest.raises(PublishingError) as raised:
        run(service.configure(fields={"bucket": "delivery-bucket"}))
    assert raised.value.code == "DELIVERY_CREDENTIALS_REQUIRED"


def test_a_second_configuration_rotation_keeps_one_credential(session):
    vault = InMemoryVault()
    service = _delivery_service(session, vault)
    run(
        service.configure(
            fields={"bucket": "delivery-bucket"}, access_key_id="AKIA1", secret_access_key="s1"
        )
    )
    first_ref = session.get(AppSetting, SETTING_KEY).value_json["secret_ref_id"]
    run(service.configure(fields={"bucket": "delivery-bucket-2"}, access_key_id="AKIA2", secret_access_key="s2"))
    second_ref = session.get(AppSetting, SETTING_KEY).value_json["secret_ref_id"]
    assert first_ref == second_ref, "rotating keeps the same vault record; it does not orphan one"
    assert any(call[0] == "rotate" for call in vault.calls)


def test_clearing_the_configuration_removes_both_halves(session):
    vault = InMemoryVault()
    service = _delivery_service(session, vault)
    run(service.configure(fields={"bucket": "delivery-bucket"}, access_key_id="AKIA1", secret_access_key="s1"))
    run(service.clear())
    assert session.get(AppSetting, SETTING_KEY) is None
    assert run(service.status())["configured"] is False


def test_the_service_uses_the_provider_from_configuration(session, tmp_path):
    vault = InMemoryVault()
    path, digest, size = _video(tmp_path)
    uploaded = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal uploaded
        if request.method == "HEAD":
            if not uploaded:
                return httpx.Response(404)
            return httpx.Response(200, headers={"content-length": str(size), "x-amz-meta-sha256": digest})
        uploaded = True
        return httpx.Response(200, headers={"etag": '"abc"'})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = DeliveryService(session, vault, client=client, clock=lambda: FIXED_NOW)
    run(service.configure(fields={"bucket": "delivery-bucket"}, access_key_id="AKIA1", secret_access_key="s1"))

    receipt, object_key = run(service.ensure_uploaded(path, sha256=digest, size_bytes=size))
    assert object_key == f"creatoros/publish/{digest[:2]}/{digest}.mp4"
    assert receipt.bytes == size

    url = run(service.public_url(object_key))
    assert url.object_key == object_key
    assert url.expires_at == FIXED_NOW + timedelta(seconds=DeliveryConfig(bucket="delivery-bucket").url_ttl_s)


def test_an_unreadable_credential_degrades_to_unconfigured_instead_of_crashing(session, tmp_path):
    vault = InMemoryVault()
    service = _delivery_service(session, vault)
    run(service.configure(fields={"bucket": "delivery-bucket"}, access_key_id="AKIA1", secret_access_key="s1"))
    ref_id = session.get(AppSetting, SETTING_KEY).value_json["secret_ref_id"]
    vault.fail_get.add(ref_id)

    status = run(service.status())
    assert status["configured"] is True  # the rows exist...
    assert status["health"]["available"] is False  # ...but the credential cannot be read
    assert "vault" in status["health"]["detail"].lower()


# --------------------------------------------------------------------------- ledger


def _job_with_delivery(session, *, sha256: str = "a" * 64):
    composer = make_composer(session)
    asset = make_asset(session, sha256=sha256)
    account = make_account(
        session, platform=Platform.INSTAGRAM, provider_account_id=f"ig-{sha256[:8]}"
    )
    project = composer.create_intent(title="Reel", source_asset_id=asset.id)
    destination = composer.add_destination(
        project.id, platform="instagram", account_id=account.id, caption="hello"
    )
    return destination.job


def test_recording_a_delivery_twice_updates_one_row_and_marks_it_verified(session):
    job = _job_with_delivery(session)
    service = _delivery_service(session)
    from creatoros.integrations.social.publishing.media_delivery import DeliveryReceipt, SignedUrl

    receipt = DeliveryReceipt(
        bucket="b1", object_key="creatoros/publish/ab/x.mp4", bytes=10, sha256="a" * 64, etag='"e"', uploaded_at=FIXED_NOW
    )
    url = SignedUrl(url="https://x/y", expires_at=FIXED_NOW + timedelta(minutes=15), object_key=receipt.object_key)
    first = service.record_delivery(job, receipt=receipt, url=url)
    second = service.record_delivery(job, receipt=receipt, url=url)
    assert first.id == second.id
    assert second.state is DeliveryAssetState.VERIFIED
    assert session.query(PublishDelivery).count() == 1


def test_releasing_an_object_marks_every_row_that_used_it(session):
    job = _job_with_delivery(session)
    from creatoros.integrations.social.publishing.media_delivery import DeliveryReceipt

    receipt = DeliveryReceipt(
        bucket="b1", object_key="creatoros/publish/ab/x.mp4", bytes=10, sha256="a" * 64, etag='"e"', uploaded_at=FIXED_NOW
    )
    service = _delivery_service(session)
    service.record_delivery(job, receipt=receipt, url=None)

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200)

    run(service.configure(fields={"bucket": "delivery-bucket"}, access_key_id="A", secret_access_key="s"))
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    released = run(service.release(receipt.object_key, reason="published"))
    assert released == 1
    assert calls == ["DELETE"]
    row = session.query(PublishDelivery).one()
    assert row.state is DeliveryAssetState.DELETED
    assert row.deleted_at is not None


def test_cleanup_only_deletes_objects_whose_destinations_are_finished(session):
    finished = _job_with_delivery(session, sha256="a" * 64)
    waiting = _job_with_delivery(session, sha256="b" * 64)
    from creatoros.integrations.social.publishing.media_delivery import DeliveryReceipt

    def receipt(key: str) -> DeliveryReceipt:
        return DeliveryReceipt(
            bucket="b1", object_key=key, bytes=10, sha256="a" * 64, etag='"e"', uploaded_at=FIXED_NOW
        )

    service = _delivery_service(session)
    service.record_delivery(finished, receipt=receipt("k/finished.mp4"), url=None)
    service.record_delivery(waiting, receipt=receipt("k/waiting.mp4"), url=None)
    finished.job_state = PublishState.PUBLISHED
    session.flush()

    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted.append(str(request.url))
            return httpx.Response(204)
        return httpx.Response(200)

    run(service.configure(fields={"bucket": "delivery-bucket"}, access_key_id="A", secret_access_key="s"))
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    # Rows are younger than the retention window, so nothing goes yet.
    for row in session.query(PublishDelivery).all():
        row.created_at = FIXED_NOW - timedelta(hours=1)
    session.flush()
    assert run(service.cleanup(retention_hours=24)) == {"objects": 0, "rows_released": 0, "failed": 0}

    # Pretend the rows are old: the finished one goes, the waiting one stays.
    for row in session.query(PublishDelivery).all():
        row.created_at = FIXED_NOW - timedelta(days=3)
    session.flush()
    result = run(service.cleanup(retention_hours=24))
    assert result["objects"] == 1
    assert any("finished.mp4" in url for url in deleted)
    assert not any("waiting.mp4" in url for url in deleted)
