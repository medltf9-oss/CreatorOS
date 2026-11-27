"""API dependencies: database session, audit writer, pagination."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

import sqlalchemy as sa
from fastapi import Depends, Query, Request
from sqlalchemy.orm import Session

from ..core.security import require_token
from ..db.session import get_session_factory
from ..services.backup import AuditService

Auth = Annotated[None, Depends(require_token)]


def get_db(request: Request) -> Iterator[Session]:
    factory = getattr(request.app.state, "session_factory", None) or get_session_factory()
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_db)]


def get_audit(session: DbSession) -> AuditService:
    return AuditService(session)


Audit = Annotated[AuditService, Depends(get_audit)]


class Pagination:
    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> None:
        self.limit = limit
        self.offset = offset


Paging = Annotated[Pagination, Depends(Pagination)]


def paginate(session: Session, model: type, paging: Pagination, *where) -> tuple[list, int]:
    base = sa.select(model)
    count_stmt = sa.select(sa.func.count()).select_from(model)
    for clause in where:
        base = base.where(clause)
        count_stmt = count_stmt.where(clause)
    total = session.scalar(count_stmt) or 0
    items = list(
        session.scalars(
            base.order_by(model.created_at.desc(), model.id.desc())
            .limit(paging.limit)
            .offset(paging.offset)
        )
    )
    return items, total
