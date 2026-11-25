"""Library API tests: auth, signed media URLs, range streaming, import signing, jobs."""
from __future__ import annotations

import hashlib

import pytest

from creatoros.db.enums import DerivativeState, QueueState
from creatoros.db.models import MediaAsset, MediaDerivative, QueueJob
from creatoros.services.media.jobs import JOB_IMPORT, JOB_PREVIEW, MEDIA_QUEUE, enqueue_media_job
from creatoros.services.media.library import MediaLibrary


def _import(client, session, path, **kwargs):
    library = MediaLibrary(session)
    outcome = library.import_file(path, **kwargs)
    session.commit()
    return outcome


# ------------------------------------------------------------------- basics
def test_library_requires_auth(unauth_client):
    assert unauth_client.get("/library/stats").status_code == 401
    assert unauth_client.get("/library/assets").status_code == 401


def test_stats_are_real_numbers(client, session, media_files):
    _import(client, session, media_files.landscape)
    _import(client, session, media_files.image)

    body = client.get("/library/stats").json()
    if body["assets_total"] != 2:
        from creatoros.db.models import MediaAsset
        session.expire_all()
        print("LEAK:", [(row.id[-4:], row.original_filename, row.created_at) for row in session.query(MediaAsset).all()])
    if body["assets_total"] != 2:
        listed = client.get("/library/assets?limit=20").json()
        print("API SEES:", [(row["original_filename"], row["id"][-4:]) for row in listed["items"]])
        session.expire_all()
        from creatoros.db.models import MediaAsset
        print("DB HAS:", [(row.original_filename, row.id[-4:]) for row in session.query(MediaAsset).all()])
        from creatoros.core.settings import get_settings
        print("ROOT:", get_settings().root)
    assert body["assets_total"] == 2
    assert body["by_kind"]["video"] == 1
    assert body["by_kind"]["image"] == 1
    assert body["bytes_total"] > 0
    assert body["derivatives_total"] >= 2
    assert body["storage"]["originals"]["files"] == 2
    assert body["disk"]["free_gb"] > 0
    assert body["capabilities"]["h264_encoder"]["name"] == "libx264"
    assert len(body["recent"]) == 2


def test_list_filters_sorts_and_pages(client, session, media_files):
    _import(client, session, media_files.landscape)
    _import(client, session, media_files.image)
    _import(client, session, media_files.audio)

    videos = client.get("/library/assets?kind=video").json()
    assert videos["total"] == 1
    assert videos["items"][0]["kind"] == "video"

    by_size = client.get("/library/assets?sort=size&order=asc").json()
    sizes = [item["size_bytes"] for item in by_size["items"]]
    assert sizes == sorted(sizes)

    by_name = client.get("/library/assets?sort=name&order=asc").json()
    names = [item["original_filename"] for item in by_name["items"]]
    assert names == sorted(names)

    search = client.get("/library/assets?q=still").json()
    assert search["total"] == 1

    paged = client.get("/library/assets?limit=1&offset=1").json()
    assert paged["limit"] == 1 and len(paged["items"]) == 1 and paged["total"] == 3


def test_asset_detail_includes_probe_and_derivatives(client, session, media_files):
    outcome = _import(client, session, media_files.landscape)
    body = client.get(f"/library/assets/{outcome.asset_id}").json()
    assert body["sha256"] == outcome.sha256
    assert body["probe"]["video"]["width"] == 640
    assert body["probe_version"]
    assert any(item["kind"] == "thumbnail" for item in body["derivatives"])
    assert body["storage"]["original_bytes"] > 0
    assert body["thumbnail_url"] and "sig=" in body["thumbnail_url"]
    assert body["stream_url"] and "sig=" in body["stream_url"]


def test_unknown_asset_is_404(client):
    response = client.get("/library/assets/01M80ZZZZZZZZZZZZZZZZZZZ01")
    assert response.status_code == 404
    assert response.json()["code"] == "ASSET_NOT_FOUND"


# --------------------------------------------------------------- media bytes
def test_thumbnail_requires_a_signature(client, session, media_files):
    outcome = _import(client, session, media_files.image)
    unsigned = client.get(f"/library/assets/{outcome.asset_id}/thumbnail")
    assert unsigned.status_code == 403
    assert "MEDIA_URL" in unsigned.json()["code"]

    detail = client.get(f"/library/assets/{outcome.asset_id}").json()
    url = detail["thumbnail_url"]
    signed = client.get(url)
    assert signed.status_code == 200
    assert signed.headers["content-type"] == "image/webp"
    assert len(signed.content) > 500


