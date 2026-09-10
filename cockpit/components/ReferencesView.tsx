"use client";

import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import {
  apiFailure,
  listReferences,
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
    const impl = fetchImpl ?? fetch;
    void (async () => {
      try {
        const channels = await getChannels(impl);
        if (cancelled || channels.length === 0) return;
        const first = channels[0];
        if (first === undefined) return;
        const next = await getChannelStyle(first.channel_id, { fetchImpl: impl });
        if (cancelled) return;
        setStyle(next);
      } catch {
        if (cancelled) return;
        setStyleFailed(true);
      }
    })();
    return () => {
      cancelled = true;
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
  const styleName = style !== null ? savedStyleName(style) : null;

  return (
    <div data-testid="references-view">
      {error !== null ? <ErrorNotice code={error.code} detail={error.detail} /> : null}
      <div className="refs-grid">
        <section className="card">
          <h2 className="card-title">参考動画を追加</h2>
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
              placeholder="/path/to/reference.mp4"
            />
            <p className="field-hint">
              ここに動画をドラッグ＆ドロップ、またはクリックして選択できます
            </p>
          </div>
          {library === null ? (
            <p className="empty-note">ライブラリを読み込んでいます…</p>
          ) : library.length > 0 ? (
            <ul className="list-plain" data-testid="library-reference-list">
              {library.map((item) => (
                <li key={item.source_id} data-testid="library-reference-item">
                  <span className="ref-thumb" aria-hidden="true" />
                  <span className="ref-memo">{item.location}</span>
                  <details className="ref-internals">
                    <summary>・・・</summary>
                    <span>{item.source_id}</span>
                    <span>{item.sha256}</span>
                  </details>
                </li>
              ))}
            </ul>
          ) : (
            <p className="empty-note" data-testid="library-reference-empty">
              ライブラリに保存済みの参照はまだありません。
            </p>
          )}
          {references.length > 0 ? (
            <ul className="list-plain" data-testid="reference-list">
              {references.map((item, index) => (
                <li key={`${item.source_id}-${index}`} data-testid="reference-item">
                  <span className="ref-thumb" aria-hidden="true" />
                  <span className="ref-memo">{item.path}</span>
                  <details className="ref-internals">
                    <summary>・・・</summary>
                    <span>
                      {item.source_id}（ライブラリ版 {item.library_version}）
                    </span>
                    <span>{item.sha256}</span>
                  </details>
                </li>
              ))}
            </ul>
          ) : null}
          {annotations.length > 0 ? (
            <ul className="list-plain" data-testid="annotation-list">
              {annotations.map((item, index) => (
                <li key={`${item.saved_at}-${index}`} data-testid="annotation-item">
                  <span className="ref-thumb" aria-hidden="true" />
                  <span className="ref-memo">{item.comment}</span>
                  <details className="ref-internals">
                    <summary>・・・</summary>
                    <span>{item.source_id}</span>
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
        </section>
        <section className="card">
          <h2 className="card-title">いつものスタイル</h2>
          {style === null && !styleFailed ? (
            <p className="empty-note">スタイルを確認しています…</p>
          ) : null}
          {styleFailed ? (
            <p className="empty-note">スタイルを確認できませんでした</p>
          ) : null}
          {style !== null && style.current !== null ? (
            <p>{styleName ?? `版${style.current}のスタイル`}</p>
          ) : null}
          {style !== null && style.current === null ? (
            <p className="empty-note">保存されたスタイルはまだありません</p>
          ) : null}
          {compareIds.length >= 2 ? (
            <div style={{ marginTop: "var(--space-4)" }}>
              <h3 className="card-title">どちらが近いですか？</h3>
              <PairwisePrompt referenceIds={compareIds} />
            </div>
          ) : null}
          <div className="actions">
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
      </div>
      <section id="details" className="card">
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
            <p className="empty-note">A/Bの記録はまだありません</p>
          )}
        </details>
      </section>
    </div>
  );
}
