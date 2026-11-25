"""Publishing errors.

Same shape as :class:`creatoros.integrations.social.errors.SocialError`, for the same reason: a
stable machine code, a sentence a human can act on, and a detail mapping that contains facts about
the request - never a token, a signed URL or a filesystem path.
"""

from __future__ import annotations

from typing import Any

#: The destination exists and the caller should look at its state, not retry blindly.
CONFLICT_CODES = frozenset(
    {
        "DESTINATION_EXISTS",
        "INTENT_HAS_IN_FLIGHT_DESTINATION",
        "DESTINATION_LOCKED",
        "DESTINATION_TERMINAL",
        "APPROVAL_ALREADY_USED",
    }
)

#: Something the user must change before the request can succeed.
USER_ACTION_CODES = frozenset(
    {
        "ACCOUNT_NOT_PUBLISH_CAPABLE",
        "NO_SOURCE_ASSET",
        "SOURCE_ASSET_NOT_READY",
        "DELIVERY_NOT_CONFIGURED",
    }
)


class PublishingError(Exception):
    """A failure with a stable code and no secret material."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.detail = detail or {}

    @property
    def user_action_required(self) -> bool:
        return self.code in USER_ACTION_CODES

    @property
    def conflict(self) -> bool:
        return self.code in CONFLICT_CODES

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": dict(self.detail)}
