"""批 38：AI 质量评估 harness —— 真实 key 下跑完整链路并产出基线指标。

默认跳过：仅当 ``APW_EVAL=1`` **且** 仓库根目录 ``.env`` 存在真实
``DEEPSEEK_API_KEY`` 时运行（conftest 默认清空 key，本文件显式注入，
仅作用于本次评估运行）。对 3 份 golden 文件执行
上传 → compute → narrate → 采访一轮 → 蒸馏，汇总指标写入
``output/eval/eval-report.json``（output/ 已 gitignore）并 print 摘要。

断言刻意从宽：只断言流程跑通与报告文件生成，不断言质量阈值——首批
是建立基线，供后续提示词/模型变更做量化对比。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from conftest import auth, data_of

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden"
EVAL_FILES = ["01_核心产品指标日报.csv", "03_功能模块使用周报.csv", "07_AB测试结果.csv"]


def _real_key() -> str:
    env = REPO_ROOT / ".env"
    if not env.exists():
        return ""
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("DEEPSEEK_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


pytestmark = pytest.mark.skipif(
    os.getenv("APW_EVAL") != "1" or not _real_key(),
    reason="eval harness runs only with APW_EVAL=1 and a real DEEPSEEK_API_KEY in the repo .env",
)


def _enable_real_key(monkeypatch) -> None:
    """conftest 清空了 DEEPSEEK_API_KEY；评估运行时注入真实 key。"""

    import app.config as config
    import app.infrastructure.llm.deepseek as deepseek_module

    real = _real_key()
    monkeypatch.setattr(config.settings, "deepseek_api_key", real)
    if hasattr(deepseek_module, "DeepSeekSettings"):
        monkeypatch.setattr(
            deepseek_module.DeepSeekSettings,
            "from_app_settings",
            staticmethod(lambda _settings: deepseek_module.DeepSeekSettings(api_key=real)),
        )


def test_eval_pipeline_baseline(client, owner, monkeypatch):
    _enable_real_key(monkeypatch)

    # ---- 项目 + 上传 3 份 golden 文件 ----
    project = data_of(
        client.post(
            "/api/v1/projects",
            headers=auth(owner),
            json={"workspace_id": owner["workspace"]["id"], "name": "eval-baseline"},
        )
    )
    for name in EVAL_FILES:
        content = (GOLDEN / name).read_bytes()
        response = client.post(
            "/api/v1/datasets/upload",
            headers=auth(owner),
            data={"project_id": project["id"]},
            files={"file": (name, BytesIO(content), "text/csv")},
        )
        assert response.status_code == 200, response.text

    # ---- compute + narrate ----
    computed = data_of(client.post(f"/api/v1/projects/{project['id']}/auto-report/compute", headers=auth(owner)))
    report = computed["report"]
    data_of(client.post(f"/api/v1/auto-reports/{report['id']}/narrate", headers=auth(owner)))
    fetched = data_of(client.get(f"/api/v1/auto-reports/{report['id']}", headers=auth(owner)))
    assert fetched["status"] == "succeeded"
    narration_fact_check = (fetched.get("deterministic_json") or {}).get("fact_check") or {}

    # ---- 采访一轮（手动回答一条即可蒸馏）----
    data_of(
        client.post(
            "/api/v1/interview-questions",
            headers=auth(owner),
            json={
                "project_id": project["id"],
                "topic": "评估",
                "question_text": "哪个指标最值得验证？",
                "answer_text": "留存率下滑最值得验证，建议对比活动前后。",
            },
        )
    )
    distilled = data_of(
        client.post("/api/v1/ai/distill-interview", headers=auth(owner), json={"project_id": project["id"]})
    )
    distill_fact_check = distilled.get("fact_check") or {}

    # ---- 汇总指标 ----
    insights = distilled.get("created") or []
    evidence_hit = sum(1 for item in insights if item.get("evidence_json")) if insights else 0
    total_tokens = 0
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import AIRun

    with SessionLocal() as db:
        runs = db.scalars(select(AIRun).where(AIRun.workspace_id == owner["workspace"]["id"])).all()
        total_tokens = sum((run.prompt_tokens or 0) + (run.completion_tokens or 0) for run in runs)
    manual_review = []
    for dataset in (fetched.get("deterministic_json") or {}).get("datasets") or []:
        for column in dataset.get("metrics") or []:
            if isinstance(column, dict) and column.get("stat_note"):
                manual_review.append(
                    {"dataset": dataset.get("name"), "column": column.get("name"), "stat_note": column.get("stat_note")}
                )

    metrics = {
        "generated_at": datetime.now(UTC).isoformat(),
        "files": EVAL_FILES,
        "fact_check": {"narration": narration_fact_check, "distill": distill_fact_check},
        "evidence_hit_rate": round(evidence_hit / len(insights), 4) if insights else None,
        "token_per_conclusion": round(total_tokens / len(insights), 1) if insights else None,
        "manual_review": manual_review,
        "insights_created": len(insights),
    }
    out_dir = REPO_ROOT / "output" / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "eval-report.json"
    out_file.write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        "EVAL SUMMARY:",
        json.dumps(
            {k: metrics[k] for k in ("fact_check", "evidence_hit_rate", "token_per_conclusion")},
            ensure_ascii=False,
        ),
    )

    assert out_file.exists()
    assert narration_fact_check or distill_fact_check  # 至少一条链路产出了校验结果
