"use client";

import { useRef } from "react";
import type { EpisodeStatus } from "@/lib/api";
import {
  activeRunId,
  formatElapsed,
  groupProgress,
  lastStartedRowOfActiveRun,
  perStageLatestRows,
  runningStageRows,
  stageWorkName,
  STAGE_GROUPS,
} from "@/lib/stageGroups";
import { useNow } from "@/components/useNow";

const SHOW_AFTER_MS = 10_000;

type EpisodeWaitInfoProps = {
  status: EpisodeStatus;
  onRequery?: () => void;
  /** Deterministic clock override (from the polling view's ticker). */
  now?: number;
};

const UNMEASURED_TEXT = "この工程の進み具合は取得できません";

function clockOf(raw: string): string {
  const at = new Date(raw);
  return Number.isNaN(at.getTime())
    ? raw
    : [at.getHours(), at.getMinutes(), at.getSeconds()]
        .map((part) => String(part).padStart(2, "0"))
        .join(":");
}

function reportLine(status: EpisodeStatus): string {
  const raw = status.last_worker_report_at;
  if (raw === null || raw === undefined || raw === "") return "最後の作業報告 応答不明";
  return `最後の作業報告 ${clockOf(raw)}`;
}

function lastRealProgress(status: EpisodeStatus): string | null {
  const arrived = status.stage_runs
    .map((row) => row.first_output_arrived_at)
    .filter((value): value is string => typeof value === "string" && value !== "")
    .sort();
  const latest = arrived.at(-1);
  return latest === undefined ? null : clockOf(latest);
}

/**
 * 工程2P 10秒必須の待機情報（改訂条件5）: 経過時間・現在の作業・
 * 完了済み/現在/未着手の段階・操作要否を、payload の実在項目だけから
 * 出す。捏造百分率なし・未計測は未計測と言う・動作報告がなければ
 * 応答不明（正常とは言わない）・中止は未実装と明示して再照会を出す。
 * 経過時間の reference は intake の wall-clock（無ければこの画面を開いた
 * 時刻——それ自体は実測である）。
 */
