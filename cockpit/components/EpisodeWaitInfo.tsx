"use client";

import { useRef } from "react";
import type { EpisodeStatus } from "@/lib/api";
import {
  formatElapsed,
  groupProgress,
  stageGroupOf,
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

function reportLine(status: EpisodeStatus): string {
  const raw = status.last_worker_report_at;
  if (raw === null || raw === undefined || raw === "") return "応答不明";
  const at = new Date(raw);
  const clock = Number.isNaN(at.getTime())
    ? raw
    : [at.getHours(), at.getMinutes(), at.getSeconds()]
        .map((part) => String(part).padStart(2, "0"))
        .join(":");
  return `最後の作業報告 ${clock}`;
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

  const runActive =
    (status.current_run ?? null) !== null ||
    status.stage_runs.some((row) => row.status === "running");
  const unreviewed = status.unreviewed_proposal_set === true;
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
      : status.stage_runs.filter((row) => currentGroup.stages.includes(row.stage_name));
  const currentWork = stageGroupOf(status.current_stage) ?? status.current_stage;
  const retryCount = status.current_run_retry_count;
  const retryReason =
    status.stage_runs.find(
      (row) => row.last_error_code !== null && row.run_id === (status.current_run ?? null),
    )?.last_error_code ?? null;

  return (
    <div data-testid="wait-guidance" className="wait-guidance" aria-busy={runActive}>
      <dl className="status-list">
        <div>
          <dt>経過時間</dt>
          <dd data-testid="wait-elapsed">
            {formatElapsed(elapsedMs)}
            {Number.isNaN(intakeMs) ? "（画面を開いてから）" : ""}
          </dd>
        </div>
        <div>
          <dt>現在の作業</dt>
          <dd data-testid="wait-current-work">{currentWork}</dd>
        </div>
      </dl>
      <div aria-live="polite">
        <p data-testid="wait-groups">
          {groupLines.map((line) => (
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
              : "今は操作不要です（自動で進行しています）"}
        </p>
        {runActive ? (
          <p className="field-hint" data-testid="wait-worker-report">
            {reportLine(status)}
          </p>
        ) : null}
        {typeof retryCount === "number" && retryCount > 0 ? (
          <p className="field-hint" data-testid="wait-retry">
            再試行中です（{retryCount}回目）
            {retryReason !== null ? `：理由 ${retryReason}` : ""}
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
