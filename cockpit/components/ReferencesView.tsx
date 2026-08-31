"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  apiFailure,
  listReferences,
  registerReference,
  type LibraryReference,
} from "@/lib/api";
import { DOMAIN_LABEL, POLARITY_LABEL } from "@/lib/domains";
import {
  addReference,
  loadAnnotations,
  loadReferences,
  type RegisteredReference,
  type SavedAnnotation,
} from "@/lib/referenceStore";
import ErrorNotice from "@/components/ErrorNotice";

type ReferencesViewProps = {
  fetchImpl?: typeof fetch;
};

/**
 * Saved-reference surface (task 50 + 51 wiring): register local reference
 * files into the persistent backend library (idempotent POST
 * /references), list the PERSISTED library (GET /references — survives
 * restarts), and keep this session's registrations + annotations beside
 * it.
 */
export default function ReferencesView({ fetchImpl }: ReferencesViewProps) {
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ code: string; detail: string } | null>(null);
  const [references, setReferences] = useState<RegisteredReference[]>([]);
  const [annotations, setAnnotations] = useState<SavedAnnotation[]>([]);
  const [library, setLibrary] = useState<LibraryReference[] | null>(null);

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
    void refreshLibrary();
  }, [refreshLibrary]);

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

  return (
    <div data-testid="references-view">
      <Link href="/new-episode" className="top-link">
        ← 新しいエピソード
      </Link>
      {error !== null ? <ErrorNotice code={error.code} detail={error.detail} /> : null}
      <section className="card">
        <h2 className="section-title">参照を登録</h2>
        <p className="page-subtitle" style={{ marginBottom: "var(--space-3)" }}>
          ローカルの参照動画ファイルを参照ライブラリに登録します（sha256 を計算して永続化します）。
        </p>
        <label className="field" htmlFor="reference-path-input">
          参照ファイルパス
          <input
            type="text"
            id="reference-path-input"
            value={path}
            onChange={(event) => setPath(event.target.value)}
            placeholder="/path/to/reference.mp4"
          />
        </label>
        <div className="actions">
          <button
            type="button"
            className="btn-primary"
            onClick={() => void register()}
            disabled={busy || path.trim() === ""}
            data-testid="register-reference-button"
          >
            登録
          </button>
        </div>
      </section>
      <section className="card">
        <h2 className="section-title">保存済みの参照ライブラリ（バックエンド永続化）</h2>
        {library === null ? (
          <p className="empty-note">ライブラリを読み込んでいます…</p>
        ) : library.length > 0 ? (
          <ul className="list-plain" data-testid="library-reference-list">
            {library.map((item) => (
              <li
                key={item.source_id}
                data-testid="library-reference-item"
                style={{ flexDirection: "column", alignItems: "stretch" }}
              >
                <span>{item.source_id}</span>
                <span>{item.location}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="empty-note" data-testid="library-reference-empty">
            ライブラリに保存済みの参照はまだありません。
          </p>
        )}
        <h3 className="section-title" style={{ marginTop: "var(--space-4)" }}>
          このセッションで登録
        </h3>
        {references.length > 0 ? (
          <ul className="list-plain" data-testid="reference-list">
            {references.map((item, index) => (
              <li
                key={`${item.source_id}-${index}`}
                data-testid="reference-item"
                style={{ flexDirection: "column", alignItems: "stretch" }}
              >
                <span>
                  {item.source_id}（ライブラリ版 {item.library_version}）
                </span>
                <span>{item.path}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="empty-note">このセッションではまだ参照を登録していません。</p>
        )}
        <h3 className="section-title" style={{ marginTop: "var(--space-4)" }}>
          保存済みの注釈（このセッション）
        </h3>
        {annotations.length > 0 ? (
          <ul className="list-plain" data-testid="annotation-list">
            {annotations.map((item, index) => (
              <li
                key={`${item.saved_at}-${index}`}
                data-testid="annotation-item"
                style={{ flexDirection: "column", alignItems: "stretch" }}
              >
                <span>
                  {item.comment}（{item.source_id}）
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
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="empty-note">
            注釈はエピソード画面の「この瞬間を注釈」から保存します。
          </p>
        )}
      </section>
    </div>
  );
}
