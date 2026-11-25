"""Late-bound access to the AI service.

The orchestration engine must not import `main` (circular) and must not build a
service for read-only endpoints. It asks this module for the factory the application
installed at boot; if none was installed, the failure is explicit and becomes
`AI_UNAVAILABLE` at the API edge.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

ServiceFactory = Callable[[Session], Any]

_factory: ServiceFactory | None = None


def set_service_factory(factory: ServiceFactory | None) -> None:
    global _factory
    _factory = factory


def get_service_factory() -> ServiceFactory:
    if _factory is None:
        raise RuntimeError(
            "AI_UNAVAILABLE: local inference is not configured (no gateway/governor installed)"
        )
    return _factory


def build_service(session: Session) -> Any:
    return get_service_factory()(session)


__all__ = ["ServiceFactory", "build_service", "get_service_factory", "set_service_factory"]
