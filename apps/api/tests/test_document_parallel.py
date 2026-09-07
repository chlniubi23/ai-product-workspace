"""Batch 25: two-wave parallel sections + coherence harmonize pass.

The routing adapter dispatches by system-prompt markers instead of a FIFO
queue -- parallel section calls arrive in arbitrary order, so a queue would
hand section N another section's scripted reply.  Concurrency is observed by
counting in-flight ``complete()`` calls and capping the semaphore at 3.
"""

from __future__ import annotations

import asyncio
import json
import time

from conftest import auth, data_of
from sqlalchemy import select

from app import db as database
from app.models import AIRun, Job
from app.services.job_handlers import JobContext

# ---------------------------------------------------------------------------
# routing fake adapter
# ---------------------------------------------------------------------------


class _RoutingAdapter:
    """Dispatches scripted results by prompt markers; records call order,
    concurrency peak and per-call timings."""

    configured = True

    def __init__(
        self,
        *,
        outline_sections: int = 0,
        section_delay: float = 0.0,
        section_content: str | None = None,
        fail_section_headings: set[str] | None = None,
        harmonize_rewrite: str | None = None,
        harmonize_drop_heading: bool = False,
        harmonize_fail: bool = False,
    ):
        from app.infrastructure.llm.deepseek import LlmResult

        self._llm = LlmResult
        self.outline_sections = outline_sections
        self.section_delay = section_delay
        self.section_content = section_content
        self.fail_section_headings = fail_section_headings or set()
        self.harmonize_rewrite = harmonize_rewrite
        self.harmonize_drop_heading = harmonize_drop_heading
        self.harmonize_fail = harmonize_fail
        self.calls: list[dict] = []
        self.active = 0
        self.peak = 0
        self.sequence = 0

    def _result(self, structured=None, content=None):
        return self._llm(structured=structured, content=content, finish_reason="stop", prompt_tokens=100, completion_tokens=200)

    async def complete(self, *, messages, response_schema, request_metadata):
        system = messages[0].content
        user = messages[1].content
        self.sequence += 1
        start = self.sequence
        record = {"system": system, "user": user, "start": start}
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if "文档架构师" in system:
                result = self._result(
                    structured={
                        "findings": [
                            {"id": "finding-1", "title": "注册转化缺口 14.3%", "evidence_hint": "漏斗", "severity": "高"}
                        ],
                        "sections": [
                            {"heading": f"第{i}节", "purpose": f"写透第{i}节"} for i in range(1, self.outline_sections + 1)
                        ],
                        "root_cause": "升级引导缺失导致转化缺口。",
                    }
                )
            elif "文档主编" in system:
                if self.harmonize_fail:
                    result = self._result(content="not json at all")
                else:
                    payload = json.loads(user)
                    headings = [item["heading"] for item in payload.get("sections") or []]
                    if self.harmonize_drop_heading:
                        headings = headings[:-1]
                    sections = [
                        {"heading": heading, "content": f"{self.harmonize_rewrite or ''}{heading} 校对后正文。"}
                        for heading in headings
                    ]
                    result = self._result(structured={"sections": sections})
            elif "撰写助手" in system:  # single-pass fallback
                result = self._result(
                    structured={
                        "title": "单次路径",
                        "summary": "单次生成。",
                        "sections": [{"heading": "需求背景", "content": "单次正文。" * 20}],
                        "key_findings": [],
                        "recommendations": [],
                        "limitations": [],
                    }
                )
            else:  # one section call: heading is quoted in 「」 after 第 N/M 节
                heading = system.split("节「", 1)[1].split("」", 1)[0]
                record["heading"] = heading
                if self.section_delay:
                    await asyncio.sleep(self.section_delay)
                if heading in self.fail_section_headings:
                    result = self._result(content="not json at all")
                else:
                    content = self.section_content or (f"{heading} 正文。" + "设计要点与数据依据充分展开。 " * 40)
                    result = self._result(structured={"heading": heading, "content": content})
            self.calls.append(record)
            return result
        finally:
            self.active -= 1
            record["end"] = self.sequence + 1000  # ordering marker only


def _patch(monkeypatch, fake: _RoutingAdapter) -> None:
    import app.services.ai_stages as ai_stages

    monkeypatch.setattr(ai_stages, "DeepSeekAdapter", lambda _settings: fake)


