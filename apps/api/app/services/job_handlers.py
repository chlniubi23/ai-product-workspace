from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai_context import REPORT_OUTPUT_SCHEMA, empty_report_output, validate_report_output
from ..common import _require_pandas, model_dict, pd
from ..config import settings
from ..db import SessionLocal
from ..infrastructure.jobs import JobContext, JobExecutionError, JobExecutor, JobResult
from ..models import (
    AnalysisArtifact,
    AnalysisRun,
    DatasetVersion,
    Document,
    DocumentVersion,
    FeedbackCluster,
    FeedbackClusterItem,
    FeedbackItem,
    FeedbackNote,
    Job,
    Project,
    User,
    Workspace,
    now,
)
from ..schemas import DocumentGenerate
from ..services.audit import audit
from .ai_stages import _run_ai_stage
from .analysis_pipeline import (
    _analysis_artifacts,
    _analysis_result_summary,
    _replace_quality_report,
    _replace_version_columns,
    _run_auto_analyses,
)
from .datasets import (
    _apply_cleaning,
    _column_schema,
    _job_storage_path,
    _quality_summary,
    _read_dataframe,
    _update_cleaning_operation_rows,
)
from .documents import (
    _build_document_context,
    _document_system_prompt,
    _render_ai_document_markdown,
    _render_document_markdown,
)

job_executor = JobExecutor(SessionLocal)



def _job(db: Session, workspace_id: str, job_type: str, input_json: dict[str, Any] | None = None, status_value: str = "queued", result_type: str | None = None, result_id: str | None = None) -> Job:
    payload = {"_retryable": True, **(input_json or {})}
    job = Job(workspace_id=workspace_id, job_type=job_type, status=status_value, progress=0, current_step="queued", input_json=payload, result_type=result_type, result_id=result_id, attempt_count=0)
    db.add(job)
    return job


def _job_payload(job: Job) -> dict[str, Any]:
    source = job.input_json if isinstance(job.input_json, dict) else {}
    public_input = {key: value for key, value in source.items() if not str(key).startswith("_")}
    can_retry = bool(source.get("_retryable", True)) and job.status in {"failed", "cancelled"} and job_executor.has_handler(job.job_type)
    return model_dict(job, {"input_json": public_input, "retryable": can_retry})


def _handle_dataset_parse(context: JobContext) -> JobResult:
    db = context.db
    payload = context.input
    version = db.get(DatasetVersion, payload.get("dataset_version_id"))
    if version is None:
        raise JobExecutionError("NOT_FOUND", "Dataset version not found", retryable=False)
    file_name = str(payload.get("file_name") or version.file_name)
    storage_path = str(payload.get("_storage_path") or version.storage_path)
    context.progress(10, "读取上传文件")
    frame = _read_dataframe(_job_storage_path(storage_path), file_name, payload.get("worksheet_name"))
    if len(frame) > settings.max_rows_per_dataset or len(frame.columns) > settings.max_columns_per_dataset:
        raise JobExecutionError("VALIDATION_ERROR", f"Dataset exceeds {settings.max_rows_per_dataset} rows or {settings.max_columns_per_dataset} columns", retryable=False)
    if len(frame.columns) == 0 or len(frame) == 0:
        raise JobExecutionError("VALIDATION_ERROR", "Dataset must contain a header row and at least one data row", retryable=False)
    context.progress(45, "检查数据质量")
    score, quality_status, summary = _quality_summary(frame)
    schema = _column_schema(frame)
    context.progress(75, "写入字段字典")
    version.row_count = len(frame)
    version.column_count = len(frame.columns)
    version.schema_json = {"columns": schema}
    version.status = "ready"
    _replace_version_columns(db, version, schema)
    _replace_quality_report(db, version, score, quality_status, summary)
    audit(db, version.dataset.project.workspace_id, payload.get("_actor_id"), "dataset.parsed", "dataset_version", version.id, {"quality_status": quality_status, "row_count": len(frame)})

    context.progress(85, "自动分析")
    auto = _run_auto_analyses(db, version, schema, frame, str(payload.get("_actor_id") or ""))

    db.commit()
    return JobResult(
        result_type="dataset_version",
        result_id=version.id,
        input_updates={
            "rows": len(frame),
            "columns": len(frame.columns),
            "auto_analysis_run_ids": auto["run_ids"],
            "auto_analysis_plan": auto["plan"],
        },
    )


