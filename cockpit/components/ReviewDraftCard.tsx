"use client";

import type { InvestigationState, ReviewCommandDraft } from "@/lib/api";
import { confirmationReasonJa } from "@/lib/reviewText";

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

const MATERIALS_CHECKED_TEXT =
  "周辺の字幕と場面情報を確認しましたが、原因はまだ特定できていません";
const UNCONFIRMED_TEXT = "原因はまだ確認できていません";

/**
 * Honest 工程1 display state (backend `_investigation_state`): the
 * completion form 「原因を調査しました」 is forbidden — investigation never
 * claims an identified cause. Preference: explicit state on the draft,
 * then the chat response's state, then derivation from hypothesis
 * presence (investigated-without-hypothesis is reported as unconfirmed —
 * this surface never claims materials it cannot verify were checked).
 */
function investigationDisplay(
  draft: ReviewCommandDraft,
  responseState: InvestigationState | undefined,
): string | null {
  const hasHypothesis = typeof draft.hypothesis === "string" && draft.hypothesis !== "";
  const state =
    draft.investigation_state ??
    responseState ??
    (hasHypothesis
      ? "hypothesis-proposed"
      : draft.investigated === true
        ? "unconfirmed"
        : undefined);
  if (state === "hypothesis-proposed") {
    return hasHypothesis ? `AIの仮説：${draft.hypothesis}` : UNCONFIRMED_TEXT;
  }
  if (state === "materials-checked") return MATERIALS_CHECKED_TEXT;
  if (state === "unconfirmed") return UNCONFIRMED_TEXT;
  return null;
}

/**
 * One echoed draft, numbered when the message proposed several. Testids
 * carry the single-draft ids for backward compatibility when only one
 * draft is shown; the caller passes the suffix-free base names. The 採用
 * button renders ONLY when the caller passes `onAdopt` (工程2 rework:
 * alternatives sets adopt one draft at a time; command bundles never do —
 * they carry the apply-all button instead).
 */
export default function ReviewDraftCard({
  draft,
  index,
  multi,
  investigationState,
  adoptable = false,
  busy = false,
  onAdopt,
}: {
  draft: ReviewCommandDraft;
  index: number;
  multi: boolean;
  investigationState?: InvestigationState;
  adoptable?: boolean;
  busy?: boolean;
  onAdopt?: () => void;
}) {
  const suffix = multi ? `-${index + 1}` : "";
  const investigation = investigationDisplay(draft, investigationState);
  return (
    <div
      className="card"
      data-testid={`review-draft${suffix}`}
      style={{ marginTop: "var(--space-3)" }}
    >
      <p>
        {multi ? `修正 ${index + 1}：` : ""}
        <span className="mono">{draft.command_id}</span>
      </p>
      <dl className="status-list">
        <div>
          <dt>解釈</dt>
          <dd data-testid={`review-draft-kind${suffix}`}>
            {draft.command_kind !== null
              ? (COMMAND_KIND_LABEL[draft.command_kind] ?? draft.command_kind)
              : "解釈なし"}
          </dd>
        </div>
        <div>
          <dt>対象時刻</dt>
          <dd className="mono" data-testid={multi ? undefined : "review-draft-target"}>
            {draft.target_seconds !== null ? `${draft.target_seconds}s` : "—"}
          </dd>
        </div>
        <div>
          <dt>時間差</dt>
          <dd className="mono" data-testid={multi ? undefined : "review-draft-delta"}>
            {draft.seconds_delta !== null ? `+${draft.seconds_delta}s` : "—"}
          </dd>
        </div>
      </dl>
      {draft.needs_confirmation ? (
        <p
          className="field-hint"
          data-testid={`review-draft-needs-confirmation${suffix}`}
        >
          曖昧のため確認が必要: {confirmationReasonJa(draft.confirmation_reason)}
        </p>
      ) : null}
      {investigation !== null ? (
        <p className="field-hint" data-testid={`review-draft-investigation${suffix}`}>
          {investigation}
        </p>
      ) : null}
      {onAdopt !== undefined ? (
        <div className="actions">
          <button
            type="button"
            className="btn-primary"
            onClick={onAdopt}
            disabled={busy || !adoptable}
            data-testid={`review-draft-adopt${suffix}`}
          >
            {busy ? "適用中…" : "この案を採用"}
          </button>
          {!adoptable ? (
            <span className="field-hint">曖昧な案は採用できません</span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