def test_media_bytes_are_embeddable_cross_origin(client, session, media_files):
    """<img>/<video> live on a different origin (Vite in dev, file:// when packaged)."""
    outcome = _import(client, session, media_files.image)
    detail = client.get(f"/library/assets/{outcome.asset_id}").json()
    response = client.get(detail["thumbnail_url"])
    assert response.status_code == 200
    assert response.headers["cross-origin-resource-policy"] == "cross-origin"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_tampered_signature_is_rejected(client, session, media_files):
    outcome = _import(client, session, media_files.image)
    detail = client.get(f"/library/assets/{outcome.asset_id}").json()
    url = detail["thumbnail_url"].replace("sig=", "sig=00")
    assert client.get(url).status_code == 403


def test_expired_url_is_rejected(client, session, media_files):
    outcome = _import(client, session, media_files.image)
    detail = client.get(f"/library/assets/{outcome.asset_id}").json()
    url = detail["thumbnail_url"]
    parts = url.split("?")[1].split("&")
    rebuilt = [part for part in parts if not part.startswith("exp=")]
    expired = url.split("?")[0] + "?" + "&".join(rebuilt + ["exp=1"])
    assert client.get(expired).status_code == 403


def test_signed_media_urls_work_without_a_bearer_token(client, unauth_client, session, media_files):
    """<img> and <video> cannot send Authorization: the signature alone must authorize bytes."""
    outcome = _import(client, session, media_files.image)
    detail = client.get(f"/library/assets/{outcome.asset_id}").json()
    thumb = unauth_client.get(detail["thumbnail_url"])
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/webp"
    stream = unauth_client.get(detail["stream_url"], headers={"Range": "bytes=0-49"})
    assert stream.status_code == 206
    assert len(stream.content) == 50
    # ...and the unsigned route still refuses an anonymous caller.
    assert unauth_client.get(f"/library/assets/{outcome.asset_id}/thumbnail").status_code == 403


def test_stream_supports_range_requests(client, session, media_files):
    outcome = _import(client, session, media_files.landscape, auto_thumbnail=False)
    detail = client.get(f"/library/assets/{outcome.asset_id}").json()
    response = client.get(detail["stream_url"], headers={"Range": "bytes=0-99"})
    assert response.status_code == 206
    assert len(response.content) == 100
    assert response.headers["content-range"].startswith("bytes 0-99/")
    assert response.headers["accept-ranges"] == "bytes"


def test_stream_serves_the_original_bytes(client, session, media_files):
    outcome = _import(client, session, media_files.audio, auto_thumbnail=False)
    detail = client.get(f"/library/assets/{outcome.asset_id}").json()
    response = client.get(detail["stream_url"])
    assert response.status_code == 200
    assert hashlib.sha256(response.content).hexdigest() == outcome.sha256


