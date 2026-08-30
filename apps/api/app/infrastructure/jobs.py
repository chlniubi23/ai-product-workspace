from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..models import AuditLog, Job

TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled"}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class JobCancelled(RuntimeError):
    """Raised cooperatively when a running job has been cancelled."""


class JobExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class JobResult:
    result_type: str | None = None
    result_id: str | None = None
    input_updates: dict[str, Any] | None = None


class JobContext:
    """Database-backed progress and cancellation context passed to handlers."""

    def __init__(self, db: Session, job_id: str) -> None:
        self.db = db
        self.job_id = job_id

    @property
    def job(self) -> Job:
        job = self.db.get(Job, self.job_id)
        if job is None:
            raise JobExecutionError("JOB_NOT_FOUND", "Job no longer exists", retryable=False)
        return job

    @property
    def input(self) -> dict[str, Any]:
        value = self.job.input_json
        return dict(value) if isinstance(value, dict) else {}

    def check_cancelled(self) -> None:
        job = self.job
        self.db.refresh(job)
        if job.status == "cancelled":
            raise JobCancelled("Job was cancelled")

    def progress(self, value: int, step: str) -> None:
        job = self.job
        self.db.refresh(job)
        if job.status == "cancelled":
            raise JobCancelled("Job was cancelled")
        job.progress = max(0, min(99, int(value)))
        job.current_step = step[:255]
        self.db.commit()


class JobHandler(Protocol):
    def __call__(self, context: JobContext) -> JobResult | None:
        ...


FailureHandler = Callable[[Session, Job, str, str], None]
CancellationHandler = Callable[[Session, Job], None]


@dataclass(frozen=True)
class _RegisteredHandler:
    run: JobHandler
    on_failure: FailureHandler | None = None
    on_cancel: CancellationHandler | None = None


