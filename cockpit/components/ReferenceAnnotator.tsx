"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  parseReferencePreview,
  registerReference,
  CockpitApiError,
  type DomainPolarities,
  type ParsePreviewDraft,
} from "@/lib/api";
import { DOMAIN_LABEL, POLARITY_LABEL } from "@/lib/domains";
import {
  addAnnotation,
  distinctSourceIds,
  loadAnnotations,
  type SavedAnnotation,
} from "@/lib/referenceStore";
import DomainChips from "@/components/DomainChips";
import PairwisePrompt from "@/components/PairwisePrompt";
import ErrorNotice from "@/components/ErrorNotice";

type ReferenceAnnotatorProps = {
  episodeId: string;
  fetchImpl?: typeof fetch;
};

function domainsEqual(a: DomainPolarities, b: DomainPolarities): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const key of keys) {
    if (a[key as keyof DomainPolarities] !== b[key as keyof DomainPolarities]) {
      return false;
    }
  }
  return true;
}

function parseTs(raw: string): number | null {
  if (raw.trim() === "") return null;
  const value = Number(raw);
  return Number.isFinite(value) && value >= 0 ? value : null;
}

/**
 * Reference annotation flow (task 50 / PRD 16.3): free-text comment →
 * deterministic domain/polarity preview (backend keyword parser, no LLM) →
 * operator correction → save. Saving registers the reference file via
 * POST /references (persistent backend library) and keeps the corrected
 * annotation payload in the session store until backend annotation wiring
 * lands (task 51 — gap noted in learnings).
 */