def _handle_dataset_cleaning(context: JobContext) -> JobResult:
    db = context.db
    payload = context.input
    source = db.get(DatasetVersion, payload.get("source_version_id"))
    target = db.get(DatasetVersion, payload.get("target_version_id"))
    if source is None or target is None:
        raise JobExecutionError("NOT_FOUND", "Cleaning source or target version not found", retryable=False)
    context.progress(10, "读取原始版本")
    frame = _read_dataframe(_job_storage_path(source.storage_path), source.file_name)
    context.progress(35, "应用清洗规则")
    cleaned, summary = _apply_cleaning(frame, list(payload.get("operations") or []))
    if cleaned.empty:
        raise JobExecutionError("VALIDATION_ERROR", "Cleaning operations would remove every row; adjust the rules and retry", retryable=False)
    target_path = _job_storage_path(str(payload.get("_target_storage_path") or target.storage_path))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    cleaned.to_csv(target_path, index=False)
    context.progress(70, "生成清洗后版本")
    schema = _column_schema(cleaned)
    target.row_count = len(cleaned)
    target.column_count = len(cleaned.columns)
    target.file_size_bytes = target_path.stat().st_size
    target.fingerprint = hashlib.sha256(target_path.read_bytes()).hexdigest()
    target.schema_json = {"columns": schema, "parent_version_id": source.id, "cleaning_operations": payload.get("operations") or []}
    target.status = "ready"
    _replace_version_columns(db, target, schema)
    score, quality_status, quality = _quality_summary(cleaned)
    _replace_quality_report(db, target, score, quality_status, quality)
    _update_cleaning_operation_rows(
        db,
        payload,
        status="succeeded",
        summary=summary,
        source_row_count=len(frame),
        result_row_count=len(cleaned),
        result_fingerprint=target.fingerprint,
    )
    audit(db, target.dataset.project.workspace_id, payload.get("_actor_id"), "dataset.cleaning_applied", "dataset_version", target.id, {"source_version_id": source.id, "operations": payload.get("operations") or [], "summary": summary})
    db.commit()
    return JobResult(result_type="dataset_version", result_id=target.id, input_updates={"rows": len(cleaned), "cleaning_summary": summary})


