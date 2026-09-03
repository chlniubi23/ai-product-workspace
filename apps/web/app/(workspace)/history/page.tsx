"use client";

import Link from "next/link";
import { ArchiveRestore, ChevronRight, History } from "lucide-react";
import { useState } from "react";
import { apiRequest } from "@/lib/api";
import { SnapshotMeta, useWorkflowSnapshot } from "@/components/workflow/WorkflowFrame";
import { formatWorkflowDate } from "@/lib/workflow";

export default function HistoryPage() {
  const { snapshot, loading, error, refresh } = useWorkflowSnapshot();
  const [restoringId, setRestoringId] = useState("");
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

  return (
    <div className="page">
      <header className="workflow-header" style={{ marginBottom: 4 }}>
        <div className="workflow-header-main">
          <div className="workflow-eyebrow">
            <span>历史</span>
          </div>
          <h1>历史工作流</h1>
          <p>已完成并归档的项目在这里只读回看；恢复到活跃后可继续编辑。</p>
        </div>
      </header>
      <SnapshotMeta snapshot={snapshot} />
      {error && (
        <div className="form-error" role="alert">
          {error}
        </div>
      )}
      {loading ? (
        <section className="card" style={{ marginTop: 16, padding: 24 }}>
          正在加载历史…
        </section>
      ) : archived.length === 0 ? (
        <section className="card empty-state" style={{ marginTop: 16 }}>
          <History size={20} />
          <strong>还没有归档的工作流</strong>
          <p>在第 11 步生成文档后点「完成并归档」，完成的工作流会出现在这里。</p>
        </section>
      ) : (
        <div className="grid grid-2" style={{ marginTop: 16 }}>
          {archived.map((project) => (
            <section className="card card-pad" key={project.id}>
              <div className="card-head">
                <div>
                  <strong>{project.name || "未命名工作流"}</strong>
                  <div className="card-kicker">
                    归档于 {formatWorkflowDate(project.archived_at)}
                  </div>
                </div>
                <span className="tag tag-slate">已归档</span>
              </div>
              {project.goal_statement && (
                <p style={{ color: "var(--muted)", lineHeight: 1.6 }}>
                  {project.goal_statement.slice(0, 120)}
                </p>
              )}
              <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 10 }}>
                <button
                  className="btn btn-subtle btn-sm"
                  disabled={restoringId === project.id}
                  onClick={() => void restore(project.id)}
                >
                  <ArchiveRestore size={13} />
                  恢复到活跃
                </button>
                <Link className="btn btn-primary btn-sm" href={`/history/${project.id}`}>
                  回看产出 <ChevronRight size={13} />
                </Link>
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
