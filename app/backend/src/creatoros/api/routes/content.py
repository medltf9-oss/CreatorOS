"""Content routes: media assets, projects, platform variants."""
from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, status

from ...db.models import ContentProject, MediaAsset, PlatformVariant
from ...schemas.api import (
    MediaAssetCreate,
    MediaAssetOut,
    MediaAssetUpdate,
    Page,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    VariantCreate,
    VariantOut,
    VariantUpdate,
)
from ..deps import Audit, DbSession, Paging, paginate

router = APIRouter(tags=["content"])


# ------------------------------------------------------------------------ media
@router.post("/media", response_model=MediaAssetOut, status_code=status.HTTP_201_CREATED)
def create_media(body: MediaAssetCreate, session: DbSession, audit: Audit) -> MediaAsset:
    existing = session.scalar(sa.select(MediaAsset).where(MediaAsset.sha256 == body.sha256))
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"DUPLICATE_ASSET: identical content already imported as {existing.id}",
        )
    asset = MediaAsset(**body.model_dump())
    session.add(asset)
    session.flush()
    audit.record("media.create", resource_type="media_asset", resource_id=asset.id, metadata={"sha256": body.sha256})
    return asset


@router.get("/media", response_model=Page[MediaAssetOut])
def list_media(session: DbSession, paging: Paging, q: str | None = None) -> Page[MediaAssetOut]:
    where = []
    if q:
        like = f"%{q}%"
        where.append(sa.or_(MediaAsset.original_filename.ilike(like), MediaAsset.rights_notes.ilike(like)))
    items, total = paginate(session, MediaAsset, paging, *where)
    return Page(items=[MediaAssetOut.model_validate(i) for i in items], total=total, limit=paging.limit, offset=paging.offset)


@router.get("/media/{asset_id}", response_model=MediaAssetOut)
def get_media(asset_id: str, session: DbSession) -> MediaAsset:
    asset = session.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "media asset not found")
    return asset


@router.patch("/media/{asset_id}", response_model=MediaAssetOut)
def update_media(asset_id: str, body: MediaAssetUpdate, session: DbSession, audit: Audit) -> MediaAsset:
    asset = session.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "media asset not found")
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(asset, key, value)
    audit.record("media.update", resource_type="media_asset", resource_id=asset_id, metadata={"fields": list(changes)})
    return asset


@router.delete("/media/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_media(asset_id: str, session: DbSession, audit: Audit) -> None:
    asset = session.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "media asset not found")
    session.delete(asset)
    audit.record("media.delete", resource_type="media_asset", resource_id=asset_id)


# --------------------------------------------------------------------- projects
@router.post("/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(body: ProjectCreate, session: DbSession, audit: Audit) -> ContentProject:
    if body.source_asset_id and session.get(MediaAsset, body.source_asset_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "source_asset_id does not exist")
    project = ContentProject(**body.model_dump())
    session.add(project)
    session.flush()
    audit.record("project.create", resource_type="content_project", resource_id=project.id)
    return project


@router.get("/projects", response_model=Page[ProjectOut])
def list_projects(session: DbSession, paging: Paging) -> Page[ProjectOut]:
    items, total = paginate(session, ContentProject, paging)
    return Page(items=[ProjectOut.model_validate(i) for i in items], total=total, limit=paging.limit, offset=paging.offset)


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, session: DbSession) -> ContentProject:
    project = session.get(ContentProject, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return project


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def update_project(project_id: str, body: ProjectUpdate, session: DbSession, audit: Audit) -> ContentProject:
    project = session.get(ContentProject, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(project, key, value)
    audit.record("project.update", resource_type="content_project", resource_id=project_id, metadata={"fields": list(changes)})
    return project


# --------------------------------------------------------------------- variants
@router.post("/projects/{project_id}/variants", response_model=VariantOut, status_code=status.HTTP_201_CREATED)
def create_variant(project_id: str, body: VariantCreate, session: DbSession, audit: Audit) -> PlatformVariant:
    if session.get(ContentProject, project_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    duplicate = session.scalar(
        sa.select(PlatformVariant).where(
            PlatformVariant.project_id == project_id,
            PlatformVariant.platform == body.platform,
            PlatformVariant.target_account_id == body.target_account_id,
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"VARIANT_EXISTS: {body.platform} variant for this target already exists ({duplicate.id})",
        )
    variant = PlatformVariant(project_id=project_id, **body.model_dump())
    session.add(variant)
    session.flush()
    audit.record("variant.create", resource_type="platform_variant", resource_id=variant.id, metadata={"platform": body.platform.value})
    return variant


@router.get("/projects/{project_id}/variants", response_model=list[VariantOut])
def list_variants(project_id: str, session: DbSession) -> list[PlatformVariant]:
    return list(
        session.scalars(
            sa.select(PlatformVariant).where(PlatformVariant.project_id == project_id).order_by(PlatformVariant.platform)
        )
    )


@router.patch("/variants/{variant_id}", response_model=VariantOut)
def update_variant(variant_id: str, body: VariantUpdate, session: DbSession, audit: Audit) -> PlatformVariant:
    variant = session.get(PlatformVariant, variant_id)
    if variant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "variant not found")
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(variant, key, value)
    audit.record("variant.update", resource_type="platform_variant", resource_id=variant_id, metadata={"fields": list(changes)})
    return variant
