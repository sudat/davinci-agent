"use client";

import { useEffect, useState } from "react";
import { PREFERENCE_DOMAINS, type PreferenceDomain } from "@/lib/api";
import { DOMAIN_LABEL } from "@/lib/domains";
import {
  addPairwiseRecord,
  loadPairwise,
  type PairwiseChoice,
  type SavedPairwise,
} from "@/lib/referenceStore";

type PairwisePromptProps = {
  referenceIds: string[];
};

/** 「どちらも違う」 renders by name; a/b keep the classic letter label. */
export function pairwiseChoiceLabel(choice: PairwiseChoice): string {
  return choice === "neither" ? "どちらも違う" : `${choice.toUpperCase()} を選択`;
}

/**
 * Opportunistic A/B comparison (PRD 16.3: ask only when it matters). The
 * panel is NEVER rendered on plain page load — the parent mounts it only on
 * explicit operator trigger or a backend-supplied ambiguity hint
 * (needs_review). No numeric scorecard exists anywhere on this surface.
 */
export default function PairwisePrompt({ referenceIds }: PairwisePromptProps) {
  const [records, setRecords] = useState<SavedPairwise[]>([]);
  const [domain, setDomain] = useState<PreferenceDomain>("color");
  const [choice, setChoice] = useState<PairwiseChoice | "">("");
  const [reason, setReason] = useState("");

  useEffect(() => {
    setRecords(loadPairwise());
  }, []);

  // 工程2: the reason is OPTIONAL (理由（任意）) — the choice fact alone is
  // recordable; an empty reason is stored as null, never fabricated.
  const canRecord = referenceIds.length >= 2 && choice !== "";

  const record = () => {
    if (!canRecord) return;
    const [a, b] = referenceIds;
    const trimmed = reason.trim();
    const saved: SavedPairwise = {
      domain,
      choice,
      reason: trimmed === "" ? null : trimmed,
      reference_a_id: a,
      reference_b_id: b,
      saved_at: new Date().toISOString(),
    };
    setRecords(addPairwiseRecord(saved));
    setChoice("");
    setReason("");
  };

  return (
    <div className="card" data-testid="pairwise-prompt">
      <h2 className="section-title">見比べて好みを教える</h2>
      <p className="field-hint">スタイルの好みを1つだけ聞きます。</p>
      {referenceIds.length < 2 ? (
        <p className="empty-note" data-testid="pairwise-insufficient">
          見比べには2つの参考動画が必要です。まず参考動画を保存してください。
        </p>
      ) : (
        <div>
          <div className="inline-row" style={{ marginBottom: "var(--space-3)", flexWrap: "wrap" }}>
            <label htmlFor="pairwise-domain" style={{ whiteSpace: "nowrap" }}>比べる点</label>
            <select
              id="pairwise-domain"
              value={domain}
              onChange={(event) => setDomain(event.target.value as PreferenceDomain)}
              data-testid="pairwise-domain"
              style={{ flex: 1, minWidth: 0 }}
            >
              {PREFERENCE_DOMAINS.map((value) => (
                <option key={value} value={value}>
                  {DOMAIN_LABEL[value]}
                </option>
              ))}
            </select>
          </div>
          <div className="inline-row" style={{ marginBottom: "var(--space-3)", flexWrap: "wrap" }}>
            <span>選択:</span>
            <button
              type="button"
              className="choice-button"
              aria-pressed={choice === "a"}
              onClick={() => setChoice("a")}
              data-testid="pairwise-choice-a"
            >
              動画A
            </button>
            <button
              type="button"
              className="choice-button"
              aria-pressed={choice === "b"}
              onClick={() => setChoice("b")}
              data-testid="pairwise-choice-b"
            >
              動画B
            </button>
            <button
              type="button"
              className="choice-button"
              aria-pressed={choice === "neither"}
              onClick={() => setChoice("neither")}
              data-testid="pairwise-choice-neither"
            >
              どちらも違う
            </button>
          </div>
          <label className="field" htmlFor="pairwise-reason">
            理由（任意）
            <textarea
              id="pairwise-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="例: Aの方が落ち着いたテンポだから"
              style={{ minHeight: "60px" }}
              data-testid="pairwise-reason"
            />
          </label>
          <div className="actions">
            <button
              type="button"
              className="btn-primary"
              onClick={record}
              disabled={!canRecord}
              data-testid="pairwise-record"
            >
              記録する
            </button>
          </div>
        </div>
      )}
      {records.length > 0 ? (
        <details className="pairwise-history">
          <summary>これまでの見比べ</summary>
          <ul className="list-plain" data-testid="pairwise-records">
            {records.map((item, index) => (
              <li key={`${item.saved_at}-${index}`} data-testid="saved-pairwise-item">
                <span>
                  {DOMAIN_LABEL[item.domain]}: {pairwiseChoiceLabel(item.choice)}
                  {item.reason !== null ? ` — ${item.reason}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}
