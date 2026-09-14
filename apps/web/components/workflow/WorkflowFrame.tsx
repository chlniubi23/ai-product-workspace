"use client";

import Link from "next/link";
import { ArrowLeft, CircleAlert, Info, Sparkles } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { workflowSteps } from "@/lib/navigation";
import {
  STAGE_COUNT,
  formatWorkflowDate,
  isAiStage,
  loadWorkflowSnapshot,
  stepCompletion,
  type StageCompletion,
  type WorkflowSnapshot,
} from "@/lib/workflow";

export function useWorkflowSnapshot() {
  const [snapshot, setSnapshot] = useState<WorkflowSnapshot>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const next = await loadWorkflowSnapshot();
      setSnapshot(next);
      setError(next.loadErrors.join("；"));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "工作流状态加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Switching the active project (batch 9) re-scopes every list the snapshot
  // carries; mounted stage pages re-read it instead of doing a full reload.
  useEffect(() => {
    const handler = () => void refresh();
    window.addEventListener("apw-project-changed", handler);
    return () => window.removeEventListener("apw-project-changed", handler);
  }, [refresh]);
  const completion: StageCompletion = snapshot
    ? stepCompletion(snapshot)
    : Array.from({ length: STAGE_COUNT }, () => false);
  return { snapshot, loading, error, refresh, completion };
}

export function WorkflowHeader({
  step,
  title,
  description,
  completion,
  loading,
  children,
}: {
  step: number;
  title: string;
  description: string;
  completion: boolean[];
  loading?: boolean;
  children?: React.ReactNode;
}) {
  const completedCount = completion.filter(Boolean).length;
  const progress = Math.round((completedCount / STAGE_COUNT) * 100);
  const ai = isAiStage(step);
  return (
    <>
      <div className={`workflow-header ${ai ? "ai" : "compute"}`}>
        <div className="workflow-header-main">
          <div className="workflow-eyebrow">
            <span>第 {step} 步</span>
            <span className="workflow-mode">{ai ? "AI 辅助" : "确定性计算"}</span>
          </div>
          <h1>
            {title}
            {ai && <Sparkles size={15} className="workflow-ai-mark" aria-hidden="true" />}
          </h1>
          <p>{description}</p>
        </div>
        <div className="workflow-header-side">
          <strong>{progress}%</strong>
          <span>流水线完成度</span>
          {loading && <small>同步中…</small>}
          {children}
        </div>
      </div>
      {ai && (
        <div className="ai-disclosure workflow-page-disclosure" role="note">
          <Sparkles size={14} />
          以下内容由 AI 生成，需人工确认后才能使用
        </div>
      )}
    </>
  );
}

export function WorkflowGate({
  step,
  completion,
  loading,
  children,
}: {
  step: number;
  completion: boolean[];
  loading?: boolean;
  children: React.ReactNode;
}) {
  const missing = completion.slice(0, Math.max(0, step - 1)).findIndex((value) => !value);
  if (loading)
    // Batch 27: shimmer skeleton replaces the text loading card.
    return (
      <section
        className="card workflow-loading-card"
        style={{ alignItems: "stretch", textAlign: "left" }}
        aria-busy="true"
      >
        <div style={{ display: "grid", gap: 10, width: "100%", maxWidth: 420, margin: "0 auto" }}>
          <div className="skeleton" style={{ width: "40%", height: 16 }} />
          <div className="skeleton" style={{ height: 12 }} />
          <div className="skeleton" style={{ width: "80%", height: 12 }} />
        </div>
      </section>
    );
  if (missing < 0) return <>{children}</>;
  const missingStep = workflowSteps[missing];
  // Advisory, not blocking.  Completion of a later stage can depend on an AI
  // conversation (stage 8) or an approval that a single user may never need, so
  // hiding the page would deadlock the pipeline exactly like the old stage-2
  // field-mapping gate did.  Evidence contracts stay enforced server-side.
  return (
    <>
      <section className="card workflow-advisory" role="note">
        <div className="workflow-advisory-icon">
          <Info size={17} />
        </div>
        <div className="workflow-advisory-copy">
          <strong>建议先完成：{missingStep.label}</strong>
          <p>当前页面仍可操作；缺少前置产出时证据链可能不完整，提交时服务端将校验证据引用。</p>
          <Link className="btn btn-subtle btn-sm" href={missingStep.href}>
            <ArrowLeft size={13} />
            前往 {missingStep.label}
          </Link>
        </div>
      </section>
      {children}
    </>
  );
}

export function EvidenceStatus({ count, required = true }: { count: number; required?: boolean }) {
  if (!required) return null;
  return (
    <span className={`tag ${count > 0 ? "tag-green" : "tag-rose"}`}>
      <CircleAlert size={11} />
      {count > 0 ? (
        <>
          <span className="num">{count}</span> 条证据
        </>
      ) : (
        "缺少证据引用"
      )}
    </span>
  );
}

export function SnapshotMeta({ snapshot }: { snapshot?: WorkflowSnapshot }) {
  if (!snapshot) return null;
  return (
    <div className="workflow-snapshot-meta">
      最近同步：{formatWorkflowDate(snapshot.loadedAt)} · 数据集 {snapshot.datasets.length} · 分析运行{" "}
      {snapshot.analysisRuns.length}
    </div>
  );
}
