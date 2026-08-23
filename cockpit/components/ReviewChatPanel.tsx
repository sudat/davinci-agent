"use client";

import { useState } from "react";
import {
  applyReviewCommand,
  postRebuild,
  postReviewChat,
  CockpitApiError,
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

type ReviewChatPanelProps = {
  episodeId: string;
  getAtSeconds: () => number | null;
  fetchImpl?: typeof fetch;
};

/**
 * Review chat (PRD 13.3): natural-language correction -> structured
 * command preview (the echoed draft, shown verbatim with its parsed
 * kind/target/delta) -> apply -> lineage-scoped partial rebuild recorded
 * with HTTP 202. Ambiguous drafts stay unappliable until rephrased.
 */
export default function ReviewChatPanel({
  episodeId,
  getAtSeconds,
  fetchImpl,
}: ReviewChatPanelProps) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ code: string; detail: string } | null>(null);
  const [sentAt, setSentAt] = useState<number | null>(null);
  const [draft, setDraft] = useState<ReviewCommandDraft | null>(null);
  const [applyResult, setApplyResult] = useState<ReviewApplyResult | null>(null);
  const [rebuildResult, setRebuildResult] = useState<RebuildResult | null>(null);

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
    } catch (cause) {
      setError(failure(cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card" data-testid="review-chat-panel">
      <h2 className="card-title">修正チャット</h2>
      <p className="page-subtitle" style={{ marginBottom: "var(--space-3)" }}>
        自然言語で修正を伝えると、構造化コマンドの解釈プレビューを返します。確認して適用すると、影響stageのみの部分rebuildを記録します。
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
          <p>
            部分rebuildを受け付けました（HTTP 202・適用コマンド{" "}
            <span className="mono">{applyResult.applied.command_id}</span>）
          </p>
          <p>
            再build stage: <span className="mono" data-testid="rebuild-stage-hint">{rebuildResult.stage_hint}</span>
          </p>
          <p className="field-hint">{rebuildResult.note}</p>
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
