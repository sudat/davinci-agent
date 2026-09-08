"use client";

import { useState } from "react";
import type {
  ConsultationDecision,
  ConsultationJudgmentInput,
  ConsultationScope,
} from "@/lib/api";

export const DECISION_ORDER: ConsultationDecision[] = [
  "adopt",
  "revise",
  "reject",
  "both_wrong",
  "delegate",
];

export const DECISION_LABEL: Record<ConsultationDecision, string> = {
  adopt: "この方針を採用",
  revise: "コメントで修正する",
  reject: "見送る",
  both_wrong: "どちらも違う",
  delegate: "いつもの方向性におまかせ",
};

const DECISION_TESTID: Record<ConsultationDecision, string> = {
  adopt: "consultation-judgment-adopt",
  revise: "consultation-judgment-revise",
  reject: "consultation-judgment-reject",
  both_wrong: "consultation-judgment-both-wrong",
  delegate: "consultation-judgment-delegate",
};

const SCOPE_ORDER: Array<keyof ConsultationScope> = [
  "composition",
  "appearance",
  "audio",
];

export const SCOPE_LABEL: Record<keyof ConsultationScope, string> = {
  composition: "構成",
  appearance: "見た目",
  audio: "音声",
};

const SCOPE_TESTID: Record<keyof ConsultationScope, string> = {
  composition: "consultation-scope-composition",
  appearance: "consultation-scope-appearance",
  audio: "consultation-scope-audio",
};

export function scopeSummary(scope: ConsultationScope): string {
  const chosen = SCOPE_ORDER.filter((key) => scope[key]).map(
    (key) => SCOPE_LABEL[key],
  );
  return chosen.length > 0 ? chosen.join("・") : "範囲指定なし";
}

type ConsultationJudgmentFormProps = {
  consultationId: string;
  proposalId: string;
  busy: boolean;
  onSubmit: (input: ConsultationJudgmentInput) => void;
};

/**
 * Per-proposal judgment form (UX 2.5 slice-1): the submit stays disabled
 * until a decision is chosen — 判断しないことは同意にはならない — and the
 * adopt action names what it really is: 方針の採用であって、最終動画や
 * 公開の承認ではない。Scope defaults to the whole proposal (all three);
 * unchecking narrows what the judgment covers.
 */
export default function ConsultationJudgmentForm({
  consultationId,
  proposalId,
  busy,
  onSubmit,
}: ConsultationJudgmentFormProps) {
  const [decision, setDecision] = useState<ConsultationDecision | null>(null);
  const [scope, setScope] = useState<ConsultationScope>({
    composition: true,
    appearance: true,
    audio: true,
  });
  const [noteText, setNoteText] = useState("");

  const toggleScope = (key: keyof ConsultationScope) =>
    setScope((prev) => ({ ...prev, [key]: !prev[key] }));

  const submit = () => {
    if (decision === null) return;
    const note = noteText.trim();
    onSubmit({
      consultation_id: consultationId,
      proposal_id: proposalId,
      decision,
      scope,
      note: note === "" ? null : note,
    });
    setDecision(null);
    setNoteText("");
  };

  return (
    <div data-testid="consultation-judgment-form">
      <p className="field-hint">
        この提案への判断を選んでください。判断しないことは同意にはなりません。
      </p>
      <div className="chip-row" role="group" aria-label="提案への判断">
        {DECISION_ORDER.map((value) => (
          <button
            key={value}
            type="button"
            className="choice-button"
            aria-pressed={decision === value}
            disabled={busy}
            onClick={() => setDecision(value)}
            data-testid={DECISION_TESTID[value]}
          >
            {DECISION_LABEL[value]}
          </button>
        ))}
      </div>
      {decision === "adopt" ? (
        <p className="field-hint" data-testid="consultation-adopt-note">
          「この方針を採用」は編集の方針を採用することです。最終動画や公開の承認ではありません。
        </p>
      ) : null}
      <div className="inline-row" role="group" aria-label="判断の範囲">
        <span className="field-hint">範囲:</span>
        {SCOPE_ORDER.map((key) => (
          <label key={key}>
            <input
              type="checkbox"
              checked={scope[key]}
              onChange={() => toggleScope(key)}
              data-testid={SCOPE_TESTID[key]}
            />
            {SCOPE_LABEL[key]}
          </label>
        ))}
      </div>
      <label className="field" htmlFor={`consultation-judgment-note-${proposalId}`}>
        メモ（任意）
        <textarea
          id={`consultation-judgment-note-${proposalId}`}
          rows={2}
          value={noteText}
          onChange={(event) => setNoteText(event.target.value)}
          placeholder="例: 構成は良いが、字幕はもう少し少なめで"
          data-testid="consultation-judgment-note"
        />
      </label>
      <div className="actions">
        <button
          type="button"
          className="btn-primary"
          onClick={submit}
          disabled={busy || decision === null}
          data-testid="consultation-judgment-submit"
        >
          {busy ? "送信中…" : "判断を送信"}
        </button>
      </div>
      {decision === null ? (
        <p className="field-hint" data-testid="consultation-judgment-gate">
          判断を選ぶまで送信できません。
        </p>
      ) : null}
    </div>
  );
}
