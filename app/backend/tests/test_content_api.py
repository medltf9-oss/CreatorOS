"""CRUD and constraint tests for media, projects, variants, accounts, agents, queue."""
from __future__ import annotations

import hashlib
import os


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ------------------------------------------------------------------------ media
def test_create_and_list_media(client):
    payload = {
        "source_path": r"<DATA_ROOT>\data\media\incoming\a.mp4",
        "original_filename": "a.mp4",
        "sha256": _sha("a"),
        "size_bytes": 2048,
        "duration_ms": 12000,
        "width": 1080,
        "height": 1920,
        "has_audio": True,
    }
    created = client.post("/media", json=payload)
    assert created.status_code == 201, created.text
    asset = created.json()
    assert asset["id"]
    assert asset["rights_status"] == "uncertain"

    listed = client.get("/media")
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 1
    assert body["items"][0]["original_filename"] == "a.mp4"


def test_duplicate_media_is_rejected(client):
    payload = {
        "source_path": r"<DATA_ROOT>\data\media\incoming\dup.mp4",
        "original_filename": "dup.mp4",
        "sha256": _sha("dup"),
        "size_bytes": 10,
    }
    assert client.post("/media", json=payload).status_code == 201
    second = client.post("/media", json=payload)
    assert second.status_code == 409
    assert "DUPLICATE_ASSET" in second.json()["code"]


def test_media_validation_rejects_bad_sha(client):
    response = client.post(
        "/media",
        json={
            "source_path": "x.mp4",
            "original_filename": "x.mp4",
            "sha256": "not-a-sha",
            "size_bytes": 1,
        },
    )
    assert response.status_code == 422


def test_media_patch_and_delete(client):
    created = client.post(
        "/media",
        json={
            "source_path": r"<DATA_ROOT>\data\media\incoming\p.mp4",
            "original_filename": "p.mp4",
            "sha256": _sha("p"),
            "size_bytes": 5,
        },
    ).json()
    path = f"/media/{created['id']}"
    patched = client.patch(path, json={"rights_status": "owned", "rights_notes": "shot by me", "tags_json": ["demo"]})
    assert patched.status_code == 200
    assert patched.json()["rights_status"] == "owned"
    assert client.delete(path).status_code == 204
    assert client.get(path).status_code == 404


# --------------------------------------------------------------------- projects
def test_project_and_variants_flow(client):
    asset = client.post(
        "/media",
        json={
            "source_path": r"<DATA_ROOT>\data\media\incoming\proj.mp4",
            "original_filename": "proj.mp4",
            "sha256": _sha("proj"),
            "size_bytes": 99,
        },
    ).json()

    project = client.post("/projects", json={"title": "Launch clip", "source_asset_id": asset["id"]})
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]

    for platform in ("facebook", "instagram", "tiktok"):
        response = client.post(f"/projects/{project_id}/variants", json={"platform": platform})
        assert response.status_code == 201, response.text

    variants = client.get(f"/projects/{project_id}/variants").json()
    assert len(variants) == 3
    assert {v["platform"] for v in variants} == {"facebook", "instagram", "tiktok"}


def test_duplicate_variant_target_rejected(client):
    project_id = client.post("/projects", json={"title": "P"}).json()["id"]
    assert client.post(f"/projects/{project_id}/variants", json={"platform": "tiktok"}).status_code == 201
    duplicate = client.post(f"/projects/{project_id}/variants", json={"platform": "tiktok"})
    assert duplicate.status_code == 409
    assert "VARIANT_EXISTS" in duplicate.json()["code"]


def test_project_rejects_unknown_asset(client):
    response = client.post("/projects", json={"title": "X", "source_asset_id": "does-not-exist"})
    assert response.status_code == 422


def test_project_not_found(client):
    assert client.get("/projects/nope").status_code == 404


# --------------------------------------------------------------------- accounts
def test_manual_account_registration_is_gone(client):
    """Phase 6 removed POST /accounts: an account row without a credential is a fake account.

    Accounts are only created by a completed OAuth flow, which is covered by
    tests/test_social_api.py.
    """
    payload = {
        "platform": "facebook",
        "provider_account_id": "page-123",
        "display_name": "My Page",
        "account_type": "facebook_page",
        "scopes_json": ["pages_manage_posts"],
        "connection_status": "connected",
    }
    assert client.post("/accounts", json=payload).status_code == 405
    listing = client.get("/accounts")
    assert listing.status_code == 200
    assert listing.json()["total"] == 0


# ----------------------------------------------------------------------- agents
def test_agent_registry_is_seeded_by_the_application(client):
    """Phase 4 replaced the empty placeholder: the real workforce is seeded at boot."""
    response = client.get("/agents/registry/summary")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agents_total"] == 111
    assert body["leaders"] == 10
    assert body["specialists"] == 100


def test_unknown_agent_is_rejected(client):
    response = client.get("/agents/registry/does_not_exist")
    assert response.status_code == 404
    assert response.json()["code"] == "AGENT_NOT_FOUND"


# ------------------------------------------------------------------------ queue
def test_queue_enqueue_list_cancel(client):
    created = client.post("/queue/jobs", json={"job_type": "noop", "payload": {"k": "v"}, "idempotency_key": "k1"})
    assert created.status_code == 201, created.text
    job = created.json()
    assert job["state"] == "queued"

    again = client.post("/queue/jobs", json={"job_type": "noop", "payload": {"k": "v"}, "idempotency_key": "k1"})
    assert again.status_code == 201
    assert again.json()["id"] == job["id"], "duplicate enqueue should return the same job"

    stats = client.get("/queue/stats").json()
    assert stats["counts"]["queued"] >= 1

    cancelled = client.post(f"/queue/jobs/{job['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"


def test_queue_validation(client):
    assert client.post("/queue/jobs", json={"job_type": ""}).status_code == 422
    assert client.post("/queue/jobs", json={"job_type": "x", "priority": 500}).status_code == 422


def test_audit_records_writes(client):
    client.post("/projects", json={"title": "Audited"})
    events = client.get("/audit", params={"action": "project.create"}).json()
    assert events["total"] >= 1
    assert events["items"][0]["resource_type"] == "content_project"


def test_settings_roundtrip(client):
    put = client.put("/settings/ui.theme", json={"key": "ui.theme", "value_json": {"mode": "dark"}})
    assert put.status_code == 200
    listed = client.get("/settings").json()
    assert any(item["key"] == "ui.theme" for item in listed)


def test_backup_create_and_verify(client):
    response = client.post("/backups")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["integrity"] == "ok"
    assert body["size_bytes"] > 0
    from creatoros.core.settings import resolve_root

    assert os.path.exists(body["path"])
    assert body["path"].lower().startswith(str(resolve_root()).lower())

    listed = client.get("/backups").json()
    assert listed["items"][0]["verified"] is True