def _handle_analysis(context: JobContext) -> JobResult:
    db = context.db
    payload = context.input
    run = db.get(AnalysisRun, payload.get("analysis_run_id"))
    version = db.get(DatasetVersion, payload.get("dataset_version_id"))
    if run is None or version is None:
        raise JobExecutionError("NOT_FOUND", "Analysis run or dataset version not found", retryable=False)
    run.status = "running"
    run.started_at = run.started_at or now()
    run.error_code = None
    db.commit()
    context.progress(12, "读取分析数据")
    frame = _read_dataframe(_job_storage_path(version.storage_path), version.file_name)
    context.progress(45, "执行确定性计算")
    analysis_config = dict(payload.get("config") or run.config_json or {})
    if isinstance(payload.get("field_mapping"), dict) and payload.get("field_mapping"):
        analysis_config["field_mapping"] = dict(payload["field_mapping"])
    artifacts = _analysis_artifacts(frame, version, str(payload.get("analysis_type") or run.analysis_type), analysis_config)
    for item in list(run.artifacts):
        db.delete(item)
    db.flush()
    for artifact in artifacts:
        db.add(AnalysisArtifact(analysis_run_id=run.id, artifact_type=artifact["artifact_type"], title=artifact["title"], payload_json=artifact.get("payload_json") or {}, fingerprint=hashlib.sha256(json.dumps(artifact.get("payload_json") or {}, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()))
    run.status = "succeeded"
    run.completed_at = now()
    run.result_summary = _analysis_result_summary(artifacts)
    context.progress(85, "保存分析产物")
    audit(db, run.workspace_id, payload.get("_actor_id"), "analysis.completed", "analysis_run", run.id, {"artifact_count": len(artifacts)})
    db.commit()
    return JobResult(result_type="analysis_run", result_id=run.id, input_updates={"artifact_count": len(artifacts)})


def _feedback_column(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    names = {str(column).strip().lower(): str(column) for column in frame.columns}
    for candidate in candidates:
        if candidate in names:
            return names[candidate]
    return None


def _handle_feedback_import(context: JobContext) -> JobResult:
    pd = _require_pandas()
    db = context.db
    payload = context.input
    project = db.get(Project, payload.get("project_id"))
    if project is None:
        raise JobExecutionError("NOT_FOUND", "Feedback project not found", retryable=False)
    frame = _read_dataframe(_job_storage_path(str(payload.get("_storage_path"))), str(payload.get("file_name") or "feedback.csv"))
    content_column = _feedback_column(frame, ("content", "feedback", "feedback_text", "text", "comment", "message", "review"))
    if content_column is None:
        content_column = next((str(column) for column in frame.columns if frame[column].dtype == "object"), None)
    if content_column is None:
        raise JobExecutionError("VALIDATION_ERROR", "Feedback file must contain a text column", retryable=False)
    user_column = _feedback_column(frame, ("user_id", "user_ref", "account_id", "customer_id"))
    channel_column = _feedback_column(frame, ("channel", "source", "platform"))
    rating_column = _feedback_column(frame, ("rating", "score", "stars"))
    date_column = _feedback_column(frame, ("feedback_at", "created_at", "date", "timestamp"))
    external_column = _feedback_column(frame, ("external_ref", "id", "ticket_id"))
    labels_column = _feedback_column(frame, ("labels", "label", "tags", "tag"))
    total = len(frame)
    created = 0
    for index, row in frame.iterrows():
        content = str(row.get(content_column) if row.get(content_column) is not None else "").strip()
        if not content or content.lower() == "nan":
            continue
        rating = None
        if rating_column:
            try:
                rating = float(row.get(rating_column))
            except (TypeError, ValueError):
                rating = None
        feedback_at = None
        if date_column:
            parsed = pd.to_datetime(row.get(date_column), errors="coerce")
            if pd.notna(parsed):
                feedback_at = parsed.to_pydatetime().replace(tzinfo=None)
        labels: list[str] = []
        if labels_column and row.get(labels_column) is not None:
            labels = [item.strip() for item in re.split(r"[,;|]", str(row.get(labels_column))) if item.strip() and item.lower() != "nan"]
        channel = str(row.get(channel_column))[:80] if channel_column and row.get(channel_column) is not None else "import"
        label = labels[0][:120] if labels else ""
        db.add(FeedbackItem(workspace_id=project.workspace_id, project_id=project.id, content=content, external_ref=str(row.get(external_column)) if external_column and row.get(external_column) is not None else None, user_ref=str(row.get(user_column)) if user_column and row.get(user_column) is not None else None, channel=channel, rating=rating, feedback_at=feedback_at, labels_json=labels, status="unreviewed"))
        # V1.1 keeps a single durable note shape.  The legacy row above is
        # retained temporarily so existing clients can finish their migration.
        db.add(FeedbackNote(project_id=project.id, content=content, source=channel, label=label, sentiment="unknown"))
        created += 1
        if created % 250 == 0:
            context.progress(min(90, 10 + int(index / max(1, total) * 80)), f"写入反馈 {created}/{total}")
    context.progress(95, "保存反馈导入结果")
    audit(db, project.workspace_id, payload.get("_actor_id"), "feedback.imported", "feedback_item", None, {"count": created, "job_id": context.job_id})
    db.commit()
    return JobResult(result_type="feedback_items", input_updates={"rows": created})


def _feedback_theme_key(content: str) -> str:
    tokens = re.findall(r"[\u4e00-\u9fff]{2,8}|[a-zA-Z]{3,}", content.lower())
    return tokens[0] if tokens else "其他反馈"


def _handle_feedback_clusters(context: JobContext) -> JobResult:
    db = context.db
    payload = context.input
    project = db.get(Project, payload.get("project_id"))
    if project is None:
        raise JobExecutionError("NOT_FOUND", "Feedback project not found", retryable=False)
    prior_ids = [str(value) for value in (payload.get("_created_cluster_ids") or []) if value]
    for cluster_id in prior_ids:
        cluster = db.get(FeedbackCluster, cluster_id)
        if cluster is not None and cluster.project_id == project.id and cluster.status == "draft":
            db.delete(cluster)
    db.flush()
    payload["_created_cluster_ids"] = []
    context.job.input_json = {**payload}
    db.commit()
    items = db.scalars(select(FeedbackItem).where(FeedbackItem.project_id == project.id).order_by(FeedbackItem.created_at)).all()
    groups: dict[str, list[FeedbackItem]] = {}
    for item in items:
        key = (item.labels_json or [None])[0] if item.labels_json else None
        key = str(key).strip() if key else _feedback_theme_key(item.content)
        groups.setdefault(key or "其他反馈", []).append(item)
    created_ids: list[str] = []
    for index, (key, grouped) in enumerate(sorted(groups.items(), key=lambda entry: (-len(entry[1]), entry[0]))[:12]):
        cluster = FeedbackCluster(workspace_id=project.workspace_id, project_id=project.id, name=key.title(), summary=f"共 {len(grouped)} 条反馈集中提及“{key}”，请结合样本进行人工确认。", sentiment="negative" if sum(1 for item in grouped if item.rating is not None and item.rating <= 2) > len(grouped) / 2 else "neutral", sample_count=len(grouped), evidence_json=[{"type": "feedback_item", "id": item.id} for item in grouped[:20]], status="draft")
        db.add(cluster)
        db.flush()
        created_ids.append(cluster.id)
        for item in grouped:
            db.add(FeedbackClusterItem(cluster_id=cluster.id, feedback_item_id=item.id, score=1.0))
            # Keep normalized notes aligned with the deterministic grouping.
            notes = db.scalars(select(FeedbackNote).where(FeedbackNote.project_id == project.id, FeedbackNote.content == item.content)).all()
            for note in notes:
                note.cluster_name = cluster.name[:255]
        context.progress(min(95, 20 + int((index + 1) / max(1, len(groups)) * 70)), f"归纳反馈主题 {index + 1}/{len(groups)}")
        payload["_created_cluster_ids"] = created_ids
        context.job.input_json = {**payload}
        db.commit()
    audit(db, project.workspace_id, payload.get("_actor_id"), "feedback.clusters_generated", "feedback_cluster", None, {"count": len(created_ids), "job_id": context.job_id})
    db.commit()
    return JobResult(result_type="feedback_clusters", input_updates={"cluster_ids": created_ids})


def _cleanup_feedback_cluster_attempt(db: Session, job: Job) -> None:
    payload = job.input_json if isinstance(job.input_json, dict) else {}
    project_id = payload.get("project_id")
    for cluster_id in payload.get("_created_cluster_ids") or []:
        cluster = db.get(FeedbackCluster, str(cluster_id))
        if cluster is not None and (not project_id or cluster.project_id == project_id) and cluster.status == "draft":
            db.delete(cluster)
    db.flush()


def _mark_feedback_clusters_failed(db: Session, job: Job, code: str, message: str) -> None:
    _cleanup_feedback_cluster_attempt(db, job)


def _mark_feedback_clusters_cancelled(db: Session, job: Job) -> None:
    _cleanup_feedback_cluster_attempt(db, job)


def _handle_document_generation(context: JobContext) -> JobResult:
    document = context.db.get(Document, context.input.get("document_id"))
    if document is None:
        raise JobExecutionError("NOT_FOUND", "Document not found", retryable=False)
    payload = context.input if isinstance(context.input, dict) else {}
    body = DocumentGenerate(
        project_id=str(payload.get("project_id") or document.project_id),
        document_type=str(payload.get("document_type") or document.document_type or "prd"),
        title=str(payload.get("title") or document.title),
        source_refs=list(payload.get("source_refs") or []),
        template_options=dict(payload.get("template_options") or {}),
    )
    user = context.db.get(User, payload.get("_actor_id"))
    if user is None:
        raise JobExecutionError("NOT_FOUND", "Generating user no longer exists", retryable=False)
    workspace = context.db.get(Workspace, document.workspace_id)
    if workspace is None:
        raise JobExecutionError("NOT_FOUND", "Workspace not found", retryable=False)

    context.progress(20, "装配证据上下文")
    doc_context = _build_document_context(body, context.db, user)

    context.progress(45, "AI 撰写文档")
    audience = str((doc_context["options"] or {}).get("audience") or "产品团队")[:120]
    # Sync handler on a worker thread: no ambient event loop exists here, so
    # asyncio.run() is safe (TestClient's inline background execution runs on
    # a threadpool thread as well).
    try:
        ai_result = asyncio.run(
            _run_ai_stage(
                db=context.db,
                user=user,
                workspace=workspace,
                feature_name="document_generation",
                system_prompt=_document_system_prompt(body.document_type, audience),
                context=doc_context["safe_context"],
                flag_name="document_generation_enabled",
                response_schema=REPORT_OUTPUT_SCHEMA,
                output_validator=validate_report_output,
                empty_output=dict(empty_report_output(limitation="AI provider is not configured.")),
                min_output_tokens=8192,
            )
        )
    except HTTPException as exc:
        # Budget rejection (429) or a feature flag flip mid-flight: the AIRun
        # bookkeeping already happened inside; fall back to the template.
        ai_result = {"status": "failed", "error_code": str(getattr(exc, "detail", {}).get("code") if isinstance(exc.detail, dict) else "AI_REJECTED"), "output": {}}

    context.progress(80, "渲染文档")
    if ai_result.get("status") == "succeeded":
        markdown = _render_ai_document_markdown(body, ai_result["output"], doc_context)
    else:
        # Deterministic fallback keeps the deliverable usable without a
        # provider; the reason is recorded on the audit trail.
        markdown, _ = _render_document_markdown(body, context.db, user)

    latest = context.db.scalar(select(DocumentVersion.version_number).where(DocumentVersion.document_id == document.id).order_by(DocumentVersion.version_number.desc()).limit(1)) or 0
    version = DocumentVersion(document_id=document.id, version_number=latest + 1, content_markdown=markdown, evidence_json=doc_context["evidence"], created_by=user.id)
    context.db.add(version)
    context.db.flush()
    document.current_version_id = version.id
    audit(
        context.db,
        document.workspace_id,
        user.id,
        "document.generation_completed",
        "document",
        document.id,
        {"job_id": context.job_id, "ai_status": ai_result.get("status"), "ai_error_code": ai_result.get("error_code"), "version": version.version_number},
    )
    context.db.commit()
    return JobResult(result_type="document", result_id=document.id)


def _mark_dataset_parse_failed(db: Session, job: Job, code: str, message: str) -> None:
    version_id = (job.input_json or {}).get("dataset_version_id")
    version = db.get(DatasetVersion, version_id) if version_id else None
    if version is not None:
        version.status = "failed"


def _mark_cleaning_failed(db: Session, job: Job, code: str, message: str) -> None:
    payload = job.input_json if isinstance(job.input_json, dict) else {}
    target_id = payload.get("target_version_id")
    target = db.get(DatasetVersion, target_id) if target_id else None
    if target is not None:
        target.status = "failed"
    _update_cleaning_operation_rows(db, payload, status="failed", error_code=code, error_message=message)


def _mark_cleaning_cancelled(db: Session, job: Job) -> None:
    payload = job.input_json if isinstance(job.input_json, dict) else {}
    target_id = payload.get("target_version_id")
    target = db.get(DatasetVersion, target_id) if target_id else None
    if target is not None:
        target.status = "cancelled"
    _update_cleaning_operation_rows(db, payload, status="cancelled")


def _mark_analysis_failed(db: Session, job: Job, code: str, message: str) -> None:
    run_id = (job.input_json or {}).get("analysis_run_id")
    run = db.get(AnalysisRun, run_id) if run_id else None
    if run is not None:
        run.status = "failed"
        run.error_code = code
        run.completed_at = now()


def _mark_document_failed(db: Session, job: Job, code: str, message: str) -> None:
    document_id = (job.input_json or {}).get("document_id")
    document = db.get(Document, document_id) if document_id else None
    if document is not None:
        document.status = "generation_failed"


def _register_job_handlers() -> None:
    registrations = {
        "dataset_parse": (_handle_dataset_parse, _mark_dataset_parse_failed, None),
        "dataset_cleaning": (_handle_dataset_cleaning, _mark_cleaning_failed, _mark_cleaning_cancelled),
        "analysis_run": (_handle_analysis, _mark_analysis_failed, None),
        "feedback_import": (_handle_feedback_import, None, None),
        "feedback_cluster_generation": (_handle_feedback_clusters, _mark_feedback_clusters_failed, _mark_feedback_clusters_cancelled),
        "document_generation": (_handle_document_generation, _mark_document_failed, None),
    }
    for job_type, (handler, on_failure, on_cancel) in registrations.items():
        if not job_executor.has_handler(job_type):
            job_executor.register(job_type, handler, on_failure=on_failure, on_cancel=on_cancel)


def _feedback_payload(item: FeedbackItem) -> dict[str, Any]:
    # Clients consume the documented ``labels`` field, not the storage column
    # name ``labels_json`` (BUG-021).
    return model_dict(item, {"labels": list(item.labels_json or [])})
