"use client";

import { useMemo, useRef, useState } from "react";
import {
  applyReviewCommand,
  postRebuild,
  postReviewChat,
  CockpitApiError,
  type EpisodeStatus,
  type RebuildResult,
  type ReviewApplyResult,
  type ReviewCommandDraft,
} from "@/lib/api";
import ErrorNotice from "@/components/ErrorNotice";

const COMMAND_KIND_LABEL: Record<string, string> = {
  remove_section: "区間を削除",
  keep_longer: "長めに残す",
  quiet_longer: "静かな場面を長く",
  use_other_take: "別テイクを使用",
  insert_broll: "Bロールを追加",
  mark_boring: "退屈とマーク",
  subtitle_shorter: "字幕を短く",
  remove_effect: "効果を削除",
  lower_bgm: "BGMを下げる",
  match_color: "色を合わせる",
  channel_lower_third: "チャンネル限定テロップ",
  episode_only: "このエピソード限定",
};

type RebuildPhase = "done" | "running" | "scheduled" | "recorded";

const REBUILD_PHASE_LABEL: Record<Exclude<RebuildPhase, "recorded">, string> = {
  scheduled: "再build予約済み",
  running: "再build実行中",
  done: "再build完了",
};

type ReviewChatPanelProps = {
  episodeId: string;
  getAtSeconds: () => number | null;
  status: EpisodeStatus | null;
  fetchImpl?: typeof fetch;
};

/**
 * Review chat (PRD 13.3): natural-language correction -> structured
 * command preview (the echoed draft, shown verbatim with its parsed
 * kind/target/delta) -> apply -> lineage-scoped partial rebuild, executed
 * by the detached runner. The indicator derives scheduled/running/done
 * from the page's job-status polling (stage_runs). Ambiguous drafts stay
 * unappliable until rephrased.
 */
