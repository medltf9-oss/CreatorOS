"""Facebook Page video publisher tests.

The publisher is tested against a recorded transport, not against Facebook: the point is the
protocol it speaks (three phases, offsets, multipart chunk part, status polling) and the way it
classifies failures. A failure during `finish` must never look retryable, because Meta may already
have the video.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from creatoros.db.enums import PublishErrorCategory, PublishMode
from creatoros.integrations.social.base import HttpResponse
from creatoros.integrations.social.publishing.publishers import (
    FacebookVideoPublisher,
    PublishContext,
    PublisherError,
)

PAGE_ID = "1010101010"
TOKEN = "EAAG-secret-page-token"
VIDEO_ID = "555000111"


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class Recorder:
    """A transport that answers from a script and remembers what it was asked."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    async def request(
        self, method, url, *, params=None, data=None, json=None, headers=None, timeout=20.0, files=None
    ):
        record = {
            "method": method,
            "url": url,
            "params": params,
            "data": data,
            "files": {key: len(value[1]) for key, value in (files or {}).items()},
            "timeout": timeout,
        }
        self.calls.append(record)
        if not self.script:
            raise AssertionError(f"unexpected call: {method} {url}")
        answer = self.script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def aclose(self):
        return None


def ok(body: dict, status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, headers={}, body=body)


def err(status: int, code: int, message: str = "boom", subcode: int | None = None) -> HttpResponse:
    error = {"message": message, "code": code}
    if subcode is not None:
        error["error_subcode"] = subcode
    return HttpResponse(status=status, headers={}, body={"error": error})


def make_context(tmp_path, *, size: int = 9 * 1024 * 1024, **overrides) -> PublishContext:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x" * size)
    values = {
        "job_id": "01JOBJOBJOBJOBJOBJOBJOBJOB",
        "account_provider_id": PAGE_ID,
        "access_token": TOKEN,
        "mode": PublishMode.VIDEO,
        "caption": "Coffee, then the gym.",
        "title": "Morning routine",
        "hashtags": ("morning", "gym"),
        "media_path": str(path),
        "media_bytes": size,
        "api_version": "v23.0",
    }
    values.update(overrides)
    return PublishContext(**values)


CHUNK = 4 * 1024 * 1024


def test_a_successful_publish_runs_the_three_phases_in_order(tmp_path):
    size = 9 * 1024 * 1024
    ctx = make_context(tmp_path, size=size)
    recorder = Recorder(
        [
            ok({"video_id": VIDEO_ID, "upload_session_id": "SESSION1", "start_offset": 0, "end_offset": CHUNK}),
            ok({"start_offset": CHUNK, "end_offset": 2 * CHUNK}),
            ok({"start_offset": 2 * CHUNK, "end_offset": size}),
            ok({"start_offset": size, "end_offset": size}),
            ok({"success": True}),
        ]
    )
    publisher = FacebookVideoPublisher(recorder, chunk_size=CHUNK)
    progress: list[tuple[int, int]] = []
    result = run(publisher.publish(ctx, on_progress=lambda sent, total: progress.append((sent, total))))

    phases = [call["data"]["upload_phase"] for call in recorder.calls]
    assert phases == ["start", "transfer", "transfer", "transfer", "finish"]
    assert result.provider_object_id == VIDEO_ID
    assert result.state == "processing"
    assert progress[-1] == (size, size), "progress must reach the file size"
    assert all(call["url"].startswith(f"https://graph.facebook.com/v23.0/{PAGE_ID}/videos") for call in recorder.calls)


def test_the_transfer_sends_the_chunk_as_multipart_and_advances_the_offset(tmp_path):
    ctx = make_context(tmp_path, size=CHUNK + 10)
    recorder = Recorder(
        [
            ok({"video_id": VIDEO_ID, "upload_session_id": "S1", "start_offset": 0, "end_offset": CHUNK}),
            ok({"start_offset": CHUNK, "end_offset": CHUNK + 10}),
            ok({"start_offset": CHUNK + 10, "end_offset": CHUNK + 10}),
            ok({"success": True}),
        ]
    )
    run(FacebookVideoPublisher(recorder, chunk_size=CHUNK).publish(ctx))
    first = recorder.calls[1]
    assert first["files"] == {"video_file_chunk": CHUNK}
    assert first["data"]["start_offset"] == "0"
    assert first["data"]["upload_session_id"] == "S1"
    second = recorder.calls[2]
    assert second["data"]["start_offset"] == str(CHUNK)
    assert second["files"] == {"video_file_chunk": 10}


