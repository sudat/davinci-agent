"use client";

import { useCallback, useEffect, useState } from "react";
import {
  CockpitApiError,
  getKitPreviews,
  kitPreviewFileUrl,
  selectKitRecipe,
  type KitPreviewsPayload,
} from "@/lib/api";
import ErrorNotice from "@/components/ErrorNotice";

const DOMAIN_LABEL: Record<string, string> = {
  subtitle: "字幕",
  audio: "音声",
  color: "カラー",
};

type KitPreviewPanelProps = {
  episodeId: string;
  fetchImpl?: typeof fetch;
};

/**
 * Production-kit recipe A/B block (task 11): candidate snippets rendered
 * from real episode footage, one <video> per recipe, plus the operator's
 * 採用 / どちらも不要・現状維持 choice recorded into kit-selections.json.
 */
export default function KitPreviewPanel({
  episodeId,
  fetchImpl,
}: KitPreviewPanelProps) {
  const [payload, setPayload] = useState<KitPreviewsPayload | null>(null);
  const [busyDomain, setBusyDomain] = useState<string | null>(null);
  const [error, setError] = useState<{ code: string; detail: string } | null>(null);
  const [notFound, setNotFound] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setPayload(await getKitPreviews(episodeId, fetchImpl));
      setError(null);
      setNotFound(false);
    } catch (cause) {
      if (cause instanceof CockpitApiError) {
        if (cause.status === 404) {
          setNotFound(true);
          return;
        }
        setError({ code: cause.code, detail: cause.detail });
      } else {
        setError({ code: "unexpected-client-error", detail: String(cause) });
      }
    }
  }, [episodeId, fetchImpl]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const choose = async (domain: string, recipeId: string | null) => {
    setBusyDomain(domain);
    setError(null);
    try {
      await selectKitRecipe(
        episodeId,
        domain,
        recipeId === null
          ? { recipe_id: null, note: "どちらも不要/現状維持" }
          : { recipe_id: recipeId },
        fetchImpl,
      );
      await refresh();
    } catch (cause) {
      if (cause instanceof CockpitApiError) {
        setError({ code: cause.code, detail: cause.detail });
      } else {
        setError({ code: "unexpected-client-error", detail: String(cause) });
      }
    } finally {
      setBusyDomain(null);
    }
  };

  if (notFound) {
    return null;
  }

  return (
    <section className="card" data-testid="kit-preview-panel">
      <h2 className="card-title">キットA/Bプレビュー</h2>
      <p className="page-subtitle" style={{ marginBottom: "var(--space-3)" }}>
        実素材の代表シーンを各レシピで snippets 比較し、採用する演出を選びます。
      </p>
      {error !== null ? <ErrorNotice code={error.code} detail={error.detail} /> : null}
      {payload === null ? (
        <p className="empty-note">キットプレビューの有無を確認しています…</p>
      ) : payload.available ? (
        payload.domains.map((domain) => (
          <div
            className="card"
            key={domain.domain}
            data-testid="kit-domain"
            data-domain={domain.domain}
            style={{ marginTop: "var(--space-3)" }}
          >
            <h3 className="section-title">
              {DOMAIN_LABEL[domain.domain] ?? domain.domain}
              {domain.selection === null ? null : (
                <span className="mono" data-testid="kit-selection-state">
                  {" "}
                  選択: {domain.selection}
                </span>
              )}
            </h3>
            <p className="field-hint">
              snippet: {domain.snippet.duration_seconds.toFixed(1)}秒 (
              {domain.snippet.origin})
            </p>
            {domain.candidates.map((candidate) => (
              <div
                key={candidate.recipe_id}
                data-testid="kit-candidate"
                data-recipe-id={candidate.recipe_id}
                style={{ marginTop: "var(--space-3)" }}
              >
                <span className="mono">{candidate.recipe_id}</span>
                <video
                  className="video-player"
                  controls
                  preload="metadata"
                  src={kitPreviewFileUrl(episodeId, candidate.file)}
                  data-testid="kit-candidate-video"
                />
                <p className="field-hint">
                  {Object.entries(candidate.resolved_params)
                    .map(([param, value]) => `${param}=${value}`)
                    .join(" ")}
                </p>
                <div className="actions">
                  <button
                    type="button"
                    className="btn-primary"
                    onClick={() => void choose(domain.domain, candidate.recipe_id)}
                    disabled={busyDomain === domain.domain}
                    data-testid="kit-adopt-button"
                  >
                    このレシピを採用
                  </button>
                </div>
              </div>
            ))}
            <div className="actions">
              <button
                type="button"
                onClick={() => void choose(domain.domain, null)}
                disabled={busyDomain === domain.domain}
                data-testid="kit-none-button"
              >
                どちらも不要/現状維持
              </button>
            </div>
          </div>
        ))
      ) : (
        <p className="empty-note" data-testid="kit-previews-empty">
          キットプレビューはまだ生成されていません
          <span className="field-hint">
            実素材のコミット後に kit previews が生成されると、ここにレシピ毎の再生画面が表示されます。
          </span>
        </p>
      )}
    </section>
  );
}
