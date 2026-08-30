"""Deterministic analytics primitives used by the Product Workspace.

The analytics package intentionally has no database or web-framework dependency.  API
and background-job layers can pass a :class:`pandas.DataFrame` (or records) to these
functions and persist the returned, JSON-serialisable artifacts.
"""

from .engine import AnalysisArtifact, AnalysisEngine
from .quality import QualityReport, apply_cleaning, assess_quality

__all__ = [
    "AnalysisArtifact",
    "AnalysisEngine",
    "QualityReport",
    "apply_cleaning",
    "assess_quality",
]
