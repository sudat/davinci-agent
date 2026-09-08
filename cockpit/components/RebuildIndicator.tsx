"use client";

import type { RebuildResult, ReviewApplyResult } from "@/lib/api";

/**
 * The apply-flow rebuild readout (PRD 13.3): phase, applied commands,
 * stage set, the honest refusal reason when the command kind is not
 * rebuild-executable yet, and the runner log. Rendered only after an
 * apply; the revert flow keeps its own note card instead.
 */
export default function RebuildIndicator({
  rebuildResult,
  applyResult,
  phaseText,
}: {
  rebuildResult: RebuildResult;
  applyResult: ReviewApplyResult;
  phaseText: string | null;
}) {
  const stageHint =
    rebuildResult.stage_hint ??
    (rebuildResult.stages !== undefined ? rebuildResult.stages.join(",") : "");
  return (
    <div className="card" data-testid="rebuild-indicator" style={{ marginTop: "var(--space-3)" }}>
      <p data-testid="rebuild-phase">
        {phaseText ?? "再build未実行（コマンドは記録済み）"}
      </p>
      <p>
        適用コマンド{" "}
        <span className="mono">
          {(applyResult.applied_commands ?? [applyResult.applied])
            .map((command) => command.command_id)
            .join("、")}
        </span>
      </p>
      <p>
        再build stage:{" "}
        <span className="mono" data-testid="rebuild-stage-hint">
          {stageHint}
        </span>
      </p>
      {rebuildResult.reason !== undefined ? (
        <p className="field-hint" data-testid="rebuild-reason">
          {rebuildResult.reason}
        </p>
      ) : null}
      {rebuildResult.runner_log !== undefined ? (
        <p className="field-hint">
          runner log: <span className="mono">{rebuildResult.runner_log}</span>
        </p>
      ) : null}
    </div>
  );
}
