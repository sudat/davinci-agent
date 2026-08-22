"use client";

import { useEffect, useState } from "react";
import { PREFERENCE_DOMAINS, type PreferenceDomain } from "@/lib/api";
import { DOMAIN_LABEL } from "@/lib/domains";
import {
  addPairwiseRecord,
  loadPairwise,
  type SavedPairwise,
} from "@/lib/referenceStore";

type PairwisePromptProps = {
  referenceIds: string[];
};

/**
 * Opportunistic A/B comparison (PRD 16.3: ask only when it matters). The
 * panel is NEVER rendered on plain page load — the parent mounts it only on
 * explicit operator trigger or a backend-supplied ambiguity hint
 * (needs_review). No numeric scorecard exists anywhere on this surface.
 */
export default function PairwisePrompt({ referenceIds }: PairwisePromptProps) {
  const [records, setRecords] = useState<SavedPairwise[]>([]);
  const [domain, setDomain] = useState<PreferenceDomain>("color");
  const [choice, setChoice] = useState<"a" | "b" | "">("");
  const [reason, setReason] = useState("");

  useEffect(() => {
    setRecords(loadPairwise());
  }, []);

  const canRecord =
    referenceIds.length >= 2 && choice !== "" && reason.trim() !== "";

  const record = () => {
    if (!canRecord) return;
    const [a, b] = referenceIds;
    const saved: SavedPairwise = {
      domain,
      choice,
      reason: reason.trim(),
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
      <h2 className="section-title">A/B比較（スタイルの好みを1つだけ聞きます）</h2>
      {referenceIds.length < 2 ? (
        <p className="empty-note" data-testid="pairwise-insufficient">
          A/B比較には2つの参照が必要です。まず参照を登録・注釈してください。
        </p>
      ) : (
        <div>
          <div className="inline-row" style={{ marginBottom: "var(--space-3)" }}>
            <label htmlFor="pairwise-domain">ドメイン</label>
            <select
              id="pairwise-domain"
              value={domain}
              onChange={(event) => setDomain(event.target.value as PreferenceDomain)}
              data-testid="pairwise-domain"
            >
              {PREFERENCE_DOMAINS.map((value) => (
                <option key={value} value={value}>
                  {DOMAIN_LABEL[value]}
                </option>
              ))}
            </select>
          </div>
          <div className="inline-row" style={{ marginBottom: "var(--space-3)" }}>
            <span>選択:</span>
            <button
              type="button"
              className="choice-button"
              aria-pressed={choice === "a"}
              onClick={() => setChoice("a")}
              data-testid="pairwise-choice-a"
            >
              A（{referenceIds[0]}）
            </button>
            <button
              type="button"
              className="choice-button"
              aria-pressed={choice === "b"}
              onClick={() => setChoice("b")}
              data-testid="pairwise-choice-b"
            >
              B（{referenceIds[1]}）
            </button>
          </div>
          <label className="field" htmlFor="pairwise-reason">
            理由（必須）
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
        <ul className="list-plain" data-testid="pairwise-records">
          {records.map((item, index) => (
            <li key={`${item.saved_at}-${index}`} data-testid="saved-pairwise-item">
              <span>
                {DOMAIN_LABEL[item.domain]}: {item.choice.toUpperCase()} を選択 —{" "}
                {item.reason}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
