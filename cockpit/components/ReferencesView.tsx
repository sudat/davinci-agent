"use client";

import { useCallback, useEffect, useRef, useState, type DragEvent, type SyntheticEvent } from "react";
import {
  apiFailure,
  listReferences,
  referencePreviewUrl,
  registerReference,
  type LibraryReference,
} from "@/lib/api";
import { DOMAIN_LABEL, POLARITY_LABEL } from "@/lib/domains";
import { getChannels, getChannelStyle, type ChannelStyle } from "@/lib/channel-styles-api";
import {
  addReference,
  distinctSourceIds,
  loadAnnotations,
  loadPairwise,
  loadReferences,
  type RegisteredReference,
  type SavedAnnotation,
  type SavedPairwise,
} from "@/lib/referenceStore";
import ErrorNotice from "@/components/ErrorNotice";
import PairwisePrompt, { pairwiseChoiceLabel } from "@/components/PairwisePrompt";

type ReferencesViewProps = {
  fetchImpl?: typeof fetch;
};

function savedStyleName(style: ChannelStyle): string | null {
  if (style.current === null) return null;
  const entry = style.versions.find((item) => item.version === style.current);
  if (entry === undefined || entry.name === "") return null;
  return entry.name;
}

function baseNameOf(location: string): string {
  const parts = location.split("/").filter((part) => part !== "");
  return parts[parts.length - 1] ?? location;
}

/** Plain short display name derived from the stored path only: basename
 *  minus the final extension, separators folded to spaces. */
export function plainNameOf(location: string): string {
  const base = baseNameOf(location);
  const dot = base.lastIndexOf(".");
  const stem = dot > 0 ? base.slice(0, dot) : base;
  return stem.replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim() || base;
}

