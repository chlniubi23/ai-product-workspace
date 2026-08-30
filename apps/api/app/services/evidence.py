from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..common import error
from ..models import (
    AnalysisArtifact,
    AnalysisRun,
    Dataset,
    DatasetVersion,
    DecisionProposal,
    Document,
    DocumentVersion,
    FeedbackCluster,
    FeedbackItem,
    FeedbackNote,
    Insight,
    Project,
    Task,
)


def _check_evidence_scope(db: Session, workspace_id: str, evidence: list[dict[str, Any]], project_id: str | None = None) -> None:
    """Validate evidence references against existence and resource boundaries.

    A reference whose type is recognised must point at an existing object
    (fabricated IDs are rejected, BUG-004), and any existing object is always
    checked against the current workspace/project boundary.
    """
    recognised_types = {
        "insight", "decision", "decision_proposal", "feedback_cluster", "feedback_theme",
        "document", "analysis_artifact", "artifact", "analysis", "analysis_run",
        "dataset", "data", "dataset_version", "data_version", "feedback", "feedback_item",
        "document_version", "task", "feedback_note",
    }
    for reference in evidence:
        raw_type, ref_id = reference.get("type"), reference.get("id")
        ref_type = str(raw_type).strip().lower().replace("-", "_") if raw_type else ""
        if not ref_type or not ref_id:
            continue
        item: Any = None
        owner_workspace: str | None = None
        owner_project: str | None = None
        if ref_type == "insight":
            item = db.get(Insight, ref_id)
            owner_workspace = item.workspace_id if item else None
            owner_project = item.project_id if item else None
        elif ref_type in {"decision", "decision_proposal"}:
            item = db.get(DecisionProposal, ref_id)
            owner_workspace = item.workspace_id if item else None
            owner_project = item.project_id if item else None
        elif ref_type in {"feedback_cluster", "feedback_theme"}:
            item = db.get(FeedbackCluster, ref_id)
            owner_workspace = item.workspace_id if item else None
            owner_project = item.project_id if item else None
        elif ref_type == "document":
            item = db.get(Document, ref_id)
            owner_workspace = item.workspace_id if item else None
            owner_project = item.project_id if item else None
        elif ref_type in {"analysis_artifact", "artifact"}:
            item = db.get(AnalysisArtifact, ref_id)
            run = db.get(AnalysisRun, item.analysis_run_id) if item else None
            owner_workspace = run.workspace_id if run else None
            owner_project = run.project_id if run else None
        elif ref_type in {"analysis", "analysis_run"}:
            item = db.get(AnalysisRun, ref_id)
            owner_workspace = item.workspace_id if item else None
            owner_project = item.project_id if item else None
        elif ref_type in {"dataset", "data"}:
            item = db.get(Dataset, ref_id)
            owner_workspace = item.workspace_id if item else None
            owner_project = item.project_id if item else None
        elif ref_type in {"dataset_version", "data_version"}:
            item = db.get(DatasetVersion, ref_id)
            dataset = db.get(Dataset, item.dataset_id) if item else None
            owner_workspace = dataset.workspace_id if dataset else None
            owner_project = dataset.project_id if dataset else None
        elif ref_type in {"feedback", "feedback_item"}:
            item = db.get(FeedbackItem, ref_id)
            owner_workspace = item.workspace_id if item else None
            owner_project = item.project_id if item else None
        elif ref_type == "feedback_note":
            item = db.get(FeedbackNote, ref_id)
            project = db.get(Project, item.project_id) if item and item.project_id else None
            owner_workspace = project.workspace_id if project else None
            owner_project = item.project_id if item else None
        elif ref_type == "document_version":
            item = db.get(DocumentVersion, ref_id)
            document = db.get(Document, item.document_id) if item else None
            owner_workspace = document.workspace_id if document else None
            owner_project = document.project_id if document else None
        elif ref_type == "task":
            item = db.get(Task, ref_id)
            owner_workspace = item.workspace_id if item else None
            owner_project = item.project_id if item else None
        if ref_type in recognised_types and item is None:
            raise error("VALIDATION_ERROR", f"Evidence {ref_type} '{ref_id}' does not exist", 400)
        if owner_workspace is not None and owner_workspace != workspace_id:
            raise error("FORBIDDEN", "Evidence is outside the current workspace", 403)
        if project_id and owner_project is not None and owner_project != project_id:
            raise error("FORBIDDEN", "Evidence is outside the current project", 403)


