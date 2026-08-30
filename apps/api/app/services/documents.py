from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..common import error, model_dict, serialize
from ..models import (
    AnalysisArtifact,
    AnalysisRun,
    DecisionProposal,
    Document,
    DocumentVersion,
    FeedbackCluster,
    Insight,
    User,
    now,
)
from ..schemas import DocumentGenerate
from ..services.access import project_for
from .evidence import _require_confirmed_insight_refs


def _document_payload(document: Document, db: Session) -> dict[str, Any]:
    versions = db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document.id).order_by(DocumentVersion.version_number)).all()
    current = next((version for version in versions if version.id == document.current_version_id), versions[-1] if versions else None)
    return model_dict(document, {"current_version": model_dict(current) if current else None, "versions": [model_dict(version) for version in versions]})


def _render_document_markdown(body: DocumentGenerate, db: Session, user: User) -> tuple[str, list[dict[str, Any]]]:
    project = project_for(db, user, body.project_id)
    _require_confirmed_insight_refs(db, project.workspace_id, body.source_refs, project.id)
    evidence: list[dict[str, Any]] = []
    dataset_version_ids: set[str] = set()
    analysis_run_ids: set[str] = set()
    generation_timestamp = serialize(now())
    options = body.template_options if isinstance(body.template_options, dict) else {}
    period_label = str(options.get("period_label") or options.get("period") or "Current period")[:120]
    audience = str(options.get("audience") or "Product team")[:120]
    include_evidence = bool(options.get("include_evidence", True))
    include_acceptance = bool(options.get("include_acceptance_criteria", True))
    include_tracking = bool(options.get("include_tracking_plan", False))
    include_risks = bool(options.get("include_risks", True))
    sections = [f"# {body.title}", "", f"_Draft for {audience} | {period_label}_", ""]
    evidence_sections: list[str] = []
    for reference in body.source_refs:
        if not isinstance(reference, dict):
            raise error("VALIDATION_ERROR", "Document source references must be objects", 400)
        ref_type, ref_id = reference.get("type"), reference.get("id")
        if not ref_type or not ref_id:
            raise error("VALIDATION_ERROR", "Document source references require type and id", 400)
        ref_type = str(ref_type).strip().lower().replace("-", "_")
        safe_reference = {"type": str(ref_type)[:80], "id": str(ref_id)[:120]}
        evidence.append(safe_reference)
        item: Any = None
        if ref_type == "insight":
            item = db.get(Insight, ref_id)
            if item and (item.workspace_id != project.workspace_id or item.project_id != project.id):
                item = None
            if item is not None and item.status != "confirmed":
                raise error("INSIGHT_NOT_CONFIRMED", "Only confirmed insights can be referenced by a document", 409, {"insight_id": str(ref_id), "status": item.status})
            if item:
                evidence_sections.extend([f"### Insight: {item.title}", "", item.content, ""])
        elif ref_type == "decision_proposal":
            item = db.get(DecisionProposal, ref_id)
            if item and (item.workspace_id != project.workspace_id or item.project_id != project.id):
                item = None
            if item:
                evidence_sections.extend([f"### Decision proposal: {item.title}", "", f"- Problem: {item.problem_statement}", f"- Action: {item.proposed_action}", f"- Validation: {item.validation_plan}", ""])
        elif ref_type == "analysis_artifact":
            item = db.get(AnalysisArtifact, ref_id)
            analysis_run = db.get(AnalysisRun, item.analysis_run_id) if item else None
            if item and (analysis_run is None or analysis_run.workspace_id != project.workspace_id or analysis_run.project_id != project.id):
                item = None
            if item:
                analysis_run_ids.add(str(item.analysis_run_id))
                analysis_run = db.get(AnalysisRun, item.analysis_run_id)
                if analysis_run is not None:
                    dataset_version_ids.add(str(analysis_run.dataset_version_id))
                evidence_sections.extend([f"### Analysis: {item.title}", "", "```json", json.dumps(item.payload_json, ensure_ascii=False, indent=2, default=str), "```", ""])
        elif ref_type == "feedback_cluster":
            item = db.get(FeedbackCluster, ref_id)
            if item and (item.workspace_id != project.workspace_id or item.project_id != project.id):
                item = None
            if item:
                evidence_sections.extend([f"### Feedback theme: {item.name}", "", item.summary, ""])
        if item is None:
            raise error("VALIDATION_ERROR", f"Document source reference {ref_type} '{ref_id}' was not found in this project", 400)

        # A confirmed insight can point at an artifact/run/version. Include
        # those upstream identifiers in the immutable evidence manifest too.
        if ref_type == "insight" and isinstance(item.evidence_json, list):
            for nested in item.evidence_json:
                if not isinstance(nested, dict):
                    continue
                nested_type = str(nested.get("type") or "").strip().lower().replace("-", "_")
                nested_id = str(nested.get("id") or "")
                if not nested_id:
                    continue
                if nested_type in {"dataset_version", "data_version"}:
                    dataset_version_ids.add(nested_id)
                elif nested_type in {"analysis_run", "analysis"}:
                    analysis_run_ids.add(nested_id)
                elif nested_type in {"analysis_artifact", "artifact"}:
                    artifact = db.get(AnalysisArtifact, nested_id)
                    if artifact is not None:
                        analysis_run_ids.add(str(artifact.analysis_run_id))
                        run = db.get(AnalysisRun, artifact.analysis_run_id)
                        if run is not None:
                            dataset_version_ids.add(str(run.dataset_version_id))

    if body.document_type == "weekly_report":
        sections.extend([
            "## This period",
            "",
            "Draft summary of confirmed metrics, insights, feedback and completed work.",
            "",
            "## Key changes",
            "",
            "- Confirmed changes and metric movement: pending review.",
            "",
            "## Core issues and feedback",
            "",
            "- Prioritize issues supported by the evidence below.",
            "",
            "## Completed work",
            "",
            "- Confirm completed tasks before publishing this report.",
            "",
            "## Next period plan",
            "",
            "- Convert approved decisions into owned tasks with due dates.",
            "",
        ])
        if include_risks:
            sections.extend(["## Risks and open questions", "", "- Items without evidence remain open questions.", ""])
    elif body.document_type == "retrospective":
        sections.extend([
            "## Background and goal",
            "",
            "Describe the product context, intended outcome and review period.",
            "",
            "## Facts and outcomes",
            "",
            "Separate observed results from interpretation.",
            "",
            "## Root-cause hypotheses",
            "",
            "Record hypotheses with the evidence needed to validate them.",
            "",
            "## Decisions and improvements",
            "",
            "List approved actions, owners and validation plans.",
            "",
            "## Follow-up",
            "",
            "- Add follow-up tasks and due dates before the retrospective is finalized.",
            "",
        ])
        if include_risks:
            sections.extend(["## Risks and unresolved items", "", "- Mark unresolved assumptions explicitly.", ""])
    else:
        sections.extend([
            "## Requirement background",
            "",
            "Describe the user problem and the evidence that motivates this draft.",
            "",
            "## Problem and evidence",
            "",
            "Summarize the confirmed problem, affected users and supporting evidence.",
            "",
            "## Goals and non-goals",
            "",
            "- Goals: define the outcome this proposal should achieve.",
            "- Non-goals: record explicitly excluded scope.",
            "",
            "## Target users and scenarios",
            "",
            "Describe the target user, scenario and expected value.",
            "",
            "## Feature scope",
            "",
            "Describe the in-scope functionality and explicit exclusions.",
            "",
            "## User flow",
            "",
            "Describe the primary user steps and important decision points.",
            "",
            "## Page and interaction",
            "",
            "Describe page states, inputs, outputs and interaction requirements.",
            "",
            "## Data and tracking",
            "",
            "Define the metric dictionary entries and events needed to evaluate the change.",
            "",
        ])
        if include_tracking:
            tracking_events = options.get("tracking_events") or options.get("events") or []
            if isinstance(tracking_events, list) and tracking_events:
                sections.extend(["### Tracking plan", "", *[f"- {str(event)[:240]}" for event in tracking_events[:30]], ""])
            else:
                sections.extend(["### Tracking plan", "", "- Add event names, properties and success metrics before implementation.", ""])
        if include_acceptance:
            sections.extend(["## Acceptance criteria", "", "- The user flow is testable with explicit inputs and expected outputs.", "- Results are tied to approved evidence and the relevant dataset version.", ""])
        if include_risks:
            sections.extend(["## Risks and open questions", "", "- Record rollout risks, dependencies and items awaiting confirmation.", ""])

    if include_evidence:
        sections.extend(["## Evidence", "", *evidence_sections])
    sections.extend(
        [
            "## Evidence manifest",
            "",
            f"- Generated at: {generation_timestamp}",
            f"- Dataset version IDs: {', '.join(sorted(dataset_version_ids)) or 'none'}",
            f"- Analysis run IDs: {', '.join(sorted(analysis_run_ids)) or 'none'}",
            f"- Source refs: {json.dumps(evidence, ensure_ascii=False, sort_keys=True)}",
            "",
        ]
    )
    sections.extend(["## Draft status", "", "This document is a draft. Human editing, evidence review and explicit confirmation are required before publication.", ""])
    return "\n".join(sections), evidence
