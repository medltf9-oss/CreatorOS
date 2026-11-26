"""TikTok publisher tests: chunk planning, Direct Post, Inbox, URL expiry and rate limits.

Two things are worth testing more than the happy path: that the `upload_url` (a credential) never
escapes into a result or an error, and that the chunk ladder matches TikTok's documented rules,
because a wrong `total_chunk_count` is rejected at init and looks like a mystery failure.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from creatoros.db.enums import PublishErrorCategory, PublishMode
from creatoros.integrations.social.base import HttpResponse
from creatoros.integrations.social.publishing.publishers import (
    PublishContext,
    PublisherError,
    TikTokInboxPublisher,
    TikTokPublisher,
    plan_chunks,
)

TOKEN = "act.secret-token"
PUBLISH_ID = "v_pub_file~v2-1.100"
POST_ID = "7400000000000000000"
UPLOAD_URL = "https://open-upload.tiktokapis.com/upload/v1/abc?sign=SECRETSIGNATURE"
CREATOR = {
    "creator_username": "creatoros",
    "privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"],
    "comment_disabled": False,
    "duet_disabled": False,
    "stitch_disabled": False,
    "max_video_post_duration_sec": 600,
}
MB = 1024 * 1024


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class Recorder:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    async def request(
        self, method, url, *, params=None, data=None, json=None, headers=None, timeout=20.0, files=None, content=None
    ):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "json": json,
                "content_len": len(content) if content is not None else None,
            }
        )
        if not self.script:
            raise AssertionError(f"unexpected call: {method} {url}")
        answer = self.script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def aclose(self):
        return None


def ok(data: dict) -> HttpResponse:
    return HttpResponse(status=200, headers={}, body={"data": data, "error": {"code": "ok", "message": ""}})


def api_error(code: str, status: int = 200, message: str = "failed") -> HttpResponse:
    return HttpResponse(status=status, headers={}, body={"error": {"code": code, "message": message}})


async def _no_sleep(_seconds: float) -> None:
    return None


def make_context(tmp_path, *, size: int = 2 * MB, **overrides) -> PublishContext:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"t" * size)
    values = {
        "job_id": "01JOBJOBJOBJOBJOBJOBJOBJOB",
        "account_provider_id": "tt-open-id",
        "access_token": TOKEN,
        "mode": PublishMode.DIRECT_POST,
        "caption": "Day one",
        "hashtags": ("fyp",),
        "media_path": str(path),
        "media_bytes": size,
        "api_version": "v2",
        "options": {"privacy_level": "SELF_ONLY"},
    }
    values.update(overrides)
    return PublishContext(**values)


# --------------------------------------------------------------------------- chunk plan


def test_the_chunk_ladder_matches_tiktoks_documented_rules():
    small = plan_chunks(1024)
    assert (small.chunk_size, small.count) == (1024, 1)

    exactly_sixty_four = plan_chunks(64 * MB)
    assert (exactly_sixty_four.chunk_size, exactly_sixty_four.count) == (64 * MB, 1)

    above = plan_chunks(64 * MB + MB)
    assert above.count == 6
    assert above.chunk_size > 10 * MB - 1
    start, end = above.bounds(above.count - 1, 64 * MB + MB)
    assert end == 64 * MB + MB - 1, "the last chunk absorbs the remainder"
    assert start % above.chunk_size == 0


def test_every_chunk_lands_inside_the_file():
    size = 150 * MB
    plan = plan_chunks(size)
    covered = 0
    for index in range(plan.count):
        start, end = plan.bounds(index, size)
        assert 0 <= start <= end < size
        covered += end - start + 1
    assert covered == size


# --------------------------------------------------------------------------- direct post


HAPPY = [
    ok(CREATOR),
    ok({"publish_id": PUBLISH_ID, "upload_url": UPLOAD_URL}),
    HttpResponse(status=206, headers={}, body={}),
    ok({"status": "PROCESSING_DOWNLOAD"}),
    ok({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [POST_ID]}),
]


def test_a_direct_post_runs_creator_info_init_upload_and_status(tmp_path):
    recorder = Recorder(list(HAPPY))
    sealed: list[tuple[str, str]] = []
    publisher = TikTokPublisher(
        recorder, on_upload_session=lambda pid, url: sealed.append((pid, url)), sleep=_no_sleep
    )
    result = run(publisher.publish(make_context(tmp_path)))

    assert result.state == "published"
    assert result.provider_object_id == POST_ID
    assert result.permalink == f"https://www.tiktok.com/@creatoros/video/{POST_ID}"
    assert sealed == [(PUBLISH_ID, UPLOAD_URL)], "the upload url is handed over to be sealed"
    init = recorder.calls[1]["json"]
    assert init["post_mode"] == "DIRECT_POST"
    assert init["post_info"]["privacy_level"] == "SELF_ONLY"
    assert init["post_info"]["title"].startswith("Day one")
    assert init["source_info"]["source"] == "FILE_UPLOAD"
    # The upload URL does carry its own signature - that is how it authorises - but it must not
    # appear in anything the journal, the GUI or a log line receives. That is asserted below.


def test_the_api_calls_use_a_bearer_token_and_the_upload_call_does_not(tmp_path):
    recorder = Recorder(list(HAPPY))
    publisher = TikTokPublisher(recorder, sleep=_no_sleep)
    run(publisher.publish(make_context(tmp_path)))
    assert recorder.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert recorder.calls[1]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    upload = recorder.calls[2]
    assert "Authorization" not in upload["headers"], "the upload URL is authorised by its own query string"
    assert upload["headers"]["Content-Range"] == f"bytes 0-{2 * MB - 1}/{2 * MB}"


def test_a_direct_post_without_a_privacy_level_is_refused_before_init(tmp_path):
    recorder = Recorder([ok(CREATOR)])
    publisher = TikTokPublisher(recorder, sleep=_no_sleep)
    with pytest.raises(PublisherError) as raised:
        run(publisher.publish(make_context(tmp_path, options={})))
    assert raised.value.code == "PRIVACY_LEVEL_REQUIRED"
    assert raised.value.category is PublishErrorCategory.VALIDATION
    assert len(recorder.calls) == 1, "nothing may be created before the request is valid"


def test_a_privacy_level_the_creator_does_not_have_is_refused(tmp_path):
    recorder = Recorder([ok({**CREATOR, "privacy_level_options": ["SELF_ONLY"]})])
    publisher = TikTokPublisher(recorder, sleep=_no_sleep)
    with pytest.raises(PublisherError) as raised:
        run(
            publisher.publish(
                make_context(tmp_path, options={"privacy_level": "PUBLIC_TO_EVERYONE"})
            )
        )
    assert raised.value.code == "PRIVACY_LEVEL_NOT_ALLOWED"
    assert raised.value.detail["allowed"] == ["SELF_ONLY"]


def test_an_expired_upload_url_is_retryable_and_asks_for_a_new_session(tmp_path):
    recorder = Recorder(
        [ok(CREATOR), ok({"publish_id": PUBLISH_ID, "upload_url": UPLOAD_URL}), HttpResponse(status=403, headers={}, body={})]
    )
    publisher = TikTokPublisher(recorder, sleep=_no_sleep)
    with pytest.raises(PublisherError) as raised:
        run(publisher.publish(make_context(tmp_path)))
    assert raised.value.code == "UPLOAD_URL_EXPIRED"
    assert raised.value.category is PublishErrorCategory.TRANSIENT
    assert raised.value.retryable is True


def test_a_rate_limit_from_tiktok_is_retryable(tmp_path):
    recorder = Recorder([api_error("rate_limit_exceeded", status=200)])
    publisher = TikTokPublisher(recorder, sleep=_no_sleep)
    with pytest.raises(PublisherError) as raised:
        run(publisher.publish(make_context(tmp_path)))
    assert raised.value.category is PublishErrorCategory.RATE_LIMIT
    assert raised.value.retryable is True


def test_an_invalid_scope_is_a_permission_failure(tmp_path):
    recorder = Recorder([api_error("scope_not_authorized")])
    publisher = TikTokPublisher(recorder, sleep=_no_sleep)
    with pytest.raises(PublisherError) as raised:
        run(publisher.publish(make_context(tmp_path)))
    assert raised.value.category is PublishErrorCategory.PERMISSION


def test_a_processing_failure_reports_the_reason(tmp_path):
    recorder = Recorder(
        [
            ok(CREATOR),
            ok({"publish_id": PUBLISH_ID, "upload_url": UPLOAD_URL}),
            HttpResponse(status=206, headers={}, body={}),
            ok({"status": "FAILED", "fail_reason": "video_format_check_failed"}),
        ]
    )
    publisher = TikTokPublisher(recorder, sleep=_no_sleep)
    with pytest.raises(PublisherError) as raised:
        run(publisher.publish(make_context(tmp_path)))
    assert raised.value.detail["fail_reason"] == "video_format_check_failed"
    assert raised.value.category is PublishErrorCategory.MEDIA


def test_an_unfinished_upload_stays_retryable_with_the_same_publish_id(tmp_path):
    recorder = Recorder(
        [ok(CREATOR), ok({"publish_id": PUBLISH_ID, "upload_url": UPLOAD_URL})]
        + [HttpResponse(status=206, headers={}, body={})]
        + [ok({"status": "PROCESSING_UPLOAD"}) for _ in range(2)]
    )
    publisher = TikTokPublisher(recorder, sleep=_no_sleep, status_attempts=2, status_delay_s=0)
    with pytest.raises(PublisherError) as raised:
        run(publisher.publish(make_context(tmp_path)))
    assert raised.value.code == "PUBLISH_NOT_FINISHED"
    assert raised.value.provider_object_id == PUBLISH_ID


def test_a_resumed_upload_continues_at_the_next_chunk(tmp_path):
    size = 70 * MB
    plan = plan_chunks(size)
    remaining_chunks = plan.count - 1
    recorder = Recorder(
        [ok(CREATOR)]
        + [HttpResponse(status=206, headers={}, body={}) for _ in range(remaining_chunks)]
        + [ok({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [POST_ID]})]
    )
    publisher = TikTokPublisher(recorder, sleep=_no_sleep)
    ctx = make_context(
        tmp_path,
        size=size,
        upload_url=UPLOAD_URL,
        resume_from={"publish_id": PUBLISH_ID, "upload_chunk_index": "1"},
    )
    result = run(publisher.publish(ctx))
    assert result.state == "published"
    assert len(recorder.calls) == 1 + remaining_chunks + 1, "no new init on a resumed upload"
    first_upload = recorder.calls[1]["headers"]["Content-Range"]
    start = int(first_upload.split(" ")[1].split("-")[0])
    assert start == plan.chunk_size


def test_without_a_sealed_upload_url_a_resumed_job_starts_a_new_session(tmp_path):
    recorder = Recorder(
        [
            ok(CREATOR),
            ok({"publish_id": PUBLISH_ID, "upload_url": UPLOAD_URL}),
            HttpResponse(status=206, headers={}, body={}),
            ok({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [POST_ID]}),
        ]
    )
    publisher = TikTokPublisher(recorder, sleep=_no_sleep)
    # A publish id without its upload URL: the executor could not unseal the sealed session, so the
    # only safe move is a fresh init. Init alone publishes nothing, so this cannot duplicate a post.
    ctx = make_context(tmp_path, resume_from={"publish_id": PUBLISH_ID})
    result = run(publisher.publish(ctx))
    assert result.state == "published"
    assert recorder.calls[1]["json"]["post_mode"] == "DIRECT_POST"
    assert recorder.calls[1]["url"].endswith("/post/publish/video/init/")


def test_a_rejected_upload_url_is_never_used(tmp_path):
    recorder = Recorder([ok(CREATOR), ok({"publish_id": PUBLISH_ID, "upload_url": "http://192.0.2.1/upload"})])
    publisher = TikTokPublisher(recorder, sleep=_no_sleep)
    with pytest.raises(PublisherError) as raised:
        run(publisher.publish(make_context(tmp_path)))
    assert raised.value.code == "UPLOAD_URL_REJECTED"
    assert len(recorder.calls) == 2, "the rejected URL is never called"


def test_status_reports_published_and_failed_differently(tmp_path):
    recorder = Recorder(
        [ok({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [POST_ID]}), ok({"status": "FAILED", "fail_reason": "x"})]
    )
    publisher = TikTokPublisher(recorder, sleep=_no_sleep)
    published = run(publisher.check_status(make_context(tmp_path), PUBLISH_ID))
    assert published.finished is True and published.failed is False
    failed = run(publisher.check_status(make_context(tmp_path), PUBLISH_ID))
    assert failed.failed is True


def test_the_upload_url_and_token_never_reach_an_error_or_a_result(tmp_path):
    recorder = Recorder([ok(CREATOR), ok({"publish_id": PUBLISH_ID, "upload_url": UPLOAD_URL}), HttpResponse(500, {}, {})])
    publisher = TikTokPublisher(recorder, sleep=_no_sleep)
    with pytest.raises(PublisherError) as raised:
        run(publisher.publish(make_context(tmp_path)))
    serialized = json.dumps(raised.value.as_dict())
    assert "SECRETSIGNATURE" not in serialized
    assert TOKEN not in serialized


# --------------------------------------------------------------------------- inbox


def test_an_inbox_upload_sends_no_privacy_level_and_reports_inbox(tmp_path):
    recorder = Recorder(
        [
            ok(CREATOR),
            ok({"publish_id": PUBLISH_ID, "upload_url": UPLOAD_URL}),
            HttpResponse(status=206, headers={}, body={}),
            ok({"status": "SEND_TO_USER_INBOX"}),
        ]
    )
    publisher = TikTokInboxPublisher(recorder, sleep=_no_sleep)
    result = run(publisher.publish(make_context(tmp_path, mode=PublishMode.INBOX, options={})))

    init = recorder.calls[1]["json"]
    assert init["post_mode"] == "MEDIA_UPLOAD"
    assert "privacy_level" not in init["post_info"]
    assert result.state == "inbox"
    assert result.snapshot["in_inbox"] is True
