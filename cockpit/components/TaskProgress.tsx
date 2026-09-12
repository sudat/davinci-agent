"use client";

import type { ReactNode } from "react";
import { formatElapsed } from "@/lib/stageGroups";
import { useNow } from "@/components/useNow";

type TaskProgressProps = {
  /** Plain current task name (e.g. 「AIに相談中」) — never a percentage. */
  taskName: string;
  /** Wall-clock ms when the task started (the elapsed reference). */
  startedAt: number;
  /** Deterministic clock override (tests). */
  now?: number;
  /** Technical detail, hidden behind 「詳しく見る」 when present. */
  details?: ReactNode;
};

/**
 * The ONE waiting display for operator-visible work (consultation AI send,
 * preview generation, trial-video preparation): an animated indeterminate
 * bar, the plain current task name, and a live mm:ss elapsed. Percentages
 * are never shown — progress is unmeasured, so none is fabricated.
 */
export default function TaskProgress({ taskName, startedAt, now: nowProp, details }: TaskProgressProps) {
  const ticked = useNow(nowProp === undefined);
  const now = nowProp ?? ticked;
  return (
    <div className="task-progress" data-testid="task-progress" aria-live="polite" aria-busy="true">
      <div className="task-progress-track" aria-hidden="true">
        <div className="task-progress-bar" data-testid="task-progress-bar" />
      </div>
      <p className="task-progress-line">
        <span data-testid="task-progress-name">{taskName}</span>{" "}
        <span className="mono" data-testid="task-progress-elapsed">
          {formatElapsed(now - startedAt)}
        </span>
      </p>
      {details !== undefined && details !== null ? (
        <details className="task-progress-details">
          <summary>詳しく見る</summary>
          {details}
        </details>
      ) : null}
    </div>
  );
}
