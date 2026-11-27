"""Instagram Reels publisher tests, on both login paths.

Instagram's flow has one truly irreversible call (`media_publish`), so much of what is tested here is
what happens around it: an unpublishable media URL, a container that never finishes, a container that
errors, a publish call whose answer is lost, and a retry that must not publish twice.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from creatoros.db.enums import OAuthRoute, PublishErrorCategory, PublishMode
from creatoros.integrations.social.base import HttpResponse
from creatoros.integrations.social.publishing.publishers import (
    InstagramReelsPublisher,
    PublishContext,
    PublisherError,
)

IG_USER = "17841400000000000"
TOKEN = "IGAA-secret-token"
CREATION = "17900000000000001"
MEDIA = "17900000000000002"
MEDIA_URL = "https://creatoros-media.s3.eu-west-3.amazonaws.com/creatoros/publish/ab/x.mp4"
PERMALINK = "https://www.instagram.com/reel/CxYzAbCdEfG/"


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class Recorder:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    async def request(self, method, url, *, params=None, data=None, json=None, headers=None, timeout=20.0, files=None):
        self.calls.append({"method": method, "url": url, "params": params, "data": data})
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


def err(status: int, code: int, message: str = "boom") -> HttpResponse:
    return HttpResponse(status=status, headers={}, body={"error": {"message": message, "code": code}})


async def _no_sleep(_seconds: float) -> None:
    return None


def make_context(**overrides) -> PublishContext:
    values = {
        "job_id": "01JOBJOBJOBJOBJOBJOBJOBJOB",
        "account_provider_id": IG_USER,
        "access_token": TOKEN,
        "mode": PublishMode.REELS,
        "caption": "Morning routine",
        "hashtags": ("morning",),
        "media_url": MEDIA_URL,
        "media_bytes": 7_000_000,
        "api_version": "v23.0",
        "allowed_url_hosts": ("s3.eu-west-3.amazonaws.com",),
    }
    values.update(overrides)
    return PublishContext(**values)


HAPPY = [
    ok({"id": CREATION}),
    ok({"status_code": "IN_PROGRESS"}),
    ok({"status_code": "FINISHED"}),
    ok({"id": MEDIA}),
    ok({"permalink": PERMALINK, "media_type": "VIDEO", "timestamp": "2026-11-24T10:00:00+0000"}),
]


def test_the_happy_path_creates_a_container_polls_publishes_and_verifies():
    recorder = Recorder(list(HAPPY))
    publisher = InstagramReelsPublisher(recorder, sleep=_no_sleep)
    result = run(publisher.publish(make_context()))

    assert result.provider_object_id == MEDIA
    assert result.permalink == PERMALINK
    assert result.state == "published"
    assert [call["method"] for call in recorder.calls] == ["POST", "GET", "GET", "POST", "GET"]
    create = recorder.calls[0]
    assert create["url"] == f"https://graph.instagram.com/v23.0/{IG_USER}/media"
    assert create["data"]["media_type"] == "REELS"
    assert create["data"]["video_url"] == MEDIA_URL
    assert create["data"]["caption"] == "Morning routine\n\n#morning"


def test_the_facebook_login_route_uses_the_facebook_graph_host():
    recorder = Recorder(list(HAPPY))
    publisher = InstagramReelsPublisher(recorder, route=OAuthRoute.FACEBOOK_LOGIN, sleep=_no_sleep)
    run(publisher.publish(make_context()))
    assert all("graph.facebook.com" in call["url"] for call in recorder.calls)
    assert not any("graph.instagram.com" in call["url"] for call in recorder.calls)


def test_a_localhost_media_url_is_refused_before_any_call():
    recorder = Recorder([])
    publisher = InstagramReelsPublisher(recorder, sleep=_no_sleep)
    with pytest.raises(PublisherError) as raised:
        run(publisher.publish(make_context(media_url="http://127.0.0.1:8080/clip.mp4")))
    assert raised.value.category is PublishErrorCategory.MEDIA
    assert raised.value.code == "MEDIA_URL_REJECTED"
    assert recorder.calls == [], "nothing may be sent to Instagram for a URL it could never fetch"


def test_a_media_url_on_an_unconfigured_host_is_refused():
    publisher = InstagramReelsPublisher(Recorder([]), sleep=_no_sleep)
    with pytest.raises(PublisherError) as raised:
        run(
            publisher.publish(
                make_context(media_url="https://evil.example.com/x.mp4", allowed_url_hosts=())
            )
        )
    assert raised.value.detail["reason"] == "evil.example.com is not an allowed host"


def test_a_missing_media_url_is_a_media_failure():
    publisher = InstagramReelsPublisher(Recorder([]), sleep=_no_sleep)
    with pytest.raises(PublisherError) as raised:
        run(publisher.publish(make_context(media_url=None)))
    assert raised.value.code == "MEDIA_URL_REJECTED"


def test_a_container_that_errors_is_not_retried_forever():
    recorder = Recorder([ok({"id": CREATION}), ok({"status_code": "ERROR"})])
    publisher = InstagramReelsPublisher(recorder, sleep=_no_sleep)
    with pytest.raises(PublisherError) as raised:
        run(publisher.publish(make_context()))
    assert raised.value.category is PublishErrorCategory.PROVIDER_PROCESSING
    assert raised.value.code == "CONTAINER_ERROR"
    assert raised.value.provider_object_id == CREATION
    assert raised.value.retryable is False


def test_a_container_that_is_still_processing_is_retryable_with_the_same_container():
    recorder = Recorder(
        [ok({"id": CREATION})] + [ok({"status_code": "IN_PROGRESS"}) for _ in range(3)]
    )
    publisher = InstagramReelsPublisher(
        recorder, sleep=_no_sleep, container_attempts=3, container_delay_s=0
    )
    with pytest.raises(PublisherError) as raised:
        run(publisher.publish(make_context()))
    assert raised.value.category is PublishErrorCategory.TRANSIENT
    assert raised.value.code == "CONTAINER_NOT_READY"
    assert raised.value.provider_object_id == CREATION


def test_a_lost_publish_response_is_ambiguous_and_keeps_the_creation_id():
    recorder = Recorder(
        [
            ok({"id": CREATION}),
            ok({"status_code": "FINISHED"}),
            err(503, 2, "Service temporarily unavailable"),
        ]
    )
    publisher = InstagramReelsPublisher(recorder, sleep=_no_sleep)
    with pytest.raises(PublisherError) as raised:
        run(publisher.publish(make_context()))
    error = raised.value
    assert error.category is PublishErrorCategory.AMBIGUOUS
    assert error.ambiguous is True
    assert error.retryable is False


def test_a_publish_call_with_no_media_id_is_ambiguous():
    recorder = Recorder([ok({"id": CREATION}), ok({"status_code": "FINISHED"}), ok({})])
    publisher = InstagramReelsPublisher(recorder, sleep=_no_sleep)
    with pytest.raises(PublisherError) as raised:
        run(publisher.publish(make_context()))
    assert raised.value.code == "MEDIA_ID_MISSING"
    assert raised.value.provider_object_id == CREATION


def test_a_retry_that_already_has_a_media_id_only_verifies():
    recorder = Recorder([ok({"permalink": PERMALINK, "media_type": "VIDEO"})])
    publisher = InstagramReelsPublisher(recorder, sleep=_no_sleep)
    result = run(publisher.publish(make_context(resume_from={"media_id": MEDIA})))
    assert result.provider_object_id == MEDIA
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["method"] == "GET"
    assert "media_publish" not in recorder.calls[0]["url"]


def test_a_retry_with_a_container_polls_it_instead_of_creating_another():
    recorder = Recorder(
        [
            ok({"status_code": "FINISHED"}),
            ok({"id": MEDIA}),
            ok({"permalink": PERMALINK}),
        ]
    )
    publisher = InstagramReelsPublisher(recorder, sleep=_no_sleep)
    result = run(publisher.publish(make_context(resume_from={"creation_id": CREATION})))
    assert result.provider_object_id == MEDIA
    assert "media" not in recorder.calls[0]["url"].rsplit("/", 1)[-1]


def test_an_expired_container_asks_the_user_rather_than_retrying():
    recorder = Recorder([ok({"id": CREATION}), ok({"status_code": "EXPIRED"})])
    publisher = InstagramReelsPublisher(recorder, sleep=_no_sleep)
    with pytest.raises(PublisherError) as raised:
        run(publisher.publish(make_context()))
    assert raised.value.code == "CONTAINER_EXPIRED"
    assert raised.value.retryable is False


def test_options_are_passed_through_only_when_chosen():
    recorder = Recorder(list(HAPPY))
    publisher = InstagramReelsPublisher(recorder, sleep=_no_sleep)
    run(
        publisher.publish(
            make_context(options={"share_to_feed": False, "thumb_offset": 1500, "collaborators": ["a", "b"]})
        )
    )
    data = recorder.calls[0]["data"]
    assert data["share_to_feed"] == "false"
    assert data["thumb_offset"] == "1500"
    assert data["collaborators"] == "a,b"


def test_status_reports_a_published_media_and_an_unknown_id_differently():
    recorder = Recorder([ok({"permalink": PERMALINK, "status_code": "PUBLISHED"}), ok({}, status=404)])
    publisher = InstagramReelsPublisher(recorder, sleep=_no_sleep)
    published = run(publisher.check_status(make_context(), MEDIA))
    assert published.finished is True and published.permalink == PERMALINK
    missing = run(publisher.check_status(make_context(), "0"))
    assert missing.state == "missing" and missing.failed is False


def test_deleting_an_instagram_post_is_not_offered():
    publisher = InstagramReelsPublisher(Recorder([]), sleep=_no_sleep)
    with pytest.raises(PublisherError) as raised:
        run(publisher.delete(make_context(), MEDIA))
    assert raised.value.code == "DELETE_NOT_SUPPORTED"


def test_the_token_is_never_in_a_url_or_an_error():
    recorder = Recorder([err(403, 200, "(#200) permission missing")])
    publisher = InstagramReelsPublisher(recorder, sleep=_no_sleep)
    with pytest.raises(PublisherError) as raised:
        run(publisher.publish(make_context()))
    assert TOKEN not in json.dumps(raised.value.as_dict())
    for call in recorder.calls:
        assert TOKEN not in call["url"]
    assert recorder.calls[0]["data"]["access_token"] == TOKEN
