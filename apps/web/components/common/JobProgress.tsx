"use client";

/**
 * Batch 25: one progress indicator for every long-running LLM step.
 *
 * Deterministic mode (a job row with progress/current_step): a 6px brand bar
 * + tabular-nums percentage + the job's current step + elapsed time.
 * Indeterminate mode (synchronous request still in flight, e.g. distillation):
 * a flowing bar + timer.  Styling is inline over the batch-22 design tokens
 * so this component stays self-contained (no globals.css dependency).
 */

import { useEffect, useState } from "react";

function formatElapsed(startedAt: number): string {
  const seconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  return seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds} 秒`;
}

export function JobProgress({
  progress,
  currentStep,
  startedAt,
  indeterminate = false,
  tone = "brand",
}: {
  progress?: number | null;
  currentStep?: string | null;
  startedAt?: number;
  indeterminate?: boolean;
  tone?: "brand" | "neutral";
}) {
  const [, setTick] = useState(0);

  useEffect(() => {
    if (!startedAt) return;
    const timer = window.setInterval(() => setTick((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [startedAt]);

  const fill = tone === "brand" ? "var(--brand)" : "var(--line-strong)";
  const soft = tone === "brand" ? "var(--brand-soft)" : "var(--fill)";
  const percent =
    typeof progress === "number" && Number.isFinite(progress)
      ? Math.max(0, Math.min(100, Math.round(progress)))
      : null;

  return (
    <div className="job-progress" style={{ display: "grid", gap: 7 }} role="status" aria-live="polite">
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 10 }}>
        <span style={{ fontSize: 12, color: "var(--muted)", fontWeight: 500 }}>
          {currentStep || (indeterminate ? "处理中…" : "进行中…")}
        </span>
        <span style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
          {startedAt ? (
            <span
              style={{
                fontSize: 11,
                color: "var(--faint)",
                fontFamily: "var(--font-mono)",
                fontVariantNumeric: "tabular-nums",
              }}
            >
              {formatElapsed(startedAt)}
            </span>
          ) : null}
          {percent !== null ? (
            <span
              style={{
                fontSize: 13,
                color: "var(--ink)",
                fontWeight: 600,
                fontFamily: "var(--font-mono)",
                fontVariantNumeric: "tabular-nums",
              }}
            >
              {percent}%
            </span>
          ) : null}
        </span>
      </div>
      <div
        style={{
          height: 6,
          borderRadius: 99,
          background: soft,
          overflow: "hidden",
          position: "relative",
        }}
      >
        {percent !== null ? (
          <span
            style={{
              display: "block",
              height: "100%",
              width: `${percent}%`,
              borderRadius: "inherit",
              background: fill,
              transition: "width 0.4s",
            }}
          />
        ) : (
          <span
            style={{
              display: "block",
              height: "100%",
              width: "34%",
              borderRadius: "inherit",
              background: fill,
              animation: "job-progress-flow 1.4s ease-in-out infinite",
            }}
          />
        )}
      </div>
      <style>{`@keyframes job-progress-flow { 0% { margin-left: -34%; } 100% { margin-left: 100%; } }`}</style>
    </div>
  );
}