def _generate_prd(client, owner, project_id: str, insight_id: str, title: str = "并行 PRD") -> dict:
    created = data_of(
        client.post(
            "/api/v1/ai/draft-document",
            headers=auth(owner),
            json={
                "project_id": project_id,
                "document_type": "prd",
                "title": title,
                "source_refs": [{"type": "insight", "id": insight_id}],
            },
        )
    )
    return data_of(client.get(f"/api/v1/documents/{created['document']['id']}", headers=auth(owner)))


def _prepare(client, owner, project) -> str:
    from test_document_generation import confirmed_insight, make_ready

    ready = make_ready(client, owner, project)
    insight = confirmed_insight(client, owner, ready)
    return insight["id"]


# ---------------------------------------------------------------------------
# parallel scheduling
# ---------------------------------------------------------------------------


def test_wave2_sections_run_in_parallel_with_cap_of_three(client, owner, project, monkeypatch):
    insight_id = _prepare(client, owner, project)
    # 10 sections, prd: wave1 = sections 1-2 sequential, wave2 = 8 parallel.
    # 0.5s each: sequential would take 5s, two-wave takes ~2 + ceil(8/3)*0.5.
    fake = _RoutingAdapter(outline_sections=10, section_delay=0.5)
    _patch(monkeypatch, fake)

    started = time.perf_counter()
    document = _generate_prd(client, owner, project["id"], insight_id)
    elapsed = time.perf_counter() - started

    assert fake.peak <= 3, "semaphore must cap concurrency at 3"
    assert elapsed < 4.2, f"sections did not run in parallel (took {elapsed:.1f}s)"
    # The rendered markdown carries all ten headings in outline order.
    markdown = document["current_version"]["content_markdown"]
    positions = [markdown.index(f"## 第{i}节") for i in range(1, 11)]
    assert positions == sorted(positions), "sections must land in outline order"


def test_wave1_narrative_sections_are_sequential(client, owner, project, monkeypatch):
    insight_id = _prepare(client, owner, project)
    fake = _RoutingAdapter(outline_sections=4, section_delay=0.2)
    _patch(monkeypatch, fake)

    _generate_prd(client, owner, project["id"], insight_id)

    section_calls = [call for call in fake.calls if call.get("heading")]
    by_heading = {call["heading"]: call for call in section_calls}
    # Sections 1 and 2 form the sequential narrative wave: 2 starts only
    # after 1 finished (their call-record positions are strictly ordered).
    assert by_heading["第1节"]["start"] < by_heading["第2节"]["start"]
    # Wave-2 prompts carry the outline plan instead of a written summary.
    wave2 = [call for call in fake.calls if call.get("heading") in {"第3节", "第4节"}]
    assert wave2
    assert all("不得与其他章节重复" in call["system"] and "大纲全文" in call["system"] for call in wave2)
    assert all("已写前文摘要" not in call["system"] for call in wave2)
    # The rolling summary still feeds wave-1's second section.
    assert "已写前文摘要" in by_heading["第2节"]["system"]


# ---------------------------------------------------------------------------
# coherence harmonize pass
# ---------------------------------------------------------------------------


def test_harmonize_rewrites_when_heading_set_is_preserved(client, owner, project, monkeypatch):
    insight_id = _prepare(client, owner, project)
    long_body = "表格 |数字| 保留。数据依据充分展开。" * 260  # > 6000 chars per section -> pass triggers
    fake = _RoutingAdapter(outline_sections=3, section_content=long_body, harmonize_rewrite="【校对】")
    _patch(monkeypatch, fake)

    document = _generate_prd(client, owner, project["id"], insight_id)

    harmonize_calls = [call for call in fake.calls if "文档主编" in call["system"]]
    assert len(harmonize_calls) == 1
    markdown = document["current_version"]["content_markdown"]
    assert "【校对】" in markdown, "harmonized text must replace the draft"
    assert document["current_version"]["ai_status"] == "succeeded"
    with database.SessionLocal() as db:
        runs = db.scalars(
            select(AIRun).where(
                AIRun.workspace_id == owner["workspace"]["id"],
                AIRun.feature_name == "document_harmonize",
            )
        ).all()
        assert len(runs) == 1
        assert runs[0].status == "succeeded"


