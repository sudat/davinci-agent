"use client";

import type { EpisodeStatus } from "@/lib/api";

type EpisodeProgressProps = {
  status: EpisodeStatus;
};

function measuredEtaMinutes(value: number | undefined): number | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
  return value;
}

/**
 * Stage + work units. ETA is rendered ONLY when the payload carries a
 * measured `eta_minutes` — otherwise stage/progress alone (PRD 13.2:
 * never invent precision).
 */
export default function EpisodeProgress({ status }: EpisodeProgressProps) {
  const eta = measuredEtaMinutes(status.eta_minutes);
  const units = status.work_units;
  const succeeded = status.stage_runs.filter((run) => run.status === "succeeded").length;
  const running = status.stage_runs.filter((run) => run.status === "running").length;

  return (
    <div data-testid="episode-progress">
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
    </div>
  );
}