def test_poster_for_audio_is_a_waveform_image(client, session, media_files):
    outcome = _import(client, session, media_files.audio)
    asset = session.get(MediaAsset, outcome.asset_id)
    library = MediaLibrary(session)
    library.ensure_waveform_image(asset)
    session.commit()
    detail = client.get(f"/library/assets/{outcome.asset_id}").json()
    assert detail["poster_url"]
    response = client.get(detail["poster_url"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


# -------------------------------------------------------------------- import
def test_import_requires_the_ipc_signature_when_the_shell_is_present(client, monkeypatch, media_files):
    monkeypatch.setenv("CREATOROS_IPC_SECRET", "s3cret")
    response = client.post("/library/import", json={"paths": [{"path": str(media_files.image)}]})
    assert response.status_code == 403
    assert "IPC_SIGNATURE" in response.json()["code"]


def test_signed_import_is_accepted_and_queues_jobs(client, session, media_files):
    response = client.post("/library/import", json={"paths": [{"path": str(media_files.landscape)}]})
    assert response.status_code == 202, response.text
    body = response.json()
    assert len(body["jobs"]) == 1
    assert body["jobs"][0]["job_id"]
    assert body["duplicates"] == []
    session.expire_all()
    job = session.get(QueueJob, body["jobs"][0]["job_id"])
    assert job.job_type == JOB_IMPORT
    assert job.queue == MEDIA_QUEUE
    assert job.state == QueueState.QUEUED
    assert job.payload_json["source_path"] == str(media_files.landscape)


def test_import_reports_probable_duplicates_without_copying(client, session, media_files):
    _import(client, session, media_files.image)
    response = client.post(
        "/library/import",
        json={"paths": [{"path": str(media_files.image), "original_filename": "still.jpg"}]},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["jobs"] == []
    assert len(body["duplicates"]) == 1
    assert body["duplicates"][0]["existing_asset_id"]
    # Nothing new was queued or copied.
    assert session.query(QueueJob).filter_by(job_type=JOB_IMPORT).count() == 0


def test_import_rejects_missing_and_oversized_paths(client, session):
    missing = client.post("/library/import", json={"paths": [{"path": "<DATA_ROOT>/temp/nope.mp4"}]})
    assert missing.status_code == 202
    assert missing.json()["rejected"][0]["reason"].startswith("file does not exist")


def test_import_rejects_unknown_fields(client, media_files):
    response = client.post(
        "/library/import",
        json={"paths": [{"path": str(media_files.image)}], "auto_magic": True},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_import_preview_marks_probable_duplicates(client, session, media_files):
    _import(client, session, media_files.image)
    response = client.post("/library/import/preview", json={"paths": [{"path": str(media_files.image)}]})
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["status"] == "probable_duplicate"
    assert result["existing_asset_id"]


# ---------------------------------------------------------------------- jobs
def test_jobs_endpoint_lists_only_media_jobs(client, session, media_files):
    enqueue_media_job(session, JOB_IMPORT, {"source_path": str(media_files.image)})
    session.commit()
    body = client.get("/library/jobs").json()
    assert body["total"] == 1
    assert body["items"][0]["job_type"] == JOB_IMPORT
    assert body["items"][0]["progress"] is not None or body["items"][0]["state"] == "queued"


def test_cancel_and_retry_endpoints(client, session, media_files):
    job_id = enqueue_media_job(session, JOB_IMPORT, {"source_path": str(media_files.audio)})
    session.commit()
    cancelled = client.post(f"/library/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    session.expire_all()
    assert session.get(QueueJob, job_id).state in (QueueState.CANCELLED, QueueState.QUEUED)

    job = session.get(QueueJob, job_id)
    job.state = QueueState.DEAD_LETTER
    job.attempts = 3
    session.commit()
    retried = client.post(f"/library/jobs/{job_id}/retry")
    assert retried.status_code == 200
    assert retried.json()["state"] == "queued"


def test_visible_errors_for_unknown_jobs(client):
    assert client.get("/library/jobs/01M80ZZZZZZZZZZZZZZZZZZZ01").status_code == 404
    assert client.post("/library/jobs/01M80ZZZZZZZZZZZZZZZZZZZ01/cancel").status_code == 404
    assert client.post("/library/jobs/01M80ZZZZZZZZZZZZZZZZZZZ01/retry").status_code == 404


def test_derivative_requests_validate_the_profile(client, session, media_files):
    outcome = _import(client, session, media_files.landscape)
    bad = client.post(f"/library/assets/{outcome.asset_id}/transcode", json={"profile": "imax"})
    assert bad.status_code == 422
    good = client.post(f"/library/assets/{outcome.asset_id}/transcode", json={"profile": "fast_preview"})
    assert good.status_code == 200
    assert good.json()["job_type"] == "media.transcode"


def test_derivative_request_on_audio_asset_is_rejected(client, session, media_files):
    outcome = _import(client, session, media_files.audio)
    response = client.post(f"/library/assets/{outcome.asset_id}/preview")
    # Accepted at request time (the job rejects it), but the job reports a real failure.
    assert response.status_code == 200
    job = session.get(QueueJob, response.json()["job_id"])
    assert job.job_type == JOB_PREVIEW


def test_profiles_endpoint_documents_four_plus_profiles(client):
    body = client.get("/library/profiles").json()
    ids = {item["id"] for item in body["profiles"]}
    assert {"fast_preview", "editing_proxy", "normalized", "audio_wav", "audio_aac"} == ids
    for item in body["profiles"]:
        assert item["extension"] in (".mp4", ".wav", ".m4a")


# -------------------------------------------------------------------- deletes
def test_delete_derivative_requires_confirmation(client, session, media_files):
    outcome = _import(client, session, media_files.image)
    asset = session.get(MediaAsset, outcome.asset_id)
    library = MediaLibrary(session)
    thumb = library.primary_thumbnail(asset.id)
    assert client.delete(f"/library/derivatives/{thumb.id}").status_code == 428
    ok = client.delete(f"/library/derivatives/{thumb.id}?confirm=true")
    assert ok.status_code == 200
    session.expire_all()
    assert session.get(MediaDerivative, thumb.id).state == DerivativeState.DELETED
    assert not library.derivative_file(thumb).exists() if thumb.rel_path else True


def test_delete_asset_requires_the_asset_id_as_confirmation(client, session, media_files):
    outcome = _import(client, session, media_files.image)
    assert client.delete(f"/library/assets/{outcome.asset_id}").status_code == 428
    assert client.delete(f"/library/assets/{outcome.asset_id}?confirm=wrong").status_code == 428
    ok = client.delete(f"/library/assets/{outcome.asset_id}?confirm={outcome.asset_id}")
    assert ok.status_code == 200
    assert ok.json()["original_removed"] is True
    assert client.get(f"/library/assets/{outcome.asset_id}").status_code == 404


def test_delete_asset_in_use_requires_force(client, session, media_files, project_factory):
    outcome = _import(client, session, media_files.image)
    project_factory(source_asset_id=outcome.asset_id)
    session.commit()
    blocked = client.delete(f"/library/assets/{outcome.asset_id}?confirm={outcome.asset_id}")
    assert blocked.status_code == 409
    assert "ASSET_IN_USE" in blocked.json()["code"]
    forced = client.delete(f"/library/assets/{outcome.asset_id}?confirm={outcome.asset_id}&force=true")
    assert forced.status_code == 200


# ----------------------------------------------------------------- misc routes
def test_metadata_patch_validates_enums(client, session, media_files):
    outcome = _import(client, session, media_files.image)
    ok = client.patch(f"/library/assets/{outcome.asset_id}", json={"tags_json": ["launch"], "rights_status": "owned"})
    assert ok.status_code == 200
    assert ok.json()["changes"]["tags"] == 1
    bad = client.patch(f"/library/assets/{outcome.asset_id}", json={"rights_status": "stolen"})
    assert bad.status_code == 422


def test_probe_endpoint_returns_stored_and_recommendation(client, session, media_files):
    outcome = _import(client, session, media_files.audio)
    body = client.get(f"/library/assets/{outcome.asset_id}/probe").json()
    assert body["stored"]["kind"] == "audio"
    assert body["recommendation"]["profile"] == "audio_wav"


def test_inspect_endpoint_reprobes_the_file(client, session, media_files):
    outcome = _import(client, session, media_files.landscape)
    before = session.get(MediaAsset, outcome.asset_id).probe_json
    body = client.post(f"/library/assets/{outcome.asset_id}/inspect").json()
    assert body["kind"] == "video"
    assert body["probe"]["video"]["width"] == 640
    assert before["video"]["height"] == body["probe"]["video"]["height"]


def test_storage_and_recovery_endpoints(client, session, media_files):
    _import(client, session, media_files.image)
    storage = client.get("/library/storage").json()
    assert storage["areas"]["originals"]["bytes"] > 0
    assert "never deleted" in storage["spacing_note"]
    scan = client.post("/library/recovery/scan?older_than_minutes=0").json()
    assert "staging_removed" in scan


def test_capabilities_report_a_verified_encoder(client):
    body = client.get("/library/capabilities").json()
    assert body["h264_encoder"]["verified"] is True
    assert body["h264_encoder"]["hardware"] is False  # measured on this driver
    assert body["ffmpeg"]["available"] is True


def test_version_reports_phase_5_capabilities(client):
    body = client.get("/version").json()
    assert body["api"] == "phase-8.24"
    assert body["capabilities"]["media_library"] == "available"
    assert body["capabilities"]["media_transcode"] == "available"
    assert body["capabilities"]["media_hardware_encoding"] == "unavailable"
    # Phase 7 replaced the placeholder with a per-platform map; the engine itself is available.
    assert body["capabilities"]["publishing"] == "available"
    assert "publish_facebook" in body["capabilities"]


@pytest.fixture()
def project_factory(session):
    from creatoros.db.models import ContentProject

    def make(**overrides):
        counter = session.query(ContentProject).count() + 1
        project = ContentProject(
            title=overrides.pop("title", f"Project {counter}"),
            **overrides,
        )
        session.add(project)
        session.flush()
        return project

    return make