def test_harmonize_failure_keeps_pre_harmonize_draft(client, owner, project, monkeypatch):
    insight_id = _prepare(client, owner, project)
    long_body = "数据依据充分展开，保留表格与数字。" * 260
    fake = _RoutingAdapter(outline_sections=3, section_content=long_body, harmonize_fail=True)
    _patch(monkeypatch, fake)

    document = _generate_prd(client, owner, project["id"], insight_id)

    assert document["current_version"]["ai_status"] == "succeeded"
    assert document["current_version"]["ai_error_code"] is None
    assert "数据依据充分展开" in document["current_version"]["content_markdown"]


def test_harmonize_output_with_changed_headings_is_rejected(client, owner, project, monkeypatch):
    insight_id = _prepare(client, owner, project)
    long_body = "数据依据充分展开，保留表格与数字。" * 260
    fake = _RoutingAdapter(outline_sections=3, section_content=long_body, harmonize_drop_heading=True)
    _patch(monkeypatch, fake)

    document = _generate_prd(client, owner, project["id"], insight_id)

    # The heading contract was broken -> the pre-harmonize draft survives.
    assert "数据依据充分展开" in document["current_version"]["content_markdown"]


def test_long_documents_harmonize_in_batches(client, owner, project, monkeypatch):
    insight_id = _prepare(client, owner, project)
    long_body = "数据依据充分展开，保留表格与数字。" * 260
    fake = _RoutingAdapter(outline_sections=10, section_content=long_body, harmonize_rewrite="【校对】")
    _patch(monkeypatch, fake)

    document = _generate_prd(client, owner, project["id"], insight_id)

    # 10 sections -> batches of 6 + 4, each its own provider call and AIRun.
    harmonize_calls = [call for call in fake.calls if "文档主编" in call["system"]]
    assert len(harmonize_calls) == 2
    first_batch = json.loads(harmonize_calls[0]["user"])
    second_batch = json.loads(harmonize_calls[1]["user"])
    assert len(first_batch["sections"]) == 6 and len(second_batch["sections"]) == 4
    assert len(first_batch["document_headings"]) == 10, "each batch sees the full chapter order"
    with database.SessionLocal() as db:
        runs = db.scalars(
            select(AIRun).where(
                AIRun.workspace_id == owner["workspace"]["id"],
                AIRun.feature_name == "document_harmonize",
            )
        ).all()
        assert len(runs) == 2
    markdown = document["current_version"]["content_markdown"]
    assert markdown.count("【校对】") >= 1
    assert document["current_version"]["ai_status"] == "succeeded"


def test_short_document_skips_harmonize(client, owner, project, monkeypatch):
    insight_id = _prepare(client, owner, project)
    fake = _RoutingAdapter(outline_sections=2)  # ~1.3k chars each, below 6000
    _patch(monkeypatch, fake)

    _generate_prd(client, owner, project["id"], insight_id)

    assert not [call for call in fake.calls if "文档主编" in call["system"]]
    with database.SessionLocal() as db:
        assert (
            db.scalars(
                select(AIRun).where(
                    AIRun.workspace_id == owner["workspace"]["id"],
                    AIRun.feature_name == "document_harmonize",
                )
            ).all()
            == []
        )


# ---------------------------------------------------------------------------
# outline slim context + degradation + AIRun accounting
# ---------------------------------------------------------------------------


def test_outline_call_uses_summary_layer_not_full_aggregates(client, owner, project, monkeypatch):
    insight_id = _prepare(client, owner, project)
    fake = _RoutingAdapter(outline_sections=2)
    _patch(monkeypatch, fake)

    _generate_prd(client, owner, project["id"], insight_id)

    outline_call = next(call for call in fake.calls if "文档架构师" in call["system"])
    payload = json.loads(outline_call["user"])
    assert set(payload) <= {"goal", "question", "findings", "solution", "decision", "field_labels"}
    assert "artifacts" not in payload


def test_wave2_single_section_failure_degrades_only_that_section(client, owner, project, monkeypatch):
    insight_id = _prepare(client, owner, project)
    fake = _RoutingAdapter(outline_sections=4, fail_section_headings={"第3节"})
    _patch(monkeypatch, fake)

    document = _generate_prd(client, owner, project["id"], insight_id)

    markdown = document["current_version"]["content_markdown"]
    assert "## 第3节" in markdown  # section still present (outline bullets)
    assert "## 第4节" in markdown and "第4节 正文" in markdown  # siblings unaffected
    assert document["current_version"]["ai_status"] == "succeeded"


