"""Batch 10: auto-report split into instant compute + job-based narration.

``POST .../auto-report/compute`` must be AI-free (no AIRun, no job) and persist
a complete deterministic report as ``not_configured`` in one fast request.
``POST /auto-reports/{id}/narrate`` queues an ``auto_report_narration`` job:
success upgrades the report to ``succeeded`` with the deterministic sections
kept in front of the AI interpretation; any degraded outcome keeps the
deterministic body untouched and only records ``error_code``.  The legacy
combined endpoint stays compute + synchronous narration.
"""

from __future__ import annotations

from io import BytesIO

from conftest import auth, data_of, error_of
from sqlalchemy import select

from app import db as database
from app.models import AIRun, Job
from app.services.job_handlers import job_executor

CSV_EVENTS = (
    "user_id,event_time,event_name,channel\n"
    + "".join(
        f"u_{i % 20:03d},2026-08-{(i % 28) + 1:02d}T10:00:00Z,page_view,{'organic' if i % 2 else 'paid'}\n"
        for i in range(40)
    )
)
CSV_METRICS = (
    "date,dau,new_users\n"
    + "".join(f"2026-08-{(i % 28) + 1:02d},{1000 + i},{50 + i}\n" for i in range(28))
)


def _batch(client, user, project_id: str, files: list[tuple[str, bytes]]):
    payload = {"project_id": project_id}
    return client.post(
        "/api/v1/datasets/upload-batch",
        headers=auth(user),
        data=payload,
        files=[("files", (name, BytesIO(content), "text/csv")) for name, content in files],
    )


def _compute(client, user, project_id: str):
    return client.post(f"/api/v1/projects/{project_id}/auto-report/compute", headers=auth(user))


def _narrate(client, user, report_id: str):
    return client.post(f"/api/v1/auto-reports/{report_id}/narrate", headers=auth(user))


def _get_report(client, user, report_id: str) -> dict:
    return data_of(client.get(f"/api/v1/auto-reports/{report_id}", headers=auth(user)))


def _upload_two(client, owner, project):
    _batch(
        client,
        owner,
        project["id"],
        [("events.csv", CSV_EVENTS.encode()), ("metrics.csv", CSV_METRICS.encode())],
    )


# --------------------------------------------------------------------------
# compute: instant, deterministic, AI-free
# --------------------------------------------------------------------------


def test_compute_is_deterministic_and_never_touches_ai(client, owner, project):
    _upload_two(client, owner, project)
    with database.SessionLocal() as db:
        ai_before = len(db.scalars(select(AIRun)).all())
        jobs_before = len(db.scalars(select(Job)).all())

    payload = data_of(_compute(client, owner, project["id"]))
    report = payload["report"]
    assert payload["status"] == "not_configured"
    assert report["status"] == "not_configured"
    assert report["error_code"] is None
    assert len(report["dataset_version_ids"]) == 2
    assert report["deterministic_json"]["datasets"]
    assert not report["deterministic_json"]["read_failures"]
    headings = [section["heading"] for section in report["sections_json"]]
    assert any("数据概况" in heading for heading in headings)
    assert report["key_findings"], "deterministic findings must not be empty"
    assert "40" in report["content_markdown"]

    # Instant means AI-free: not a single AIRun or job may appear.
    with database.SessionLocal() as db:
        assert len(db.scalars(select(AIRun)).all()) == ai_before
        assert len(db.scalars(select(Job)).all()) == jobs_before


def test_compute_always_creates_a_new_report(client, owner, project):
    _upload_two(client, owner, project)
    first = data_of(_compute(client, owner, project["id"]))["report"]
    second = data_of(_compute(client, owner, project["id"]))["report"]
    assert first["id"] != second["id"]


# --------------------------------------------------------------------------
# narrate: job-based, deterministic body preserved on any degraded outcome
# --------------------------------------------------------------------------


def test_narrate_without_provider_keeps_deterministic_body(client, owner, project):
    _upload_two(client, owner, project)
    computed = data_of(_compute(client, owner, project["id"]))["report"]

    payload = data_of(_narrate(client, owner, computed["id"]))
    assert payload["job"]["id"]

    fetched = _get_report(client, owner, computed["id"])
    assert fetched["status"] == "not_configured"
    assert fetched["error_code"] == "LLM_NOT_CONFIGURED"
    assert fetched["content_markdown"] == computed["content_markdown"], "deterministic body untouched"
    with database.SessionLocal() as db:
        run = db.scalar(select(AIRun).where(AIRun.feature_name == "auto_report_narration"))
        assert run is not None
        assert run.status == "not_configured"
        assert run.error_code == "LLM_NOT_CONFIGURED"


