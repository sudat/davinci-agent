"use client";

import {
  DECISION_LABEL,
  scopeSummary,
} from "@/components/ConsultationJudgmentForm";
import ConsultationJudgmentForm from "@/components/ConsultationJudgmentForm";
import ConsultationProposalCard from "@/components/ConsultationProposalCard";
import {
  isPolicyOutcomeEntry,
  type ConsultationEntry,
  type ConsultationJudgmentInput,
  type ConsultationPayload,
  type ConsultationPolicyOutcomeEntry,
} from "@/lib/api";

function clockOf(raw: string): string {
  const at = new Date(raw);
  if (Number.isNaN(at.getTime())) return raw;
  return [at.getHours(), at.getMinutes(), at.getSeconds()]
    .map((part) => String(part).padStart(2, "0"))
    .join(":");
}

type ConsultationEntryListProps = {
  payload: ConsultationPayload | null;
  busy: boolean;
  onJudgment: (input: ConsultationJudgmentInput) => void;
};

function nonEmptyText(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

/** Slice-2 outcome line. Every field is read defensively: old data may
 *  omit them, so absence renders as 不明 — never fabricated, never a crash. */
export function outcomeLineOf(entry: ConsultationPolicyOutcomeEntry): string {
  const status: unknown = entry.status;
  if (status === "honored") {
    return `採用した方針の反映結果：反映されました（対象版 ${nonEmptyText(entry.plan_version) ?? "不明"}）`;
  }
  if (status === "failed") {
    const reasons: unknown = entry.reasons;
    const list = Array.isArray(reasons)
      ? reasons.filter((reason): reason is string => nonEmptyText(reason) !== null)
      : [];
    return `採用した方針の反映結果：反映できませんでした：${list.length > 0 ? list.join("、") : "理由は不明"}`;
  }
  return "採用した方針の反映結果：不明";
}

function outcomeKeyOf(entry: ConsultationPolicyOutcomeEntry, index: number): string {
  return `policy-outcome:${nonEmptyText(entry.judgment_id) ?? nonEmptyText(entry.recorded_at) ?? `row-${index}`}`;
}

/** One journal row: a policy-outcome entry renders as its own 実装結果
 *  line; anything else is a message entry with proposals + judgments. */
function JournalRow({
  entry,
  busy,
  onJudgment,
}: {
  entry: ConsultationEntry;
  busy: boolean;
  onJudgment: (input: ConsultationJudgmentInput) => void;
}) {
  if (isPolicyOutcomeEntry(entry)) {
    return (
      <div
        data-testid="consultation-policy-outcome"
        data-judgment-id={nonEmptyText(entry.judgment_id) ?? undefined}
      >
        <p>{outcomeLineOf(entry)}</p>
      </div>
    );
  }
  return (
    <div
      data-testid="consultation-entry"
      data-consultation-id={entry.consultation_id}
    >
      <p data-testid="consultation-entry-message">
        相談: {entry.message}（{clockOf(entry.created_at)}）
      </p>
      {entry.proposals.map((proposal) => {
        const recorded = entry.judgments.filter(
          (judgment) => judgment.proposal_id === proposal.proposal_id,
        );
        return (
          <div key={proposal.proposal_id}>
            <ConsultationProposalCard proposal={proposal} />
            {recorded.length > 0 ? (
              <ul className="list-plain" data-testid="consultation-judgments-recorded">
                {recorded.map((judgment) => (
                  <li key={judgment.judgment_id} data-testid="consultation-judgment-recorded">
                    記録済みの判断: {DECISION_LABEL[judgment.decision] ?? judgment.decision}
                    （{scopeSummary(judgment.scope)}）{clockOf(judgment.created_at)}
                    {judgment.note !== null && judgment.note !== "" ? (
                      <span className="field-hint">メモ: {judgment.note}</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : null}
            <ConsultationJudgmentForm
              consultationId={entry.consultation_id}
              proposalId={proposal.proposal_id}
              busy={busy}
              onSubmit={onJudgment}
            />
          </div>
        );
      })}
    </div>
  );
}
export default function ConsultationEntryList({
  payload,
  busy,
  onJudgment,
}: ConsultationEntryListProps) {
  if (payload === null) {
    return (
      <p className="empty-note" data-testid="consultation-loading">
        相談の記録を確認しています…
      </p>
    );
  }
  if (payload.consultations.length === 0) {
    return (
      <p className="empty-note" data-testid="consultation-empty">
        まだ相談はありません。上の入力欄から方針を相談できます。
      </p>
    );
  }
  return (
    <>
      {payload.consultations.map((entry, index) => (
        <JournalRow
          key={
            isPolicyOutcomeEntry(entry)
              ? outcomeKeyOf(entry, index)
              : entry.consultation_id
          }
          entry={entry}
          busy={busy}
          onJudgment={onJudgment}
        />
      ))}
    </>
  );
}
