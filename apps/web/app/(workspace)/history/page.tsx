"use client";

import Link from "next/link";
import { ArchiveRestore, ChevronRight, History, Trash2 } from "lucide-react";
import { useState } from "react";
import { apiRequest } from "@/lib/api";
import { SnapshotMeta, useWorkflowSnapshot } from "@/components/workflow/WorkflowFrame";
import { getActiveProjectId, setActiveProjectId, formatWorkflowDate } from "@/lib/workflow";

export default function HistoryPage() {
  const { snapshot, loading, error, refresh } = useWorkflowSnapshot();
  const [restoringId, setRestoringId] = useState("");
  // Batch 26 delete flow: a confirmation overlay (existing modal style) --
  // irreversible cascade delete must never be a single click.
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const archived = snapshot?.archivedProjects || [];

  async function restore(projectId: string) {
    if (restoringId) return;
    setRestoringId(projectId);
    try {
      await apiRequest(`/projects/${projectId}/unarchive`, { method: "POST" });
      await refresh();
    } catch {
      /* 恢复失败时保留状态；错误会在下次刷新的 loadErrors 中可见 */
    } finally {
      setRestoringId("");
    }
  }

  async function remove(projectId: string) {
    if (deleting || !deleteTarget) return;
    setDeleting(true);
    setDeleteError("");
    try {
      // Owner-only cascade delete: clears every artifact and uploaded file,
      // irreversible by design -- hence the overlay confirmation.
      await apiRequest(`/projects/${projectId}?confirm=${projectId}`, { method: "DELETE" });
      if (getActiveProjectId() === projectId) {
        // The deleted project can no longer be the active scope; clearing the
        // key lets the snapshot fall back to the first active project.
        setActiveProjectId(null);
      }
      setDeleteTarget(null);
      await refresh();
    } catch (cause) {
      setDeleteError(cause instanceof Error ? cause.message : "删除失败");
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="page">
      <header className="workflow-header" style={{ marginBottom: 4 }}>
        <div className="workflow-header-main">
          <div className="workflow-eyebrow">
            <span>历史</span>
          </div>
          <h1>历史工作流</h1>
          <p>已归档项目在此只读回看；恢复后可继续编辑。</p>
        </div>
      </header>
      <SnapshotMeta snapshot={snapshot} />
      {error && (
        <div className="form-error" role="alert">
          {error}
        </div>
      )}
      {deleteError && (
        <div className="form-error" role="alert" style={{ marginTop: 10 }}>
          {deleteError}
        </div>
      )}
      {loading ? (
        <section
          className="card"
          style={{ marginTop: 16, padding: 24, display: "grid", gap: 12 }}
          aria-busy="true"
        >
          <div className="skeleton" style={{ width: "40%", height: 16 }} />
          <div className="skeleton" style={{ height: 12 }} />
          <div className="skeleton" style={{ width: "80%", height: 12 }} />
        </section>
      ) : archived.length === 0 ? (
        <section className="card empty-state" style={{ marginTop: 16 }}>
          <History size={20} />
          <strong>暂无归档项目</strong>
          <p>在交付页生成文档并完成归档后，项目将显示在此处。</p>
        </section>
      ) : (
        <div className="grid grid-2" style={{ marginTop: 16 }}>
          {archived.map((project) => (
            <section className="card card-pad" key={project.id}>
              <div className="card-head">
                <div>
                  <strong>{project.name || "未命名工作流"}</strong>
                  <div className="card-kicker">归档于 {formatWorkflowDate(project.archived_at)}</div>
                </div>
                <span className="tag tag-slate">已归档</span>
              </div>
              {project.goal_statement && (
                <p style={{ color: "var(--muted)", lineHeight: 1.6 }}>
                  {project.goal_statement.slice(0, 120)}
                </p>
              )}
              <div
                style={{
                  display: "flex",
                  gap: 8,
                  justifyContent: "flex-end",
                  marginTop: 10,
                  flexWrap: "wrap",
                }}
              >
                <button
                  className="btn btn-subtle btn-sm"
                  disabled={restoringId === project.id}
                  onClick={() => void restore(project.id)}
                >
                  <ArchiveRestore size={13} />
                  恢复到活跃
                </button>
                <button
                  className="btn btn-danger btn-sm"
                  disabled={deleting}
                  onClick={() => {
                    setDeleteError("");
                    setDeleteTarget({ id: project.id, name: project.name || "未命名工作流" });
                  }}
                >
                  <Trash2 size={13} />
                  删除
                </button>
                <Link className="btn btn-primary btn-sm" href={`/history/${project.id}`}>
                  回看产出 <ChevronRight size={13} />
                </Link>
              </div>
            </section>
          ))}
        </div>
      )}
      {deleteTarget && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="确认删除项目"
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 60,
            display: "grid",
            placeItems: "center",
            padding: 18,
            background: "rgba(16,28,52,.32)",
          }}
        >
          <div
            className="card"
            style={{ width: "min(440px, 100%)", padding: 23, boxShadow: "var(--shadow-overlay)" }}
          >
            <div className="card-head">
              <div>
                <h2 className="card-title">删除「{deleteTarget.name}」？</h2>
                <div className="card-kicker">此操作不可恢复。</div>
              </div>
            </div>
            <p style={{ color: "var(--ink)", fontSize: 13, lineHeight: 1.7, margin: "4px 0 18px" }}>
              删除后该项目及全部产物不可恢复：数据集与上传文件、分析、报告、采访、洞察、问题、方案、决策、文档都会被永久清除。仅项目
              Owner 可执行。
            </p>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <button className="btn" disabled={deleting} onClick={() => setDeleteTarget(null)}>
                取消
              </button>
              <button
                className="btn btn-danger"
                disabled={deleting}
                onClick={() => void remove(deleteTarget.id)}
              >
                <Trash2 size={14} />
                {deleting ? "删除中…" : "确认删除"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
