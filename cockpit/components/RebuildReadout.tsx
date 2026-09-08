"use client";

import RebuildIndicator from "@/components/RebuildIndicator";
import type { RebuildResult, ReviewApplyResult } from "@/lib/api";

/**
 * The apply/revert aftermath in one place: the revert note (with the
 * rebuild-launch failure hint, P1b honesty) plus the live rebuild
 * indicator. Extracted so the panel composes the readout instead of
 * inlining the phase plumbing (工程1 undo / V44-1 batch rebuild).
 */
export default function RebuildReadout({
  revertNote,
  rebuildResult,
  applyResult,
  phaseText,
}: {
  revertNote: string | null;
  rebuildResult: RebuildResult | null;
  applyResult: ReviewApplyResult | null;
  phaseText: string | null;
}) {
  if (revertNote === null && (rebuildResult === null || applyResult === null)) {
    return null;
  }
  const stageHint =
    rebuildResult?.stage_hint ??
    (rebuildResult?.stages !== undefined ? rebuildResult.stages.join(",") : "");
  return (
    <>
      {revertNote !== null ? (
        <div className="card" data-testid="review-revert-note" style={{ marginTop: "var(--space-3)" }}>
          <p>{revertNote}</p>
          {rebuildResult !== null ? (
            <>
              {phaseText !== null ? <p data-testid="rebuild-phase">{phaseText}</p> : null}
              {rebuildResult.scheduled === false ? (
                <p className="field-hint" data-testid="review-revert-rebuild-failed">
                  もう一度「前の状態に戻す」を押すと、再構築の起動だけをやり直します。
                </p>
              ) : null}
              <p>
                再build stage:{" "}
                <span className="mono" data-testid="rebuild-stage-hint">
                  {stageHint}
                </span>
              </p>
            </>
          ) : null}
        </div>
      ) : null}
      {rebuildResult !== null && applyResult !== null ? (
        <RebuildIndicator
          rebuildResult={rebuildResult}
          applyResult={applyResult}
          phaseText={phaseText}
        />
      ) : null}
    </>
  );
}