export default function EpisodeWaitInfo({
  status,
  onRequery,
  now: nowProp,
}: EpisodeWaitInfoProps) {
  const openedAtRef = useRef<number | null>(null);
  if (openedAtRef.current === null) openedAtRef.current = Date.now();
  const ticked = useNow(true);
  const now = nowProp ?? ticked;
  const intakeMs = Date.parse(status.intake_created_at ?? "");
  const anchor = Number.isNaN(intakeMs) ? (openedAtRef.current ?? now) : intakeMs;
  const elapsedMs = Math.max(0, now - anchor);
  if (elapsedMs <= SHOW_AFTER_MS) return null;

  // codex P1-2: activity is ONLY the active run's running rows — a run id's
  // existence or an old run's running residue never marks the episode busy.
  // 導出源は stageGroups.runningStageRows に集約（工程名・開始時刻・経過の
  // 同一ソース）。job.current_stage は到達済み段階であり別物。
  const activeRunningRows = runningStageRows(status);
  const runActive = activeRunningRows.length > 0;
  const unreviewed = status.unreviewed_proposal_set === true;
  const reportUnknown =
    status.last_worker_report_at === null ||
    status.last_worker_report_at === undefined ||
    status.last_worker_report_at === "";
  const idleDone =
    !runActive &&
    !unreviewed &&
    (status.status === "PREVIEW_READY" || status.status === "FROZEN");
  if (idleDone) return null;

  const failedGroups = STAGE_GROUPS.filter(
    (group) => groupProgress(status, group) === "failed",
  ).map((group) => group.label);
  const segment = (
    heading: string,
    state: "done" | "current" | "untouched",
  ): string | null => {
    const labels = STAGE_GROUPS.filter((group) => groupProgress(status, group) === state)
      .map((group) => group.label);
    return labels.length > 0 ? `${heading}: ${labels.join("・")}` : null;
  };
  const groupLines = [
    segment("完了済みの段階", "done"),
    segment("現在の段階", "current"),
    segment("未着手の段階", "untouched"),
  ];

  const currentGroup = STAGE_GROUPS.find((group) =>
    group.stages.includes(status.current_stage),
  );
  const currentGroupRows =
    currentGroup === undefined
      ? []
      : [...perStageLatestRows(status).entries()].filter(([stage]) =>
          currentGroup.stages.includes(stage),
        );
  // 現在工程の同一ソース導出（codex再指摘）: 工程名も開始時刻も現行runの
  // 実行中行からだけ出す。複数並行は推測してまとめず工程ごとに併記する。
  const runningStageNames = [
    ...new Set(activeRunningRows.map((row) => row.stage_name)),
  ];
  function stageElapsedText(stageName: string): string {
    const stamps = activeRunningRows
      .filter((row) => row.stage_name === stageName)
      .map((row) => Date.parse(row.first_started_at ?? ""))
      .filter((ms) => !Number.isNaN(ms));
    return stamps.length === 0
      ? "不明（開始時刻を取得できません）"
      : formatElapsed(now - Math.min(...stamps));
  }
  // 停止/終端時は成長する経過を捏造しない: 現行runに開始時刻付きの行が
  // あれば、その実測時刻を「最後の実行開始」として固定表示する。現行runを
  // 特定できない（未起動の予約のみ等）ときは旧runの時刻を使わず、今回の
  // 実行を特定できない旨を明示する（旧run時刻の誤表示の回帰対策）。
  const lastStarted = lastStartedRowOfActiveRun(status);
  const stoppedStageElapsed =
    activeRunId(status) === null
      ? "実行中の工程はありません（最後の実行開始 不明（今回の実行を特定できません））"
      : lastStarted === null || lastStarted.first_started_at === undefined
        ? "実行中の工程はありません"
        : `実行中の工程はありません（最後の実行開始 ${clockOf(lastStarted.first_started_at)}）`;
  const stageElapsedLine =
    runningStageNames.length === 0
      ? stoppedStageElapsed
      : runningStageNames
          .map((name) => `${stageWorkName(name)}（${name}） ${stageElapsedText(name)}`)
          .join("・");
  const currentWork =
    runningStageNames.length > 0
      ? runningStageNames.map((name) => stageWorkName(name)).join("・")
      : `${stageWorkName(status.current_stage)}（実行中の工程なし）`;
  const retryCount = status.current_run_retry_count;
  const retryReason =
    status.stage_runs.find(
      (row) => row.last_error_code !== null && row.run_id === (status.current_run ?? null),
    )?.last_error_code ?? null;
  // W8: 回数だけで将来動作を断定しない。実経路の状態で言い分ける:
  // 実行中行あり→再試行のうえ実行中（現在形）、終端失敗→履歴と停止を区別し
  // 再実行予定は主張しない、終端成功→履歴と完了を区別、行なし→不明。
  const retryLine = ((): string | null => {
    if (typeof retryCount !== "number" || retryCount <= 0) return null;
    const reason = retryReason !== null ? `：理由 ${retryReason}` : "";
    if (activeRunningRows.length > 0) {
      return `再試行のうえ実行中です（${retryCount}回目・最大回数は提供されていません）${reason}。`;
    }
    const active = activeRunId(status);
    const activeRows =
      active === null
        ? []
        : status.stage_runs.filter((row) => row.run_id === active);
    if (activeRows.some((row) => row.status === "failed_blocked")) {
      return `再試行の履歴があります（${retryCount}回）${reason}。停止しています（自動で次に動く予定は確認できません）。`;
    }
    if (
      activeRows.length > 0 &&
      activeRows.every((row) => row.status === "succeeded")
    ) {
      return `再試行の履歴があります（${retryCount}回）${reason}。現在は完了しています。`;
    }
    return `再試行の履歴があります（${retryCount}回）${reason}。現在の状態は不明です。`;
  })();

  return (
    <div data-testid="wait-guidance" className="wait-guidance" aria-busy={runActive}>
      <dl className="status-list">
        <div>
          <dt>全体の経過時間</dt>
          <dd data-testid="wait-elapsed">
            {formatElapsed(elapsedMs)}
            {Number.isNaN(intakeMs) ? "（画面を開いてから）" : ""}
          </dd>
        </div>
        <div>
          <dt>現在の工程の経過</dt>
          <dd data-testid="wait-stage-elapsed">{stageElapsedLine}</dd>
        </div>
        <div>
          <dt>現在の作業</dt>
          <dd data-testid="wait-current-work">{currentWork}</dd>
        </div>
        <div>
          <dt>最後の実進捗</dt>
          <dd data-testid="wait-last-progress">
            {lastRealProgress(status) ?? "取得できません"}
          </dd>
        </div>
      </dl>
      <div aria-live="polite">
        <p data-testid="wait-groups">
          {groupLines
            .filter((line): line is string => line !== null)
            .map((line) => (
              <span key={line} className="wait-group-line">
                {line}
              </span>
            ))}
        </p>
        {currentGroupRows.length === 0 ? (
          <p className="field-hint" data-testid="wait-unmeasured">
            {UNMEASURED_TEXT}
          </p>
        ) : null}
        <p data-testid="wait-operator-action">
          {unreviewed
            ? "あなたの確認待ちです（修正案を確認してください）"
            : failedGroups.length > 0
              ? "動いていません（止まっている段階を確認してください）"
              : reportUnknown
                ? "進み具合は応答不明です（しばらく待つか再照会してください）"
                : "今は操作不要です（自動で進行しています）"}
        </p>
        <p className="field-hint" data-testid="wait-worker-report">
          {reportLine(status)}
        </p>
        {/* 再試行の表示は実経路の状態から導く: stage_runner.py _attempts
            は同一工程の runner_fn を再実行するが、上限・次回失敗時の動作・
            自動再実行の予定は payload に無い。終端行に再実行予定を主張せず、
            履歴と現在の状態を区別する（不明なら不明）。数値は作らない。 */}
        {retryLine !== null ? (
          <p className="field-hint" data-testid="wait-retry">
            {retryLine}
          </p>
        ) : null}
        {failedGroups.length > 0 ? (
          <div data-testid="wait-stalled">
            <p data-testid="wait-failed">
              止まっている段階があります: {failedGroups.join("・")}
            </p>
            <p className="field-hint" data-testid="wait-stop-unsupported">
              安全な中止は未実装です
            </p>
            <button
              type="button"
              className="btn-small"
              data-testid="requery-button"
              onClick={onRequery}
            >
              再照会
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
