"use client";

import { useMemo, useRef, useState } from "react";
import {
  apiFailure,
  applyReviewCommand,
  postRebuild,
  postReviewChat,
  type EpisodeStatus,
  type RebuildResult,
  type ReviewApplyResult,
  type ReviewCommandDraft,
} from "@/lib/api";
import ErrorNotice from "@/components/ErrorNotice";
import ReviewDraftCard from "@/components/ReviewDraftCard";

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
 * command preview (echoed drafts, shown verbatim with parsed
 * kind/target/delta — SEVERAL when one message named several corrections)
 * -> apply -> lineage-scoped partial rebuild, executed by the detached
 * runner. Ambiguous drafts stay unappliable until rephrased (safety rule).
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
  const [drafts, setDrafts] = useState<ReviewCommandDraft[] | null>(null);
  const [applyResult, setApplyResult] = useState<ReviewApplyResult | null>(null);
  const [rebuildResult, setRebuildResult] = useState<RebuildResult | null>(null);
  const [baselinePreviewRuns, setBaselinePreviewRuns] = useState<number | null>(null);
  const statusRef = useRef<EpisodeStatus | null>(null);
  statusRef.current = status;

  const send = async () => {
    if (text.trim() === "") return;
    setBusy(true);
    setError(null);
    setDrafts(null);
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
      setDrafts(result.drafts ?? [result.draft]);
    } catch (cause) {
      setError(apiFailure(cause));
    } finally {
      setBusy(false);
    }
  };

  const applyAndRebuild = async () => {
    // sentAt may be null (no player position): the drafts carry explicit
    // targets, so the echo applies regardless — position is only a hint.
    if (drafts === null || drafts.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      const applied = await applyReviewCommand(
        episodeId,
        { text: drafts[0].text, at_seconds: sentAt, drafts },
        fetchImpl,
      );
      setApplyResult(applied);
      const commandIds =
        applied.applied_commands?.map((command) => command.command_id) ?? [
          applied.applied.command_id,
        ];
      const rebuilt = await postRebuild(
        episodeId,
        {
          applied_command: commandIds[0],
          ...(commandIds.length > 1 ? { applied_commands: commandIds } : {}),
        },
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
      setError(apiFailure(cause));
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

  const anyAmbiguous = drafts?.some((draft) => draft.needs_confirmation) ?? false;

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
      {drafts !== null && drafts.length > 0 ? (
        drafts.map((draft, index) => (
          <ReviewDraftCard
            key={draft.command_id}
            draft={draft}
            index={index}
            multi={drafts.length > 1}
          />
        ))
      ) : null}
      {drafts !== null && drafts.length > 0 ? (
        <div className="actions">
          <button
            type="button"
            className="btn-primary"
            onClick={() => void applyAndRebuild()}
            disabled={busy || anyAmbiguous}
            data-testid="review-apply-button"
          >
            {drafts.length > 1 ? "この修正をすべて適用" : "この修正を適用"}
          </button>
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
