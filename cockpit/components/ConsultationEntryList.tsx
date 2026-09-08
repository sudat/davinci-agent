"use client";

import {
  DECISION_LABEL,
  scopeSummary,
} from "@/components/ConsultationJudgmentForm";
import ConsultationJudgmentForm from "@/components/ConsultationJudgmentForm";
import ConsultationProposalCard from "@/components/ConsultationProposalCard";
import type { ConsultationJudgmentInput, ConsultationPayload } from "@/lib/api";

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

/** The consultation journal: one entry per message with its proposals,
 *  each proposal followed by its recorded (append-only) judgments and
 *  the judgment form. Still-loading / no-consultation states render here. */
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
      {payload.consultations.map((entry) => (
        <div
          key={entry.consultation_id}
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
      ))}
    </>
  );
}