def test_the_finish_call_carries_the_caption_title_and_hashtags(tmp_path):
    ctx = make_context(tmp_path, size=10)
    recorder = Recorder(
        [
            ok({"video_id": VIDEO_ID, "upload_session_id": "S1", "start_offset": 0, "end_offset": 10}),
            ok({"start_offset": 10, "end_offset": 10}),
            ok({"success": True}),
        ]
    )
    run(FacebookVideoPublisher(recorder, chunk_size=CHUNK).publish(ctx))
    finish = recorder.calls[-1]["data"]
    assert finish["upload_phase"] == "finish"
    assert finish["title"] == "Morning routine"
    assert "Coffee, then the gym." in finish["description"]
    assert "#morning #gym" in finish["description"]


def test_a_resumed_publish_does_not_start_a_second_session(tmp_path):
    ctx = make_context(
        tmp_path,
        size=2 * CHUNK,
        resume_from={
            "video_id": VIDEO_ID,
            "upload_session_id": "S1",
            "upload_offset": str(CHUNK),
            "upload_end_offset": str(2 * CHUNK),
        },
    )
    recorder = Recorder(
        [
            ok({"start_offset": 2 * CHUNK, "end_offset": 2 * CHUNK}),
            ok({"success": True}),
        ]
    )
    result = run(FacebookVideoPublisher(recorder, chunk_size=CHUNK).publish(ctx))
    phases = [call["data"]["upload_phase"] for call in recorder.calls]
    assert phases == ["transfer", "finish"], "a resumed job must continue, never re-start"
    assert result.provider_object_id == VIDEO_ID
    assert recorder.calls[0]["data"]["start_offset"] == str(CHUNK)


def test_a_rate_limited_transfer_is_retryable_and_keeps_the_video_id(tmp_path):
    ctx = make_context(tmp_path, size=CHUNK * 2)
    recorder = Recorder(
        [
            ok({"video_id": VIDEO_ID, "upload_session_id": "S1", "start_offset": 0, "end_offset": CHUNK}),
            err(429, 4, "Application request limit reached"),
        ]
    )
    with pytest.raises(PublisherError) as raised:
        run(FacebookVideoPublisher(recorder, chunk_size=CHUNK).publish(ctx))
    error = raised.value
    assert error.category is PublishErrorCategory.RATE_LIMIT
    assert error.retryable is True
    assert error.provider_object_id == VIDEO_ID
    assert error.detail["operation"] == "transfer"


def test_a_failure_while_finishing_is_ambiguous_not_retryable(tmp_path):
    ctx = make_context(tmp_path, size=10)
    recorder = Recorder(
        [
            ok({"video_id": VIDEO_ID, "upload_session_id": "S1", "start_offset": 0, "end_offset": 10}),
            ok({"start_offset": 10, "end_offset": 10}),
            err(500, 1, "unknown error"),
        ]
    )
    with pytest.raises(PublisherError) as raised:
        run(FacebookVideoPublisher(recorder, chunk_size=CHUNK).publish(ctx))
    error = raised.value
    assert error.category is PublishErrorCategory.AMBIGUOUS
    assert error.retryable is False
    assert error.ambiguous is True
    assert error.provider_object_id == VIDEO_ID, "the video may exist and the id must survive"


def test_an_expired_token_is_an_authorization_failure(tmp_path):
    ctx = make_context(tmp_path, size=10)
    recorder = Recorder([err(400, 190, "Error validating access token: Session has expired", subcode=463)])
    with pytest.raises(PublisherError) as raised:
        run(FacebookVideoPublisher(recorder, chunk_size=CHUNK).publish(ctx))
    assert raised.value.category is PublishErrorCategory.AUTHORIZATION
    assert raised.value.retryable is False


