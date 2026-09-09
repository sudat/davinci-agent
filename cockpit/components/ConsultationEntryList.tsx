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

function verbatimList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => nonEmptyText(item) !== null);
}

/** Slice-2 P1-4 outcome line (design `docs/prd/ux-slice2-p1-fix-design.md`
 *  §P1-4). New writers emit only `connected`/`failed`; legacy `honored`
 *  rows still load but never claim 反映されました. Every field is read
 *  defensively: old data may omit them, so absence renders as 不明 — never
 *  fabricated, never a crash. */
export function outcomeLineOf(entry: ConsultationPolicyOutcomeEntry): string {
  const status: unknown = entry.status;
  if (status === "connected") {
    const version = nonEmptyText(entry.plan_version) ?? "不明";
    const realized = verbatimList(entry.realized_checks);
    const unaddressed = verbatimList(entry.unaddressed);
    let line =
      `採用した方針は編集長への入力に接続されました（対象版 ${version}）。` +
      `内容どおりに実現したかは、確認済みの項目だけを表示しています。`;
    if (realized.length > 0) line += `確認済み: ${realized.join("、")}。`;
    if (unaddressed.length > 0) line += `未確認: ${unaddressed.join("、")}。`;
    return line;
  }
  if (status === "failed") {
    const reasons = verbatimList(entry.reasons);
    const reasonLine = reasons.length > 0 ? `理由：${reasons.join("、")}。` : "";
    const recorded = nonEmptyText(entry.plan_version);
    const recordedLine =
      recorded !== null ? `編集の記録は ${recorded} まで残っています。` : "";
    const connection: unknown = entry.director_connection;
    if (connection === "confirmed") {
      return `方針は編集長に渡されましたが、編集版の確定に失敗しました。${reasonLine}${recordedLine}`;
    }
    if (connection === "not_started") {
      return `方針は編集長に渡していません。理由を確認して相談へ戻れます。${reasonLine}${recordedLine}`;
    }
    if (connection === "unknown") {
      return "方針が編集長へ届いたか確認できません。重複利用を避けて停止しました。";
    }
    const list = reasons;
    return `採用した方針の反映結果：反映できませんでした：${list.length > 0 ? list.join("、") : "理由は不明"}`;
  }
  if (status === "honored") {
    const version = nonEmptyText(entry.plan_version) ?? "不明";
    return (
      `採用した方針は編集長への入力に接続された旧記録です（対象版 ${version}）。` +
      `当時の検証範囲は記録されていません。`
    );
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
