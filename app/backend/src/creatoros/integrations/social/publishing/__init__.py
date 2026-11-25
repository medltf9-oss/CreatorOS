"""Publishing engine integrations: provider clients, media delivery and metadata rules.

Nothing in this package may import ``creatoros.db`` or open a database session. Provider code takes
plain values in and returns plain results out, so it can be tested against ``httpx.MockTransport``
without a schema, and so no HTTP layer can accidentally hold a transaction open across a three
minute upload.
"""

from __future__ import annotations

from .metadata.limits import PLATFORM_RULES, PlatformRules, rules_for
from .metadata.validators import (
    Finding,
    Severity,
    ValidationReport,
    validate_media,
    validate_options,
    validate_text,
    validate_variant,
)

__all__ = [
    "PLATFORM_RULES",
    "Finding",
    "PlatformRules",
    "Severity",
    "ValidationReport",
    "rules_for",
    "validate_media",
    "validate_options",
    "validate_text",
    "validate_variant",
]
