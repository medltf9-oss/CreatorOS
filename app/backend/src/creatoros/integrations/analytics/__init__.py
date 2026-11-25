"""Provider analytics adapters and the metric catalogue. HTTP and data only, never SQLAlchemy."""
from .catalog import (
    CATALOG_VERSION,
    METRIC_CATALOG,
    MetricSpec,
    catalogue_by_key,
    ensure_catalog_is_consistent,
    metrics_for,
    preferred,
)

__all__ = [
    "CATALOG_VERSION",
    "METRIC_CATALOG",
    "MetricSpec",
    "catalogue_by_key",
    "ensure_catalog_is_consistent",
    "metrics_for",
    "preferred",
]