def _require_confirmed_insight_refs(
    db: Session,
    workspace_id: str,
    evidence: list[dict[str, Any]],
    project_id: str | None = None,
) -> None:
    """Allow documents to cite only confirmed insights.

    Evidence scope and existence are checked first so a caller cannot use the
    status check to probe an insight in another workspace or project.  Other
    evidence types keep their existing lifecycle semantics.
    """

    _check_evidence_scope(db, workspace_id, evidence, project_id)
    for reference in evidence:
        if not isinstance(reference, dict):
            continue
        ref_type = str(reference.get("type") or "").strip().lower().replace("-", "_")
        if ref_type != "insight":
            continue
        ref_id = str(reference.get("id") or "")
        insight = db.get(Insight, ref_id)
        # _check_evidence_scope above guarantees that a recognised reference
        # exists and belongs to the requested scope.
        if insight is not None and insight.status != "confirmed":
            raise error(
                "INSIGHT_NOT_CONFIRMED",
                "Only confirmed insights can be referenced by a document",
                409,
                {"insight_id": ref_id, "status": insight.status},
            )


def _require_nonempty_evidence(evidence: Any, *, subject: str = "Insight") -> list[dict[str, Any]]:
    """Require at least one structured reference before a claim can be stored.

    AI claims and human-entered claims share the same persistence contract.  A
    reference must carry both a type and an id so the scope validator can prove
    that it points to a real workspace resource.
    """

    if not isinstance(evidence, list) or not evidence:
        raise error("VALIDATION_ERROR", f"{subject} requires at least one evidence reference", 400)
    normalized: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict) or not str(item.get("type") or "").strip() or not str(item.get("id") or "").strip():
            raise error("VALIDATION_ERROR", f"{subject} evidence references require type and id", 400)
        normalized.append({"type": str(item["type"])[:80], "id": str(item["id"])[:255]})
    return normalized


def _linked_resource_scope(db: Session, link_type: str, target_id: str) -> tuple[str, str | None]:
    """Resolve a task link and return its workspace/project ownership."""
    normalized = link_type.strip().lower()
    target: Any = None
    project_id: str | None = None

    if normalized in {"dataset", "data"}:
        target = db.get(Dataset, target_id)
        if target is not None:
            project_id = target.project_id
    elif normalized in {"dataset_version", "dataset-version", "data_version", "data-version"}:
        version = db.get(DatasetVersion, target_id)
        dataset = db.get(Dataset, version.dataset_id) if version is not None else None
        target = dataset
        if target is not None:
            project_id = target.project_id
    elif normalized in {"analysis", "analysis_run", "analysis-run"}:
        target = db.get(AnalysisRun, target_id)
        if target is None and normalized == "analysis":
            artifact = db.get(AnalysisArtifact, target_id)
            target = db.get(AnalysisRun, artifact.analysis_run_id) if artifact is not None else None
        if target is not None:
            project_id = target.project_id
    elif normalized in {"analysis_artifact", "analysis-artifact", "artifact"}:
        artifact = db.get(AnalysisArtifact, target_id)
        run = db.get(AnalysisRun, artifact.analysis_run_id) if artifact is not None else None
        target = run
        if target is not None:
            project_id = target.project_id
    elif normalized in {"feedback", "feedback_item", "feedback-item"}:
        target = db.get(FeedbackItem, target_id)
        if target is not None:
            project_id = target.project_id
    elif normalized in {"feedback_cluster", "feedback-cluster", "feedback_theme", "feedback-theme"}:
        target = db.get(FeedbackCluster, target_id)
        if target is not None:
            project_id = target.project_id
    elif normalized == "insight":
        target = db.get(Insight, target_id)
        if target is not None:
            project_id = target.project_id
    elif normalized in {"decision", "decision_proposal", "decision-proposal"}:
        target = db.get(DecisionProposal, target_id)
        if target is not None:
            project_id = target.project_id
    elif normalized in {"document", "doc"}:
        target = db.get(Document, target_id)
        if target is not None:
            project_id = target.project_id
    else:
        raise error("VALIDATION_ERROR", f"Unsupported task link type: {link_type}", 422)

    if target is None:
        raise error("NOT_FOUND", "Linked object not found", 404)
    if isinstance(target, Dataset) and target.deleted_at is not None:
        raise error("NOT_FOUND", "Linked dataset not found", 404)
    workspace_id = getattr(target, "workspace_id", None)
    if not workspace_id:
        raise error("VALIDATION_ERROR", "Linked object has no workspace scope", 422)
    return workspace_id, project_id



def _validate_source_insights(db: Session, project: Project, insight_ids: list[str]) -> list[str]:
    """Confirm every referenced insight exists inside the same project.

    Stage 9 is the point where scattered observations become a named problem, so
    a dangling or cross-project insight id would silently break traceability
    back to the data that motivated the problem.
    """

    cleaned: list[str] = []
    for insight_id in insight_ids:
        insight = db.get(Insight, insight_id)
        if insight is None or insight.project_id != project.id:
            raise error("VALIDATION_ERROR", f"Insight {insight_id} is not part of this project", 422)
        if insight_id not in cleaned:
            cleaned.append(insight_id)
    return cleaned
