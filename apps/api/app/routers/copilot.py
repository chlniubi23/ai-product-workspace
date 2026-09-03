from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai_context import build_ai_context, extract_ai_insights
from ..auth import get_current_user
from ..common import error, model_dict, ok, page_params, paged, serialize
from ..db import get_db
from ..models import AIRun, CopilotMessage, CopilotSession, Insight, User, Workspace
from ..schemas import CopilotMessageCreate, CopilotSessionCreate
from ..services.access import _ensure_project_active, membership, project_for
from ..services.ai_stages import _deepseek_answer
from ..services.audit import audit
from ..services.workspace_settings import (
    _reject_ai_budget,
    _token_count,
    _workspace_ai_budget,
    _workspace_settings,
    _workspace_token_usage,
)

router = APIRouter()




@router.get("/api/v1/discussions")
def list_discussions(project_id: str | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db), pagination: tuple[int, int] = Depends(page_params)) -> dict[str, Any]:
    """Copilot sessions with their turn counts, for the stage-8 gate."""

    stmt = select(CopilotSession).where(CopilotSession.user_id == user.id)
    if project_id:
        project = project_for(db, user, project_id)
        stmt = stmt.where(CopilotSession.project_id == project.id)
    rows = db.scalars(stmt.order_by(CopilotSession.created_at.desc())).all()
    items = [
        model_dict(row, {"turn_count": len(row.messages), "stage": (row.page_context_json or {}).get("stage", "")})
        for row in rows
    ]
    return paged(items, *pagination, len(items))


