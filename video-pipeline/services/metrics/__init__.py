"""Event-derived pipeline metrics (Todo 64).

Append-only, hash-bound event bundles in; auditable MetricsReport out.
Every number is recomputable from the bundle; the honesty gate refuses
KPI claims that the recorded events cannot support.
"""

from services.metrics.bundle import EventBundle, MetricsBundleError, load_bundle
from services.metrics.derive import derive_report
from services.metrics.report import main
from services.metrics.validate import validate_report

__all__ = [
    "EventBundle",
    "MetricsBundleError",
    "derive_report",
    "load_bundle",
    "main",
    "validate_report",
]