def test_narrate_success_keeps_deterministic_sections_first(client, owner, project, monkeypatch):
    class _FakeAdapter:
        configured = True

        async def complete(self, *, messages, response_schema, request_metadata):
            from app.infrastructure.llm.deepseek import LlmResult

            return LlmResult(
                structured={
                    "title": "整体解读标题",
                    "summary": "AI 概述：数据整体健康。",
                    "sections": [{"heading": "AI 解读", "content": "- 关键点一"}],
                    "key_findings": ["发现一"],
                    "recommendations": ["建议一"],
                    "limitations": ["自动选列"],
                },
                finish_reason="stop",
                prompt_tokens=900,
                completion_tokens=700,
            )

    import app.services.ai_stages as ai_stages

    monkeypatch.setattr(ai_stages, "DeepSeekAdapter", lambda _settings: _FakeAdapter())

    _upload_two(client, owner, project)
    computed = data_of(_compute(client, owner, project["id"]))["report"]
    data_of(_narrate(client, owner, computed["id"]))

    fetched = _get_report(client, owner, computed["id"])
    assert fetched["status"] == "succeeded"
    assert fetched["error_code"] is None
    headings = [section["heading"] for section in fetched["sections_json"]]
    assert any("数据概况" in heading for heading in headings), "deterministic profile kept in front"
    assert "AI 解读" in headings
    markdown = fetched["markdown"]
    assert markdown.index("数据概况") < markdown.index("AI 解读")
    assert fetched["ai_run_id"]
    with database.SessionLocal() as db:
        run = db.scalar(
            select(AIRun)
            .where(AIRun.feature_name == "auto_report_narration")
            .order_by(AIRun.created_at.desc())
            .limit(1)
        )
        assert run is not None
        assert run.status == "succeeded"
        assert (run.prompt_tokens or 0) > 0


def test_narrate_rejects_succeeded_confirmed_and_in_flight(client, owner, project, monkeypatch):
    class _FakeAdapter:
        configured = True

        async def complete(self, *, messages, response_schema, request_metadata):
            from app.infrastructure.llm.deepseek import LlmResult

            return LlmResult(
                structured={
                    "title": "AI 标题",
                    "summary": "AI 概述。",
                    "sections": [{"heading": "AI 解读", "content": "- 要点"}],
                    "key_findings": ["发现"],
                    "recommendations": ["建议"],
                    "limitations": ["局限"],
                },
                finish_reason="stop",
                prompt_tokens=100,
                completion_tokens=100,
            )

    import app.services.ai_stages as ai_stages

    monkeypatch.setattr(ai_stages, "DeepSeekAdapter", lambda _settings: _FakeAdapter())

    _upload_two(client, owner, project)
    narrated = data_of(_compute(client, owner, project["id"]))["report"]
    data_of(_narrate(client, owner, narrated["id"]))
    assert _get_report(client, owner, narrated["id"])["status"] == "succeeded"
    again = error_of(_narrate(client, owner, narrated["id"]))
    assert again["code"] == "REPORT_ALREADY_NARRATED"

    confirmed = data_of(_compute(client, owner, project["id"]))["report"]
    data_of(client.post(f"/api/v1/auto-reports/{confirmed['id']}/confirm", headers=auth(owner)))
    assert error_of(_narrate(client, owner, confirmed["id"]))["code"] == "REPORT_ALREADY_NARRATED"

    pending = data_of(_compute(client, owner, project["id"]))["report"]
    with database.SessionLocal() as db:
        # A queued job (with a real actor) blocks a second narrate.  The next
        # test's app startup will replay this row via recover_pending(), which
        # is exactly the recovery semantics the job system promises.
        db.add(
            Job(
                workspace_id=owner["workspace"]["id"],
                job_type="auto_report_narration",
                status="queued",
                progress=0,
                current_step="queued",
                input_json={"report_id": pending["id"], "_actor_id": owner["user"]["id"]},
            )
        )
        db.commit()
    assert error_of(_narrate(client, owner, pending["id"]))["code"] == "NARRATION_IN_PROGRESS"


def test_narrate_valve_rejection_records_budget_error_without_spend(client, owner, project):
    _upload_two(client, owner, project)
    computed = data_of(_compute(client, owner, project["id"]))["report"]
    patch = client.patch(
        f"/api/v1/workspaces/{owner['workspace']['id']}/settings",
        headers=auth(owner),
        json={"ai_daily_token_budget": 100, "ai_per_request_token_budget": 50, "ai_max_output_tokens": 40},
    )
    assert patch.status_code == 200, patch.text

    data_of(_narrate(client, owner, computed["id"]))
    fetched = _get_report(client, owner, computed["id"])
    assert fetched["status"] == "not_configured"
    assert fetched["error_code"] == "AI_BUDGET_EXCEEDED"
    assert fetched["content_markdown"] == computed["content_markdown"]
    with database.SessionLocal() as db:
        run = db.scalar(
            select(AIRun)
            .where(AIRun.feature_name == "auto_report_narration")
            .order_by(AIRun.created_at.desc())
            .limit(1)
        )
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "AI_BUDGET_EXCEEDED"
        # Pre-call valve: the refusal costs zero tokens.
        assert run.prompt_tokens is None and run.completion_tokens is None