@router.post("/api/v1/copilot/sessions")
def create_copilot_session(body: CopilotSessionCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    membership(db, user, body.workspace_id)
    if body.project_id:
        project_for(db, user, body.project_id)
    session = CopilotSession(workspace_id=body.workspace_id, project_id=body.project_id, user_id=user.id, page_context_json=body.page_context)
    db.add(session)
    db.flush()
    audit(db, body.workspace_id, user.id, "copilot.session_created", "copilot_session", session.id)
    db.commit()
    return ok(model_dict(session, {"messages": []}))


@router.post("/api/v1/copilot/sessions/{session_id}/messages")
async def copilot_message(session_id: str, body: CopilotMessageCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    session = db.get(CopilotSession, session_id)
    if session is None:
        raise error("NOT_FOUND", "Copilot session not found", 404)
    membership(db, user, session.workspace_id)
    if session.user_id != user.id:
        raise error("FORBIDDEN", "Copilot session belongs to another user", 403)
    workspace = db.get(Workspace, session.workspace_id)
    if workspace is None:
        raise error("NOT_FOUND", "Workspace not found", 404)
    # Serialize budget reservations per workspace where the database supports
    # row locks; SQLite safely treats this as a no-op for local development.
    workspace = db.scalar(select(Workspace).where(Workspace.id == session.workspace_id).with_for_update()) or workspace
    workspace_settings = _workspace_settings(workspace)
    budget = _workspace_ai_budget(workspace)
    # Persist and forward only the V1.1 allowlisted context.  Legacy callers may
    # still send page metadata/raw_rows; those fields are intentionally omitted.
    safe_question = str(body.content)[:4000]
    safe_context = build_ai_context(body.context, question=safe_question)
    user_message = CopilotMessage(session_id=session.id, role="user", content_json={"content": safe_question, "context": safe_context})
    db.add(user_message)
    db.flush()
    ai_run = AIRun(workspace_id=session.workspace_id, user_id=user.id, feature_name="copilot", provider="deepseek", model=budget["model"], request_fingerprint=hashlib.sha256(json.dumps({"question": safe_question, "context": safe_context}, ensure_ascii=True, sort_keys=True, default=str).encode()).hexdigest(), status="running", input_summary_json={"session_id": session.id, "question": safe_question, "context": safe_context})
    db.add(ai_run)
    db.flush()
    if not bool(workspace_settings.get("feature_flags", {}).get("copilot_enabled", True)):
        ai_run.status = "failed"
        ai_run.error_code = "AI_FEATURE_DISABLED"
        audit(db, session.workspace_id, user.id, "copilot.feature_disabled", "ai_run", ai_run.id, {"feature": "copilot"})
        db.commit()
        raise error("AI_FEATURE_DISABLED", "Copilot is disabled for this workspace", 403)
    reserved_daily = _workspace_token_usage(db, workspace, budget["per_request"])
    if reserved_daily > budget["daily"]:
        _reject_ai_budget(db, workspace, user, ai_run, budget, daily_used=reserved_daily, reason="Workspace daily AI token budget has been exhausted")
    db.commit()
    started = time.perf_counter()
    # The conversation is grounded in the insights the user already adopted.
    # They are loaded server-side from the session's project -- never from the
    # request body -- so a caller cannot choose which rows cross the boundary.
    insights_payload: list[dict[str, Any]] = []
    if session.project_id:
        confirmed_insights = db.scalars(
            select(Insight)
            .where(Insight.project_id == session.project_id, Insight.status == "confirmed")
            .order_by(Insight.created_at.desc())
            .limit(20)
        ).all()
        insights_payload = extract_ai_insights(confirmed_insights)
    copilot_context = {**safe_context, "project_id": session.project_id, "workspace_id": session.workspace_id, "user_id": user.id, "insights": insights_payload}
    answer, result_status, details = await _deepseek_answer(
        safe_question,
        copilot_context,
        db,
        user,
        session.workspace_id,
        {**workspace_settings, "ai_max_output_tokens": budget["max_output"], "ai_model_id": budget["model"]},
    )
    ai_run = db.get(AIRun, ai_run.id)
    ai_run.status = result_status
    ai_run.latency_ms = int((time.perf_counter() - started) * 1000)
    usage = details.get("usage", {})
    ai_run.prompt_tokens = _token_count(usage.get("prompt_tokens")) or None
    ai_run.completion_tokens = _token_count(usage.get("completion_tokens")) or None
    ai_run.error_code = details.get("error") if result_status == "failed" else None
    events = details.get("events")
    if not events:
        # Terminal SSE event must reflect the real outcome so clients can stop
        # spinners on failed/not-configured runs (BUG-002).
        terminal_type = "run.completed" if result_status == "succeeded" else "run.failed"
        terminal_data: dict[str, Any] = {"status": result_status}
        if details.get("error"):
            terminal_data["error"] = details.get("error")
        events = [{"type": terminal_type, "data": terminal_data}]
    bounded_answer = str(answer)[:6000]
    ai_run.input_summary_json = {"session_id": session.id, "question": safe_question, "context": build_ai_context(copilot_context, question=safe_question), "answer": bounded_answer, "structured_answer": details.get("structured_answer"), "events": events, "provider": {key: value for key, value in details.items() if key not in {"events", "structured_answer", "ai_run"}}}
    observed_tokens = _token_count(ai_run.prompt_tokens) + _token_count(ai_run.completion_tokens)
    if result_status == "succeeded":
        effective_tokens = observed_tokens or budget["per_request"]
        prior_daily = _workspace_token_usage(db, workspace, budget["per_request"], exclude_run_id=ai_run.id)
        if observed_tokens > budget["per_request"] or prior_daily + effective_tokens > budget["daily"]:
            _reject_ai_budget(db, workspace, user, ai_run, budget, daily_used=prior_daily + effective_tokens, observed_tokens=observed_tokens, reason="AI token budget exceeded for this workspace")
    ai_run.output_reference = ai_run.id
    assistant = CopilotMessage(session_id=session.id, role="assistant", content_json={"content": bounded_answer, "structured": details.get("structured_answer"), "status": result_status}, ai_run_id=ai_run.id)
    db.add(assistant)
    session.summary = bounded_answer[:1000]
    audit(db, session.workspace_id, user.id, "copilot.run", "ai_run", ai_run.id, {"status": result_status, "feature": "copilot", "prompt_tokens": _token_count(ai_run.prompt_tokens), "completion_tokens": _token_count(ai_run.completion_tokens), "total_tokens": observed_tokens})
    db.commit()
    return ok({"run_id": ai_run.id, "message_id": assistant.id, "status": ai_run.status})


def _sse(event_type: str, data: Any) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.get("/api/v1/copilot/runs/{run_id}/events")
async def copilot_events(run_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> StreamingResponse:
    run = db.get(AIRun, run_id)
    if run is None:
        raise error("NOT_FOUND", "Copilot run not found", 404)
    membership(db, user, run.workspace_id)
    if run.user_id != user.id:
        raise error("FORBIDDEN", "Copilot run belongs to another user", 403)
    events = (run.input_summary_json or {}).get("events", [])

    async def stream():
        for event in events:
            await asyncio.sleep(0)
            yield _sse(event.get("type", "message"), event.get("data", {}))
        yield ": keepalive\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/v1/copilot/runs/{run_id}")
def get_copilot_run(run_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(AIRun, run_id)
    if run is None:
        raise error("NOT_FOUND", "Copilot run not found", 404)
    membership(db, user, run.workspace_id)
    if run.user_id != user.id:
        raise error("FORBIDDEN", "Copilot run belongs to another user", 403)
    payload = run.input_summary_json or {}
    return ok({"id": run.id, "status": run.status, "feature_name": run.feature_name, "model": run.model, "answer": payload.get("answer"), "structured_answer": payload.get("structured_answer"), "events": payload.get("events", []), "created_at": serialize(run.created_at), "latency_ms": run.latency_ms, "prompt_tokens": run.prompt_tokens, "completion_tokens": run.completion_tokens, "error_code": run.error_code, "budget": payload.get("budget")})


@router.get("/api/v1/copilot/sessions/{session_id}")
def get_copilot_session(session_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    session = db.get(CopilotSession, session_id)
    if session is None:
        raise error("NOT_FOUND", "Copilot session not found", 404)
    membership(db, user, session.workspace_id)
    _ensure_project_active(db, session.project_id)
    if session.user_id != user.id:
        raise error("FORBIDDEN", "Copilot session belongs to another user", 403)
    return ok(model_dict(session, {"messages": [model_dict(message) for message in session.messages]}))
