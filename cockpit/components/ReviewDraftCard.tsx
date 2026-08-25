"use client";

import type { ReviewCommandDraft } from "@/lib/api";
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

/**
 * One echoed draft, numbered when the message proposed several. Testids
 * carry the single-draft ids for backward compatibility when only one
 * draft is shown; the caller passes the suffix-free base names.
 */
export default function ReviewDraftCard({
  draft,
  index,
  multi,
}: {
  draft: ReviewCommandDraft;
  index: number;
  multi: boolean;
}) {
  const suffix = multi ? `-${index + 1}` : "";
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
    </div>
  );
}