export default function ReferenceAnnotator({
  episodeId,
  fetchImpl,
}: ReferenceAnnotatorProps) {
  const [refPath, setRefPath] = useState("");
  const [tsText, setTsText] = useState("");
  const [comment, setComment] = useState("");
  const [draft, setDraft] = useState<ParsePreviewDraft | null>(null);
  const [domains, setDomains] = useState<DomainPolarities>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ code: string; detail: string } | null>(null);
  const [annotations, setAnnotations] = useState<SavedAnnotation[]>([]);
  const [sourceIds, setSourceIds] = useState<string[]>([]);
  const [pairwiseOpen, setPairwiseOpen] = useState(false);

  useEffect(() => {
    setAnnotations(loadAnnotations());
    setSourceIds(distinctSourceIds());
  }, []);

  const runPreview = async () => {
    if (comment.trim() === "") return;
    setBusy(true);
    setError(null);
    try {
      const ts = parseTs(tsText);
      const next = await parseReferencePreview(
        ts === null ? { text: comment.trim() } : { text: comment.trim(), ts_seconds: ts },
        fetchImpl,
      );
      setDraft(next);
      setDomains(next.domains);
    } catch (cause) {
      if (cause instanceof CockpitApiError) {
        setError({ code: cause.code, detail: cause.detail });
      } else {
        setError({ code: "unexpected-client-error", detail: String(cause) });
      }
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    if (draft === null || refPath.trim() === "") return;
    setBusy(true);
    setError(null);
    try {
      const registered = await registerReference({ path: refPath.trim() }, fetchImpl);
      const corrected = !domainsEqual(domains, draft.domains);
      const saved: SavedAnnotation = {
        episode_id: episodeId,
        source_id: registered.source_id,
        path: refPath.trim(),
        ts_seconds: draft.ts_seconds,
        comment: draft.rationale,
        named_domains: Object.keys(domains) as SavedAnnotation["named_domains"],
        domains,
        corrected,
        saved_at: new Date().toISOString(),
      };
      setAnnotations(addAnnotation(saved));
      setSourceIds(distinctSourceIds());
      setDraft(null);
      setDomains({});
    } catch (cause) {
      if (cause instanceof CockpitApiError) {
        setError({ code: cause.code, detail: cause.detail });
      } else {
        setError({ code: "unexpected-client-error", detail: String(cause) });
      }
    } finally {
      setBusy(false);
    }
  };

  const ambiguityHint = draft !== null && draft.needs_review;
  const showPairwise = pairwiseOpen || ambiguityHint;

  return (
    <section className="card" data-testid="reference-annotator">
      <h2 className="section-title">この瞬間を注釈（参照の好み）</h2>
      <p className="page-subtitle" style={{ marginBottom: "var(--space-3)" }}>
        参照動画に対する自由記述コメントから、ドメインとポラリティを自動プレビューします（決定論的キーワード解析・LLM不使用）。
      </p>
      {error !== null ? <ErrorNotice code={error.code} detail={error.detail} /> : null}
      <label className="field" htmlFor="annotator-ref-path">
        参照動画のパス（ローカルファイル）
        <input
          type="text"
          id="annotator-ref-path"
          value={refPath}
          onChange={(event) => setRefPath(event.target.value)}
          placeholder="/path/to/reference.mp4"
        />
      </label>
      <label className="field" htmlFor="annotator-ts">
        再生位置（秒・任意）
        <input
          type="number"
          id="annotator-ts"
          value={tsText}
          onChange={(event) => setTsText(event.target.value)}
          min="0"
          step="0.1"
          placeholder="例: 12.5"
        />
      </label>
      <label className="field" htmlFor="annotator-comment">
        注釈コメント（日本語で自由記述）
        <textarea
          id="annotator-comment"
          value={comment}
          onChange={(event) => setComment(event.target.value)}
          placeholder="例: 色が良い / 字幕は嫌いだけどテンポは良い"
          style={{ minHeight: "60px" }}
        />
      </label>
      <div className="actions">
        <button
          type="button"
          onClick={() => void runPreview()}
          disabled={busy || comment.trim() === ""}
          data-testid="parse-preview-button"
        >
          ドメイン抽出プレビュー
        </button>
        <button
          type="button"
          className="btn-primary"
          onClick={() => void save()}
          disabled={busy || draft === null || refPath.trim() === ""}
          data-testid="save-annotation-button"
        >
          この注釈を保存
        </button>
      </div>
      {draft !== null ? (
        <div style={{ marginTop: "var(--space-3)" }}>
          <p className="field-hint" data-testid="parse-preview-summary">
            抽出結果: {draft.named_domains.length}件のドメイン / 確度{" "}
            {draft.confidence.toFixed(2)}
          </p>
          {ambiguityHint ? (
            <p className="notice-hint" data-testid="ambiguity-hint">
              コメントが曖昧です（ドメインを特定できません）。手動でドメインを追加するか、A/B比較で好みを伝えられます。
            </p>
          ) : null}
          <DomainChips domains={domains} onChange={setDomains} />
        </div>
      ) : null}
      <div style={{ marginTop: "var(--space-3)" }}>
        <button
          type="button"
          className="btn-small"
          onClick={() => setPairwiseOpen(true)}
          data-testid="pairwise-trigger"
        >
          スタイルを比較（A/B）
        </button>
      </div>
      {showPairwise ? <PairwisePrompt referenceIds={sourceIds} /> : null}
      <div style={{ marginTop: "var(--space-4)" }}>
        <h3 className="section-title">保存済みの注釈（このセッション）</h3>
        <p className="field-hint" data-testid="annotation-scope-note">
          ※参照の登録はバックエンドの参照ライブラリに永続化されます。注釈一覧の全件表示は今後のバックエンド接続で提供されます。
        </p>
        {annotations.length > 0 ? (
          <ul className="list-plain" data-testid="saved-annotation-list">
            {annotations.map((item, index) => (
              <li
                key={`${item.saved_at}-${index}`}
                data-testid="saved-annotation-item"
                style={{ flexDirection: "column", alignItems: "stretch" }}
              >
                <span>
                  {item.comment}（{item.source_id}
                  {item.ts_seconds !== null ? ` @ ${item.ts_seconds}s` : ""}）
                </span>
                <span>
                  {item.named_domains
                    .map(
                      (domain) =>
                        `${DOMAIN_LABEL[domain]}: ${
                          POLARITY_LABEL[item.domains[domain] ?? "unspecified"]
                        }`,
                    )
                    .join(" / ")}
                  {item.corrected ? "（抽出結果を修正済み）" : ""}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="empty-note">まだ保存された注釈はありません。</p>
        )}
        <p>
          <Link href="/references" className="top-link">
            参照一覧へ →
          </Link>
        </p>
      </div>
    </section>
  );
}