class JobExecutor:
    """Small in-process executor with persistent state and replayable inputs.

    Handlers receive their own database session and never depend on a FastAPI
    request object. Replacing this class with a queue-backed implementation does
    not require changing handler contracts.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._handlers: dict[str, _RegisteredHandler] = {}
        self._active: set[str] = set()
        self._active_lock = asyncio.Lock()
        self._recovery_tasks: set[asyncio.Task[None]] = set()

    def register(
        self,
        job_type: str,
        handler: JobHandler,
        *,
        on_failure: FailureHandler | None = None,
        on_cancel: CancellationHandler | None = None,
    ) -> None:
        if job_type in self._handlers:
            raise ValueError(f"A handler is already registered for {job_type}")
        self._handlers[job_type] = _RegisteredHandler(handler, on_failure, on_cancel)

    def has_handler(self, job_type: str) -> bool:
        return job_type in self._handlers

    def schedule(self, background_tasks: Any, job_id: str) -> None:
        background_tasks.add_task(self.enqueue, job_id)

    async def enqueue(self, job_id: str) -> None:
        async with self._active_lock:
            if job_id in self._active:
                return
            self._active.add(job_id)
        try:
            await asyncio.to_thread(self._run_sync, job_id)
        finally:
            async with self._active_lock:
                self._active.discard(job_id)

    async def recover_pending(self) -> None:
        """Replay committed queued jobs and interrupted running jobs at startup."""

        with self._session_factory() as db:
            jobs = db.scalars(select(Job).where(Job.status.in_(("queued", "running")))).all()
            job_ids: list[str] = []
            for job in jobs:
                if job.job_type not in self._handlers:
                    self._fail_without_handler(job)
                    continue
                if job.status == "running":
                    job.status = "queued"
                    job.progress = 0
                    job.current_step = "recovered_after_restart"
                    job.started_at = None
                    job.completed_at = None
                job_ids.append(job.id)
            db.commit()

        for job_id in job_ids:
            task = asyncio.create_task(self.enqueue(job_id))
            self._recovery_tasks.add(task)
            task.add_done_callback(self._recovery_tasks.discard)

    def mark_cancelled(self, db: Session, job: Job) -> None:
        registered = self._handlers.get(job.job_type)
        job.status = "cancelled"
        job.current_step = "cancelled"
        job.completed_at = _now()
        if registered and registered.on_cancel:
            registered.on_cancel(db, job)

    def _run_sync(self, job_id: str) -> None:
        with self._session_factory() as db:
            job = db.get(Job, job_id)
            if job is None or job.status != "queued":
                return
            registered = self._handlers.get(job.job_type)
            if registered is None:
                self._fail_without_handler(job)
                db.commit()
                return

            job.status = "running"
            job.progress = 1
            job.current_step = "starting"
            job.error_code = None
            job.error_message = None
            job.attempt_count = int(job.attempt_count or 0) + 1
            job.started_at = _now()
            job.completed_at = None
            self._audit(db, job, "job.started", {"attempt_count": job.attempt_count})
            db.commit()

            context = JobContext(db, job_id)
            try:
                result = registered.run(context) or JobResult()
                context.check_cancelled()
                job = context.job
                if result.input_updates:
                    job.input_json = {**context.input, **result.input_updates}
                if result.result_type is not None:
                    job.result_type = result.result_type
                if result.result_id is not None:
                    job.result_id = result.result_id
                job.status = "succeeded"
                job.progress = 100
                job.current_step = "completed"
                job.error_code = None
                job.error_message = None
                job.completed_at = _now()
                self._audit(db, job, "job.succeeded", {"attempt_count": job.attempt_count})
                db.commit()
            except JobCancelled:
                db.rollback()
                self._persist_cancelled(job_id, registered)
            except Exception as exc:
                db.rollback()
                self._persist_failure(job_id, registered, exc)

    def _persist_cancelled(self, job_id: str, registered: _RegisteredHandler) -> None:
        with self._session_factory() as db:
            job = db.get(Job, job_id)
            if job is None:
                return
            job.status = "cancelled"
            job.current_step = "cancelled"
            job.completed_at = job.completed_at or _now()
            if registered.on_cancel:
                registered.on_cancel(db, job)
            self._audit(db, job, "job.cancelled", {"attempt_count": job.attempt_count})
            db.commit()

    def _persist_failure(self, job_id: str, registered: _RegisteredHandler, exc: Exception) -> None:
        with self._session_factory() as db:
            job = db.get(Job, job_id)
            if job is None:
                return
            if job.status == "cancelled":
                if registered.on_cancel:
                    registered.on_cancel(db, job)
                db.commit()
                return

            code, message, retryable = self._error_details(exc)
            job.status = "failed"
            job.current_step = "failed"
            job.error_code = code[:80]
            job.error_message = message[:4000]
            job.completed_at = _now()
            job.input_json = {**(job.input_json or {}), "_retryable": retryable}
            if registered.on_failure:
                registered.on_failure(db, job, code, message)
            self._audit(db, job, "job.failed", {"error_code": code, "retryable": retryable})
            db.commit()

    @staticmethod
    def _error_details(exc: Exception) -> tuple[str, str, bool]:
        if isinstance(exc, JobExecutionError):
            return exc.code, str(exc), exc.retryable
        detail = getattr(exc, "detail", None)
        if isinstance(detail, dict):
            code = str(detail.get("code") or "JOB_EXECUTION_FAILED")
            message = str(detail.get("message") or exc)
            status_code = int(getattr(exc, "status_code", 500) or 500)
            return code, message, status_code >= 500
        return "JOB_EXECUTION_FAILED", str(exc) or exc.__class__.__name__, True

    @staticmethod
    def _audit(db: Session, job: Job, action: str, detail: dict[str, Any]) -> None:
        source = job.input_json if isinstance(job.input_json, dict) else {}
        db.add(
            AuditLog(
                workspace_id=job.workspace_id,
                actor_type="system",
                actor_id=None,
                action=action,
                target_type="job",
                target_id=job.id,
                detail_json={**detail, "job_type": job.job_type, "initiated_by": source.get("_actor_id")},
            )
        )

    @staticmethod
    def _fail_without_handler(job: Job) -> None:
        job.status = "failed"
        job.progress = 0
        job.current_step = "failed"
        job.error_code = "JOB_HANDLER_NOT_FOUND"
        job.error_message = f"No handler is registered for job type {job.job_type}"
        job.input_json = {**(job.input_json or {}), "_retryable": False}
        job.completed_at = _now()
