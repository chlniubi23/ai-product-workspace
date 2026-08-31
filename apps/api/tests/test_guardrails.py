"""Guardrails for split-era invariants that a refactor could silently break.

Each test locks one convention that has no single owner:

- ``_safe_data_file`` must refuse any storage_path escaping DATA_ROOT (the
  project-deletion unlink path depends on it against tampered rows).
- ``models.now()`` must keep producing naive UTC -- serialize() re-attaches
  the UTC marker on the way out and every DATETIME column stores naive UTC.
- pandas must never be imported at module top level outside analytics/
  (lazy loading keeps API startup fast; see common._require_pandas).
- the request-side feedback scrub list in services/ai_stages.py must stay a
  superset of the AI-context firewall list in ai_context.py.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings
from app.services.datasets import _safe_data_file


def test_safe_data_file_resolves_paths_inside_the_data_root():
    resolved = _safe_data_file("uploads/some-file.csv")
    assert resolved is not None
    assert resolved == (settings.data_path / "uploads" / "some-file.csv").resolve()


def test_safe_data_file_refuses_paths_that_escape_the_data_root():
    assert _safe_data_file("../outside.csv") is None
    assert _safe_data_file("uploads/../../secrets.txt") is None
    assert _safe_data_file("a/../../../b.csv") is None
    assert _safe_data_file("") is None


def test_models_now_returns_naive_utc():
    from app.models import now

    before = datetime.now(UTC).replace(tzinfo=None)
    value = now()
    after = datetime.now(UTC).replace(tzinfo=None)
    assert value.tzinfo is None, "models.now() must stay naive; serialize() owns the UTC marker"
    assert before <= value <= after


def test_no_module_top_level_pandas_import_outside_analytics():
    app_root = Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    for module in app_root.rglob("*.py"):
        relative = module.relative_to(app_root).as_posix()
        if relative.startswith("analytics"):
            continue  # engine/quality own a deliberate top-level import
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                if any(alias.name == "pandas" or alias.name.startswith("pandas.") for alias in node.names):
                    offenders.append(relative)
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "pandas":
                offenders.append(relative)
    assert offenders == [], f"pandas must be imported lazily via common._require_pandas: {offenders}"


def test_request_side_feedback_scrub_list_is_a_superset_of_the_firewall_list():
    from app.ai_context import _FEEDBACK_CONTENT_KEYS
    from app.services.ai_stages import _FEEDBACK_CONTEXT_KEYS

    assert _FEEDBACK_CONTENT_KEYS <= _FEEDBACK_CONTEXT_KEYS
    assert {"sample", "samples"} <= _FEEDBACK_CONTEXT_KEYS