export default function ReviewChatPanel({
  episodeId,
  getAtSeconds,
  status,
  fetchImpl,
}: ReviewChatPanelProps) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ code: string; detail: string } | null>(null);
  const [sentAt, setSentAt] = useState<number | null>(null);
  const [draft, setDraft] = useState<ReviewCommandDraft | null>(null);
  const [applyResult, setApplyResult] = useState<ReviewApplyResult | null>(null);
  const [rebuildResult, setRebuildResult] = useState<RebuildResult | null>(null);
  const [baselinePreviewRuns, setBaselinePreviewRuns] = useState<number | null>(null);
  const statusRef = useRef<EpisodeStatus | null>(null);
  statusRef.current = status;

  const send = async () => {
    if (text.trim() === "") return;
    setBusy(true);
    setError(null);
    setDraft(null);
    setApplyResult(null);
    setRebuildResult(null);
    const atSeconds = getAtSeconds();
    setSentAt(atSeconds);
    try {
      const result = await postReviewChat(
        episodeId,
        { text: text.trim(), at_seconds: atSeconds },
        fetchImpl,
      );
      setDraft(result.draft);
    } catch (cause) {
      setError(failure(cause));
    } finally {
      setBusy(false);
    }
  };

  const applyAndRebuild = async () => {
    if (draft === null || sentAt === null) return;
    setBusy(true);
    setError(null);
    try {
      const applied = await applyReviewCommand(
        episodeId,
        { text: draft.text, at_seconds: sentAt },
        fetchImpl,
      );
      setApplyResult(applied);
      const rebuilt = await postRebuild(
        episodeId,
        { applied_command: applied.applied.command_id },
        fetchImpl,
      );
      setRebuildResult(rebuilt);
      setBaselinePreviewRuns(
        statusRef.current !== null
          ? statusRef.current.stage_runs.filter(
              (run) => run.stage_name === "preview" && run.status === "succeeded",
            ).length
          : null,
      );
    } catch (cause) {
      setError(failure(cause));
    } finally {
      setBusy(false);
    }
  };

  const rebuildPhase = useMemo<RebuildPhase | null>(() => {
    if (rebuildResult === null) return null;
    if (!rebuildResult.scheduled) return "recorded";
    if (status === null) return "scheduled";
    const hasRunning = status.stage_runs.some((run) => run.status === "running");
    if (hasRunning) return "running";
    // "compile" rows only ever come from a rebuild (the initial chain
    // never records one), so a succeeded compile needs no baseline.
    const compileDone = status.stage_runs.some(
      (run) => run.stage_name === "compile" && run.status === "succeeded",
    );
    const previewDone = status.stage_runs.filter(
      (run) => run.stage_name === "preview" && run.status === "succeeded",
    ).length;
    if (compileDone || (baselinePreviewRuns !== null && previewDone > baselinePreviewRuns)) {
      return "done";
    }
    return "scheduled";
  }, [rebuildResult, status, baselinePreviewRuns]);

  return (
    <section className="card" data-testid="review-chat-panel">
      <h2 className="card-title">修正チャット</h2>
      <p className="page-subtitle" style={{ marginBottom: "var(--space-3)" }}>
        自然言語で修正を伝えると、構造化コマンドの解釈プレビューを返します。確認して適用すると、影響stageのみの部分rebuildが実行されます。
      </p>
      {error !== null ? <ErrorNotice code={error.code} detail={error.detail} /> : null}
      <label className="field" htmlFor="review-chat-input">
        修正指示（自然言語）
        <textarea
          id="review-chat-input"
          rows={2}
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="例: この後2秒残して"
          data-testid="review-chat-input"
        />
      </label>
      <div className="actions">
        <button
          type="button"
          className="btn-primary"
          onClick={() => void send()}
          disabled={busy || text.trim() === ""}
          data-testid="review-chat-send"
        >
          送信
        </button>
      </div>
      {draft !== null ? (
        <div className="card" data-testid="review-draft" style={{ marginTop: "var(--space-3)" }}>
          <p>
            <span className="mono">{draft.command_id}</span>
          </p>
          <dl className="status-list">
            <div>
              <dt>解釈</dt>
              <dd data-testid="review-draft-kind">
                {draft.command_kind !== null
                  ? (COMMAND_KIND_LABEL[draft.command_kind] ?? draft.command_kind)
                  : "解釈なし"}
              </dd>
            </div>
            <div>
              <dt>対象時刻</dt>
              <dd className="mono" data-testid="review-draft-target">
                {draft.target_seconds !== null ? `${draft.target_seconds}s` : "—"}
              </dd>
            </div>
            <div>
              <dt>時間差</dt>
              <dd className="mono" data-testid="review-draft-delta">
                {draft.seconds_delta !== null ? `+${draft.seconds_delta}s` : "—"}
              </dd>
            </div>
          </dl>
          {draft.needs_confirmation ? (
            <p className="field-hint" data-testid="review-draft-needs-confirmation">
              曖昧のため確認が必要: {draft.confirmation_reason}
            </p>
          ) : null}
          <div className="actions">
            <button
              type="button"
              className="btn-primary"
              onClick={() => void applyAndRebuild()}
              disabled={busy || draft.needs_confirmation}
              data-testid="review-apply-button"
            >
              適用して部分rebuild
            </button>
          </div>
        </div>
      ) : null}
      {rebuildResult !== null && applyResult !== null ? (
        <div className="card" data-testid="rebuild-indicator" style={{ marginTop: "var(--space-3)" }}>
          {rebuildPhase !== null && rebuildPhase !== "recorded" ? (
            <p data-testid="rebuild-phase">
              {REBUILD_PHASE_LABEL[rebuildPhase]}
              {rebuildPhase === "scheduled" ? "（runner起動待ち・進捗は自動更新）" : ""}
            </p>
          ) : (
            <p data-testid="rebuild-phase">再build未実行（コマンドは記録済み）</p>
          )}
          <p>
            適用コマンド <span className="mono">{applyResult.applied.command_id}</span>
          </p>
          <p>
            再build stage:{" "}
            <span className="mono" data-testid="rebuild-stage-hint">
              {rebuildResult.stage_hint ??
                (rebuildResult.stages !== undefined
                  ? rebuildResult.stages.join(",")
                  : "")}
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
      ) : null}
    </section>
  );
}

function failure(cause: unknown): { code: string; detail: string } {
  if (cause instanceof CockpitApiError) {
    return { code: cause.code, detail: cause.detail };
  }
  return { code: "unexpected-client-error", detail: String(cause) };
}