def test_airun_rows_are_one_per_call(client, owner, project, monkeypatch):
    insight_id = _prepare(client, owner, project)
    fake = _RoutingAdapter(outline_sections=3, section_content="短正文但足够长以通过校验。" * 6)
    _patch(monkeypatch, fake)

    _generate_prd(client, owner, project["id"], insight_id)

    with database.SessionLocal() as db:
        features = [
            run.feature_name
            for run in db.scalars(
                select(AIRun).where(AIRun.workspace_id == owner["workspace"]["id"]).order_by(AIRun.created_at)
            ).all()
        ]
    assert features.count("document_outline") == 1
    assert features.count("document_section") == 3


def test_progress_sequence_is_monotonic_and_reaches_harmonize_band(client, owner, project, monkeypatch):
    insight_id = _prepare(client, owner, project)
    long_body = "数据依据充分展开，保留表格与数字。" * 260
    fake = _RoutingAdapter(outline_sections=3, section_content=long_body, harmonize_rewrite="【校对】")
    _patch(monkeypatch, fake)

    progress_log: list[tuple[int, str]] = []
    original = JobContext.progress

    def spy(self, value: int, step: str):
        progress_log.append((value, step))
        return original(self, value, step)

    monkeypatch.setattr(JobContext, "progress", spy)
    _generate_prd(client, owner, project["id"], insight_id)

    values = [value for value, _ in progress_log]
    assert values == sorted(values), "progress must be monotonic"
    steps = [step for _, step in progress_log]
    assert any("正在撰写" in step for step in steps)
    assert any("连贯校对" in step for step in steps), "harmonize band must appear in the progress log"
    assert values[-1] == 95  # finish band (executor stamps 100 on success)


# ---------------------------------------------------------------------------
# payload progress exposure
# ---------------------------------------------------------------------------


def test_payloads_expose_live_progress_for_inflight_jobs(client, owner, project):
    insight_id = _prepare(client, owner, project)
    created = data_of(
        client.post(
            "/api/v1/ai/draft-document",
            headers=auth(owner),
            json={
                "project_id": project["id"],
                "document_type": "prd",
                "title": "进行中文档",
                "source_refs": [{"type": "insight", "id": insight_id}],
            },
        )
    )
    document_id = created["document"]["id"]
    with database.SessionLocal() as db:
        db.add(
            Job(
                workspace_id=owner["workspace"]["id"],
                job_type="document_generation",
                status="running",
                progress=42,
                current_step="正在撰写 第 3/10 节：数据与埋点",
                input_json={"document_id": document_id, "project_id": project["id"]},
            )
        )
        db.commit()

    payload = data_of(client.get(f"/api/v1/documents/{document_id}", headers=auth(owner)))
    assert payload["generation_job_id"]
    assert payload["generation_progress"] == {"progress": 42, "current_step": "正在撰写 第 3/10 节：数据与埋点"}

    # Terminal state (job row deleted from the active window): null progress.
    with database.SessionLocal() as db:
        job = db.scalar(select(Job).where(Job.job_type == "document_generation", Job.status == "running"))
        job.status = "succeeded"
        db.commit()
    payload = data_of(client.get(f"/api/v1/documents/{document_id}", headers=auth(owner)))
    assert payload["generation_progress"] is None


def test_report_payload_exposes_narration_progress(client, owner, project):
    from test_document_generation import make_ready

    make_ready(client, owner, project)
    computed = data_of(
        client.post(f"/api/v1/projects/{project['id']}/auto-report/compute", headers=auth(owner))
    )["report"]
    with database.SessionLocal() as db:
        db.add(
            Job(
                workspace_id=owner["workspace"]["id"],
                job_type="auto_report_narration",
                status="running",
                progress=25,
                current_step="AI 解读报告",
                input_json={"report_id": computed["id"], "_actor_id": owner["user"]["id"]},
            )
        )
        db.commit()

    payload = data_of(client.get(f"/api/v1/auto-reports/{computed['id']}", headers=auth(owner)))
    assert payload["narration_job_id"]
    assert payload["narration_progress"] == {"progress": 25, "current_step": "AI 解读报告"}

    with database.SessionLocal() as db:
        job = db.scalar(select(Job).where(Job.job_type == "auto_report_narration", Job.status == "running"))
        job.status = "succeeded"
        db.commit()
    payload = data_of(client.get(f"/api/v1/auto-reports/{computed['id']}", headers=auth(owner)))
    assert payload["narration_progress"] is None