def test_narration_handler_is_registered_for_recover(client, owner, project):
    assert job_executor.has_handler("auto_report_narration")


def test_narration_job_id_exposed_in_payloads_across_lifecycle(client, owner, project, monkeypatch):
    """Batch 16: the payload carries the in-flight narration job id so the web
    client can resume polling; it is null whenever no job is queued/running."""

    _upload_two(client, owner, project)
    computed = data_of(_compute(client, owner, project["id"]))["report"]
    assert computed["narration_job_id"] is None

    # An in-flight (queued) job shows up on both the detail and list payloads.
    with database.SessionLocal() as db:
        job = Job(
            workspace_id=owner["workspace"]["id"],
            job_type="auto_report_narration",
            status="running",
            progress=10,
            current_step="AI 解读报告",
            input_json={"report_id": computed["id"], "_actor_id": owner["user"]["id"]},
        )
        db.add(job)
        db.commit()
        job_id = job.id

    listed = data_of(client.get(f"/api/v1/projects/{project['id']}/auto-reports", headers=auth(owner)))
    row = next(item for item in listed if item["id"] == computed["id"])
    assert row["narration_job_id"] == job_id
    assert _get_report(client, owner, computed["id"])["narration_job_id"] == job_id

    # Terminal job: back to null.
    with database.SessionLocal() as db:
        stored = db.get(Job, job_id)
        stored.status = "failed"
        stored.error_code = "LLM_PROVIDER_ERROR"
        db.commit()
    assert _get_report(client, owner, computed["id"])["narration_job_id"] is None

    # A completed narrate run (fake adapter success) also leaves it null.
    class _FakeAdapter:
        configured = True

        async def complete(self, *, messages, response_schema, request_metadata):
            from app.infrastructure.llm.deepseek import LlmResult

            return LlmResult(
                structured={
                    "title": "生命周期",
                    "summary": "概述。",
                    "sections": [{"heading": "AI 解读", "content": "- 要点"}],
                    "key_findings": ["发现"],
                    "recommendations": ["建议"],
                    "limitations": ["局限"],
                },
                finish_reason="stop",
                prompt_tokens=100,
                completion_tokens=100,
            )

    import app.services.ai_stages as ai_stages

    monkeypatch.setattr(ai_stages, "DeepSeekAdapter", lambda _settings: _FakeAdapter())
    data_of(_narrate(client, owner, computed["id"]))
    assert _get_report(client, owner, computed["id"])["narration_job_id"] is None


def test_narrate_enforces_roles(client, owner, viewer, outsider, project):
    _upload_two(client, owner, project)
    computed = data_of(_compute(client, owner, project["id"]))["report"]
    assert error_of(_narrate(client, viewer, computed["id"]))["code"] == "FORBIDDEN"
    assert error_of(_narrate(client, outsider, computed["id"]))["code"] in {"FORBIDDEN", "NOT_FOUND"}


# --------------------------------------------------------------------------
# legacy combined endpoint: compute + synchronous narration
# --------------------------------------------------------------------------


def test_legacy_combined_endpoint_degrades_without_provider(client, owner, project):
    _upload_two(client, owner, project)
    payload = data_of(client.post(f"/api/v1/projects/{project['id']}/auto-report", headers=auth(owner)))
    assert payload["status"] == "not_configured"
    assert payload["report"]["status"] == "not_configured"
    assert payload["report"]["error_code"] == "LLM_NOT_CONFIGURED"
    assert payload["report"]["content_markdown"]
    assert payload["report"]["deterministic_json"]["datasets"]


def test_legacy_combined_endpoint_succeeds_with_provider(client, owner, project, monkeypatch):
    class _FakeAdapter:
        configured = True

        async def complete(self, *, messages, response_schema, request_metadata):
            from app.infrastructure.llm.deepseek import LlmResult

            return LlmResult(
                structured={
                    "title": "组合端点标题",
                    "summary": "组合端点 AI 概述。",
                    "sections": [{"heading": "AI 解读", "content": "- 要点"}],
                    "key_findings": ["发现"],
                    "recommendations": ["建议"],
                    "limitations": ["局限"],
                },
                finish_reason="stop",
                prompt_tokens=800,
                completion_tokens=600,
            )

    import app.services.ai_stages as ai_stages

    monkeypatch.setattr(ai_stages, "DeepSeekAdapter", lambda _settings: _FakeAdapter())

    _upload_two(client, owner, project)
    payload = data_of(client.post(f"/api/v1/projects/{project['id']}/auto-report", headers=auth(owner)))
    report = payload["report"]
    assert payload["status"] == "succeeded"
    assert report["status"] == "succeeded"
    headings = [section["heading"] for section in report["sections_json"]]
    assert any("数据概况" in heading for heading in headings)
    assert "AI 解读" in headings
    assert payload["usage"]["prompt_tokens"] > 0
