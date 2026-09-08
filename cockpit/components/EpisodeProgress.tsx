"use client";

import type { EpisodeStatus } from "@/lib/api";
import {
  activeRunId,
  jobStatusSuffix,
  perStageLatestRows,
  stageGroupOf,
} from "@/lib/stageGroups";
import EpisodeWaitInfo from "@/components/EpisodeWaitInfo";

type EpisodeProgressProps = {
  status: EpisodeStatus;
  /** Deterministic clock for the wait guidance (tests / the view ticker). */
  now?: number;
  /** Wired to the view's immediate requery (stall recovery, 再照会). */
  onRequery?: () => void;
};

function measuredEtaMinutes(value: number | undefined): number | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
  return value;
}

/**
 * Stage + work units + the 工程2P wait guidance. ETA is rendered ONLY when
 * the payload carries a measured `eta_minutes` — otherwise stage/progress
 * alone (PRD 13.2: never invent precision). The region is a polite live
 * area and reports busy-ness while any stage row is running; PREVIEW_READY
 * is labeled 試し編集完了 — never 全体完了 (工程2P 改訂条件1).
 */
export default function EpisodeProgress({ status, now, onRequery }: EpisodeProgressProps) {
  const eta = measuredEtaMinutes(status.eta_minutes);
  const units = status.work_units;
  const suffix = jobStatusSuffix(status.status);
  // codex P1-2: stage counts come from each stage's LATEST truthful row —
  // an old run's success/running residue never inflates the counters and
  // never keeps the region busy (aria-busy) after its run ended.
  const active = activeRunId(status);
  const latest = [...perStageLatestRows(status).values()];
  const succeeded = latest.filter((run) => run.status === "succeeded").length;
  const running = latest.filter(
    (run) => run.status === "running" && run.run_id === active,
  ).length;

  return (
    <div data-testid="episode-progress" aria-live="polite" aria-busy={running > 0}>
      {suffix !== null ? (
        <p className="work-units" data-testid="job-status-suffix">
          {suffix}
        </p>
      ) : null}
      <p data-testid="work-units" className="work-units">
        {units !== undefined
          ? `完了 ${units.completed} / 残り ${units.remaining} 作業単位`
          : `完了ステージ ${succeeded}${running > 0 ? `・実行中 ${running}` : ""}`}
      </p>
      {eta !== null ? (
        <p data-testid="eta" className="work-units">
          残り約 {Math.round(eta)} 分（実測に基づく見込み）
        </p>
      ) : null}
      {status.stage_runs.length > 0 ? (
        <table className="stage-runs" data-testid="stage-runs">
          <thead>
            <tr>
              <th>ステージ</th>
              <th>段階</th>
              <th>状態</th>
              <th>リトライ</th>
              <th>最終エラー</th>
            </tr>
          </thead>
          <tbody>
            {status.stage_runs.map((run, index) => (
              // Index key: rebuilds append fresh rows per run, so stage_name
              // repeats (plan/preview appear again post-rebuild) — only the
              // row position is unique in the payload.
              <tr key={`${run.stage_name}-${index}`}>
                <td>{run.stage_name}</td>
                <td>{stageGroupOf(run.stage_name) ?? "—"}</td>
                <td>{run.status}</td>
                <td>{run.retry_count}</td>
                <td>{run.last_error_code ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="empty-note">ステージ実行はまだありません。2秒ごとに自動更新します。</p>
      )}
      <EpisodeWaitInfo status={status} now={now} onRequery={onRequery} />
    </div>
  );
}