export default function ReferencesView({ fetchImpl }: ReferencesViewProps) {
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [dropActive, setDropActive] = useState(false);
  const [error, setError] = useState<{ code: string; detail: string } | null>(null);
  const [references, setReferences] = useState<RegisteredReference[]>([]);
  const [annotations, setAnnotations] = useState<SavedAnnotation[]>([]);
  const [pairwise, setPairwise] = useState<SavedPairwise[]>([]);
  const [library, setLibrary] = useState<LibraryReference[] | null>(null);
  const [style, setStyle] = useState<ChannelStyle | null>(null);
  const [styleFailed, setStyleFailed] = useState(false);
  /** source_ids whose preview video failed to load (missing file, 404).
   *  The comparison UI only shows while 2+ previews stay playable. */
  const [failedPreviews, setFailedPreviews] = useState<string[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);

  const refreshLibrary = useCallback(async () => {
    try {
      setLibrary((await listReferences(fetchImpl)).references);
    } catch (cause) {
      setError(apiFailure(cause));
    }
  }, [fetchImpl]);

  useEffect(() => {
    setReferences(loadReferences());
    setAnnotations(loadAnnotations());
    setPairwise(loadPairwise());
    void refreshLibrary();
  }, [refreshLibrary]);

  useEffect(() => {
    let cancelled = false;
    let settled = false;
    const impl = fetchImpl ?? fetch;
    // P6: スタイル取得は確定状態で終わらせる。channels空（未保存）も
    // 応答なしの停滞も、無限の「確認しています…」にしない。
    const settle = (apply: () => void) => {
      if (cancelled || settled) return;
      settled = true;
      clearTimeout(timer);
      apply();
    };
    const timer = setTimeout(() => {
      settle(() => setStyleFailed(true));
    }, 8000);
    void (async () => {
      try {
        const channels = await getChannels(impl);
        if (cancelled || settled) return;
        if (channels.length === 0) {
          settle(() => setStyle({ channel_id: "", versions: [], current: null }));
          return;
        }
        const first = channels[0];
        if (first === undefined) {
          settle(() => setStyleFailed(true));
          return;
        }
        const next = await getChannelStyle(first.channel_id, { fetchImpl: impl });
        settle(() => setStyle(next));
      } catch {
        settle(() => setStyleFailed(true));
      }
    })();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [fetchImpl]);

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDropActive(false);
    const dropped = event.dataTransfer.getData("text/plain").trim();
    if (dropped !== "") {
      setPath(dropped);
    } else {
      inputRef.current?.focus();
    }
  };

  const register = async () => {
    if (path.trim() === "") return;
    setBusy(true);
    setError(null);
    try {
      const result = await registerReference({ path: path.trim() }, fetchImpl);
      setReferences(
        addReference({
          source_id: result.source_id,
          path: path.trim(),
          sha256: result.sha256,
          library_version: result.library_version,
          registered_at: new Date().toISOString(),
        }),
      );
      setPath("");
      await refreshLibrary();
    } catch (cause) {
      setError(apiFailure(cause));
    } finally {
      setBusy(false);
    }
  };

  const libraryIds = (library ?? []).map((item) => item.source_id);
  const sessionIds = distinctSourceIds();
  const compareIds = [...libraryIds, ...sessionIds.filter((id) => !libraryIds.includes(id))];
  const playableIds = compareIds.filter((id) => !failedPreviews.includes(id));
  const styleName = style !== null ? savedStyleName(style) : null;

  const markPreviewFailed = useCallback((sourceId: string) => {
    setFailedPreviews((prev) => (prev.includes(sourceId) ? prev : [...prev, sourceId]));
  }, []);

  /** HEVC などブラウザがデコードできない映像は、metadata（duration、
   *  readyState>0）は読めるのに最初のフレームが復号できず videoWidth が
   *  0 のままになる。onError は発火しないため、loadedmetadata 後の
   *  videoWidth === 0 も再生不能として扱う。 */
  const checkPreviewDecodable = useCallback(
    (sourceId: string) => (event: SyntheticEvent<HTMLVideoElement>) => {
      if (event.currentTarget.videoWidth === 0) {
        markPreviewFailed(sourceId);
      }
    },
    [markPreviewFailed],
  );

  return (
    <div className="p6" data-testid="references-view">
      {error !== null ? <ErrorNotice code={error.code} detail={error.detail} /> : null}
      <div className="refs-grid">
        <section className="p6-add" aria-label="参考動画の追加">
          <h2 className="p6-region-title">参考動画を追加</h2>
          <p className="p6-lead">好きな動画を1件ずつ足して、その場で好みとして保存します。</p>
          <div
            className={`dropzone${dropActive ? " is-over" : ""}`}
            data-testid="reference-dropzone"
            onDragOver={(event) => {
              event.preventDefault();
              setDropActive(true);
            }}
            onDragLeave={() => setDropActive(false)}
            onDrop={onDrop}
          >
            <p className="dropzone-title">参考動画を追加</p>
            <input
              ref={inputRef}
              type="text"
              id="reference-path-input"
              aria-label="参照ファイルパス"
              value={path}
              onChange={(event) => setPath(event.target.value)}
              placeholder="参考動画のフォルダまたはファイル"
            />
            <p className="field-hint">
              ここに動画をドラッグ＆ドロップ、または下の欄に場所を書いて保存できます
            </p>
          </div>
          <div className="p6-cta">
            <button
              type="button"
              className="btn-primary"
              onClick={() => void register()}
              disabled={busy || path.trim() === ""}
              data-testid="register-reference-button"
            >
              好みを保存
            </button>
          </div>
        </section>
        <section className="p6-saved" aria-label="保存済みの好み">
          <h2 className="p6-region-title">保存済みの好み</h2>
          <h3 className="p6-sub-title">いつものスタイル</h3>
          {style === null && !styleFailed ? (
            <p className="empty-note">スタイルを確認しています…</p>
          ) : null}
          {styleFailed ? (
            <p className="empty-note">スタイルを確認できませんでした</p>
          ) : null}
          {style !== null && style.current !== null ? (
            <p className="p6-style-name">{styleName ?? `版${style.current}のスタイル`}</p>
          ) : null}
          {style !== null && style.current === null ? (
            <p className="empty-note">保存されたスタイルはまだありません</p>
          ) : null}
          <h3 className="p6-sub-title">保存済みの動画</h3>
          {library === null ? (
            <p className="empty-note">ライブラリを読み込んでいます…</p>
          ) : library.length > 0 ? (
            <ul className="list-plain" data-testid="library-reference-list">
              {library.map((item) => {
                const slot = compareIds.indexOf(item.source_id);
                // Every saved reference plays through the read-only preview
                // route keyed by source_id — video A and video B alike.
                const previewUrl = referencePreviewUrl(item.source_id);
                return (
                  <li key={item.source_id} data-testid="library-reference-item">
                    <video
                      className="ref-video"
                      data-testid="library-reference-preview"
                      src={previewUrl}
                      preload="metadata"
                      controls
                      onError={() => markPreviewFailed(item.source_id)}
                      onLoadedMetadata={checkPreviewDecodable(item.source_id)}
                    />
                    <span className="ref-memo">
                      {slot === 0 ? (
                        <span className="ref-slot">動画A</span>
                      ) : slot === 1 ? (
                        <span className="ref-slot">動画B</span>
                      ) : null}
                      <span className="ref-name">{plainNameOf(item.location)}</span>
                    </span>
                    <details className="ref-internals">
                      <summary>この動画の詳細</summary>
                      <span className="ref-path">{item.location}</span>
                      <span>{item.sha256}</span>
                    </details>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="empty-note" data-testid="library-reference-empty">
              ライブラリに保存済みの参照はまだありません。
            </p>
          )}
          {references.length > 0 ? (
            <ul className="list-plain" data-testid="reference-list">
              {references.map((item, index) => {
                const slot = compareIds.indexOf(item.source_id);
                const previewUrl = referencePreviewUrl(item.source_id);
                return (
                  <li key={`${item.source_id}-${index}`} data-testid="reference-item">
                    <video
                      className="ref-video"
                      data-testid="reference-preview"
                      src={previewUrl}
                      preload="metadata"
                      controls
                      onError={() => markPreviewFailed(item.source_id)}
                      onLoadedMetadata={checkPreviewDecodable(item.source_id)}
                    />
                    <span className="ref-memo">
                      {slot === 0 ? (
                        <span className="ref-slot">動画A</span>
                      ) : slot === 1 ? (
                        <span className="ref-slot">動画B</span>
                      ) : null}
                      <span className="ref-name">{plainNameOf(item.path)}</span>
                      <span className="visually-hidden">{item.source_id}</span>
                    </span>
                    <details className="ref-internals">
                      <summary>この動画の詳細</summary>
                      <span className="ref-path">{item.path}</span>
                      <span>ライブラリ版 {item.library_version}</span>
                      <span>{item.sha256}</span>
                    </details>
                  </li>
                );
              })}
            </ul>
          ) : null}
          {annotations.length > 0 ? (
            <ul className="list-plain" data-testid="annotation-list">
              {annotations.map((item, index) => (
                <li key={`${item.saved_at}-${index}`} data-testid="annotation-item">
                  <span className="ref-thumb" aria-hidden="true" />
                  <span className="ref-memo">{item.comment}</span>
                  <details className="ref-internals">
                    <summary>保存の詳細</summary>
                    <span>
                      {item.named_domains
                        .map(
                          (domain) =>
                            `${DOMAIN_LABEL[domain]}: ${
                              POLARITY_LABEL[item.domains[domain] ?? "unspecified"]
                            }`,
                        )
                        .join(" / ")}
                    </span>
                  </details>
                </li>
              ))}
            </ul>
          ) : null}
          {compareIds.length >= 2 && playableIds.length >= 2 ? (
            <div className="p6-compare">
              <h3 className="p6-sub-title">どちらが近いですか？</h3>
              <PairwisePrompt referenceIds={compareIds} />
            </div>
          ) : null}
          {compareIds.length >= 2 && playableIds.length < 2 ? (
            <p className="empty-note" data-testid="reference-compare-unavailable">
              比較するには再生できる動画が2本必要です
            </p>
          ) : null}
        </section>
      </div>
      <section id="details" className="p6-record">
        <details data-testid="references-record">
          <summary>詳しい記録</summary>
          {pairwise.length > 0 ? (
            <ul className="list-plain" data-testid="pairwise-records">
              {pairwise.map((item, index) => (
                <li key={`${item.saved_at}-${index}`}>
                  <span>
                    {DOMAIN_LABEL[item.domain]}: {pairwiseChoiceLabel(item.choice)}
                    {item.reason !== null ? ` — ${item.reason}` : ""}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="empty-note">見比べの記録はまだありません</p>
          )}
        </details>
      </section>
    </div>
  );
}