def test_a_missing_permission_is_a_permission_failure(tmp_path):
    ctx = make_context(tmp_path, size=10)
    recorder = Recorder([err(403, 200, "(#200) Requires pages_manage_posts to manage this Page")])
    with pytest.raises(PublisherError) as raised:
        run(FacebookVideoPublisher(recorder, chunk_size=CHUNK).publish(ctx))
    assert raised.value.category is PublishErrorCategory.PERMISSION


def test_a_missing_file_is_a_media_failure_not_a_provider_failure(tmp_path):
    ctx = make_context(tmp_path, size=10, media_path=str(tmp_path / "gone.mp4"))
    publisher = FacebookVideoPublisher(Recorder([]), chunk_size=CHUNK)
    with pytest.raises(PublisherError) as raised:
        run(publisher.publish(ctx))
    assert raised.value.category is PublishErrorCategory.MEDIA
    assert raised.value.code == "MEDIA_MISSING"


def test_an_oversized_file_is_refused_before_any_call(tmp_path):
    ctx = make_context(tmp_path, size=10, media_bytes=5 * 1024**3)
    recorder = Recorder([])
    with pytest.raises(PublisherError) as raised:
        run(FacebookVideoPublisher(recorder, chunk_size=CHUNK).publish(ctx))
    assert raised.value.category is PublishErrorCategory.VALIDATION
    assert recorder.calls == []


def test_status_polling_stops_when_the_video_is_ready(tmp_path):
    ctx = make_context(tmp_path, size=10)
    recorder = Recorder(
        [
            ok({"status": {"video_status": "processing"}, "permalink_url": None}),
            ok(
                {
                    "status": {"video_status": "ready"},
                    "permalink_url": f"https://www.facebook.com/{PAGE_ID}/videos/{VIDEO_ID}",
                }
            ),
        ]
    )
    publisher = FacebookVideoPublisher(recorder, chunk_size=CHUNK, sleep=_no_sleep)
    status = run(publisher.poll_until_ready(ctx, VIDEO_ID, attempts=5, delay_s=0))
    assert status.finished is True
    assert status.permalink and VIDEO_ID in status.permalink


def test_status_polling_gives_up_after_the_budget(tmp_path):
    ctx = make_context(tmp_path, size=10)
    recorder = Recorder([ok({"status": {"video_status": "processing"}}) for _ in range(3)])
    publisher = FacebookVideoPublisher(recorder, chunk_size=CHUNK, sleep=_no_sleep)
    status = run(publisher.poll_until_ready(ctx, VIDEO_ID, attempts=3, delay_s=0))
    assert status.finished is False
    assert len(recorder.calls) == 3, "polling must be bounded, never an endless loop"


def test_a_failed_processing_job_is_reported_as_failed(tmp_path):
    ctx = make_context(tmp_path, size=10)
    recorder = Recorder([ok({"status": {"video_status": "error"}})])
    status = run(FacebookVideoPublisher(recorder, chunk_size=CHUNK).check_status(ctx, VIDEO_ID))
    assert status.failed is True
    assert status.finished is False


def test_the_token_never_appears_in_an_error_or_the_result(tmp_path):
    ctx = make_context(tmp_path, size=10)
    recorder = Recorder([err(500, 1, "unknown error")])
    with pytest.raises(PublisherError) as raised:
        run(FacebookVideoPublisher(recorder, chunk_size=CHUNK).publish(ctx))
    serialized = json.dumps(raised.value.as_dict())
    assert TOKEN not in serialized
    assert TOKEN not in str(raised.value)
    assert TOKEN not in repr(ctx)


async def _no_sleep(_seconds: float) -> None:
    return None


def test_the_token_is_sent_to_the_graph_endpoint_only(tmp_path):
    """It travels in the form body, never in a URL that could end up in a log line."""
    ctx = make_context(tmp_path, size=10)
    recorder = Recorder(
        [
            ok({"video_id": VIDEO_ID, "upload_session_id": "S1", "start_offset": 0, "end_offset": 10}),
            ok({"start_offset": 10, "end_offset": 10}),
            ok({"success": True}),
        ]
    )
    run(FacebookVideoPublisher(recorder, chunk_size=CHUNK).publish(ctx))
    for call in recorder.calls:
        assert TOKEN not in call["url"]
        assert call["data"]["access_token"] == TOKEN
