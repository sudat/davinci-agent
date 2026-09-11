"use client";

import { useEffect, useRef, useState, type DragEvent } from "react";
import { useRouter } from "next/navigation";
import {
  canCreateEpisode,
  validateIntake,
  type IntakeFieldErrors,
} from "@/lib/intake";
import { apiFailure, createEpisode } from "@/lib/api";
import {
  getChannels,
  getChannelStyle,
  restoreChannelStyle,
  type ChannelStyle,
} from "@/lib/channel-styles-api";
import type { FetchLike } from "@/lib/http";
import AdvancedSection from "@/components/AdvancedSection";
import ReferenceList from "@/components/ReferenceList";
import ErrorNotice from "@/components/ErrorNotice";

type ApiErrorState = { code: string; detail: string };

type IntakeFormProps = {
  fetchImpl?: FetchLike;
};

function currentNameOf(style: ChannelStyle): string | null {
  if (style.current === null) return null;
  const entry = style.versions.find((item) => item.version === style.current);
  if (entry === undefined || entry.name === "") return null;
  return entry.name;
}

/**
 * PRD 13.2 first screen: source folder + natural-language brief are the
 * only required inputs; everything else is defaulted or collapsed. Internal
 * artifact/job IDs are never surfaced here.
 *
 * 工程3 channel/style: the channel select is fed by GET /channels (a
 * failed/empty list falls back honestly to a single デフォルト option and
 * sends no channel). Picking a channel shows its current style version
 * from GET style; create sends channel + style_version ONLY when the
 * operator explicitly picked the channel. All state derives from server
 * fetches — no sessionStorage. Restore (versions > 1) is operator-only.
 */
export default function IntakeForm({ fetchImpl }: IntakeFormProps) {
  const router = useRouter();
  const [sourceFolder, setSourceFolder] = useState("");
  const [briefText, setBriefText] = useState("");
  const [channelProfile, setChannelProfile] = useState("default");
  const [channels, setChannels] = useState<string[] | null>(null);
  const [channelsFailed, setChannelsFailed] = useState(false);
  const [channelsEmpty, setChannelsEmpty] = useState(false);
  const [channelExplicit, setChannelExplicit] = useState(false);
  const [style, setStyle] = useState<ChannelStyle | null>(null);
  const [styleFailed, setStyleFailed] = useState(false);
  const [restoring, setRestoring] = useState<number | null>(null);
  const [restored, setRestored] = useState<{ target: number; version: number } | null>(null);
  const [restoreError, setRestoreError] = useState<ApiErrorState | null>(null);
  const [targetLengthMode, setTargetLengthMode] = useState("auto");
  const [references, setReferences] = useState<string[]>([]);
  const [dropActive, setDropActive] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [apiError, setApiError] = useState<ApiErrorState | null>(null);
  const [touched, setTouched] = useState(false);
  const sourceInputRef = useRef<HTMLInputElement>(null);

  const values = { sourceFolder, briefText };
  const submittable = canCreateEpisode(values);
  const fieldErrors: IntakeFieldErrors = touched
    ? validateIntake(values)
    : {};

  useEffect(() => {
    let cancelled = false;
    const impl: FetchLike = fetchImpl ?? fetch;
    void (async () => {
      try {
        const list = await getChannels(impl);
        if (cancelled) return;
        if (list.length === 0) {
          setChannels(["default"]);
          setChannelsEmpty(true);
        } else {
          const ids = list.map((entry) => entry.channel_id);
          setChannels(ids);
          setChannelProfile(ids[0] ?? "default");
        }
      } catch {
        if (cancelled) return;
        setChannels(["default"]);
        setChannelsFailed(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [fetchImpl]);

  useEffect(() => {
    if (channels === null) return;
    let cancelled = false;
    const impl: FetchLike = fetchImpl ?? fetch;
    setStyle(null);
    setStyleFailed(false);
    setRestored(null);
    setRestoreError(null);
    void (async () => {
      try {
        const next = await getChannelStyle(channelProfile, { fetchImpl: impl });
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
  }, [channels, channelProfile, fetchImpl]);

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDropActive(false);
    // Browsers do not expose absolute folder paths from a file drop; accept
    // dropped text (a path dragged as text) and otherwise hint manual entry.
    const dropped = event.dataTransfer.getData("text/plain").trim();
    if (dropped !== "") {
      setSourceFolder(dropped);
    } else {
      sourceInputRef.current?.focus();
    }
  };

  const restoreTo = (target: number) => {
    const impl: FetchLike = fetchImpl ?? fetch;
    void (async () => {
      if (restoring !== null) return;
      setRestoring(target);
      setRestoreError(null);
      try {
        const result = await restoreChannelStyle(channelProfile, target, impl);
        const next = await getChannelStyle(channelProfile, { fetchImpl: impl });
        setStyle(next);
        setRestored({ target, version: result.version });
      } catch (cause) {
        setRestoreError(apiFailure(cause));
      } finally {
        setRestoring(null);
      }
    })();
  };

  const onSubmit = async () => {
    setTouched(true);
    setApiError(null);
    if (!canCreateEpisode(values)) return;
    setSubmitting(true);
    try {
      const result = await createEpisode(
        {
          source_folder: sourceFolder.trim(),
          brief_text: briefText.trim(),
          ...(channelExplicit && !channelsFailed && channels !== null && channels.length > 0
            ? { channel: channelProfile }
            : {}),
          ...(channelExplicit &&
          !channelsFailed &&
          style !== null &&
          style.current !== null
            ? { style_version: style.current }
            : {}),
        },
        fetchImpl ?? fetch,
      );
      router.push(`/episodes/${result.episode_id}`);
    } catch (cause) {
      setApiError(apiFailure(cause));
      setSubmitting(false);
    }
  };

  const currentName = style !== null ? currentNameOf(style) : null;
  const hasSavedStyle = style !== null && style.current !== null;

  return (
    <form
      className="p1"
      onSubmit={(event) => {
        event.preventDefault();
        void onSubmit();
      }}
    >
      {apiError !== null ? (
        <ErrorNotice code={apiError.code} detail={apiError.detail} />
      ) : null}

      <section className="p1-main" aria-label="素材と作りたい動画">
        <div className="field">
          <div
            className={`dropzone${dropActive ? " is-over" : ""}`}
            data-testid="source-dropzone"
            onDragOver={(event) => {
              event.preventDefault();
              setDropActive(true);
            }}
            onDragLeave={() => setDropActive(false)}
            onDrop={onDrop}
          >
            <p className="dropzone-title">
              <label htmlFor="source-folder">撮影素材のフォルダを選ぶ</label>
            </p>
            <input
              ref={sourceInputRef}
              id="source-folder"
              name="source_folder"
              type="text"
              placeholder="フォルダのパス"
              value={sourceFolder}
              onChange={(event) => setSourceFolder(event.target.value)}
              aria-describedby="source-folder-hint"
            />
            {fieldErrors.sourceFolder !== undefined ? (
              <p className="field-error">{fieldErrors.sourceFolder}</p>
            ) : (
              <p className="field-hint" id="source-folder-hint">
                ここにフォルダをドラッグ＆ドロップ、または欄にパスを貼り付け
              </p>
            )}
          </div>
        </div>

        <div className="field">
          <label htmlFor="brief-text">どんな動画にしたいですか？</label>
          <textarea
            id="brief-text"
            name="brief_text"
            placeholder="みなとみらいを落ち着いた雰囲気で紹介したい"
            value={briefText}
            onChange={(event) => setBriefText(event.target.value)}
          />
          {fieldErrors.briefText !== undefined ? (
            <p className="field-error">{fieldErrors.briefText}</p>
          ) : (
            <p className="field-hint">自然言語で自由に書いてください。</p>
          )}
        </div>

        {hasSavedStyle ? (
          <div className="field">
            <label>
              <input
                type="checkbox"
                checked={channelExplicit}
                onChange={(event) => setChannelExplicit(event.target.checked)}
                data-testid="style-opt-in"
              />{" "}
              いつものスタイルを使う
            </label>
            <p className="field-hint" data-testid="channel-style-current">
              {currentName !== null
                ? `使われるスタイル: ${currentName}（版${style.current}）`
                : `使われるスタイル: 版${style.current}`}
            </p>
          </div>
        ) : null}
      </section>

      <div className="p1-cta">
        <button
          type="submit"
          className="btn-primary"
          disabled={!submittable || submitting}
          data-testid="create-button"
        >
          {submitting ? "作成中…" : "動画づくりを始める"}
        </button>
        {!submittable && touched ? (
          <p className="field-hint">
            素材フォルダと「どんな動画にしたいか」の両方がそろうと始められます
          </p>
        ) : null}
      </div>

      <details className="advanced" data-testid="reference-collapse">
        <summary>参考動画</summary>
        <div className="advanced-body">
          <div className="field">
            <label htmlFor="reference-input">参考動画の追加（任意）</label>
            <ReferenceList
              items={references}
              onAdd={(value) => setReferences((prev) => [...prev, value])}
              onRemove={(index) =>
                setReferences((prev) => prev.filter((_, i) => i !== index))
              }
            />
          </div>
        </div>
      </details>

      <details className="advanced" data-testid="detail-settings">
        <summary>詳細設定</summary>
        <div className="advanced-body">
          <div className="field">
            <label htmlFor="target-length">目標尺</label>
            <select
              id="target-length"
              name="target_length"
              value={targetLengthMode}
              onChange={(event) => setTargetLengthMode(event.target.value)}
            >
              <option value="auto">自動</option>
              <option value="custom">指定（高度な設定で範囲入力）</option>
            </select>
          </div>

          <div className="field">
            <label htmlFor="channel-profile">チャンネルプロファイル</label>
            <select
              id="channel-profile"
              name="channel_profile"
              value={channelProfile}
              disabled={channels === null}
              onChange={(event) => {
                setChannelProfile(event.target.value);
                setChannelExplicit(true);
              }}
              data-testid="channel-select"
            >
              {channels === null ? (
                <option value="default">読み込み中…</option>
              ) : (
                channels.map((id) => (
                  <option key={id} value={id}>
                    {id === "default" ? "デフォルト" : id}
                  </option>
                ))
              )}
            </select>
            {channelsFailed || (channelsEmpty && !channelExplicit) ? (
              <p className="field-hint" data-testid="channels-fallback">
                {channelsFailed
                  ? "チャンネル一覧を取得できませんでした。デフォルトで作成します"
                  : "登録されているチャンネルがありません。デフォルトで作成します"}
              </p>
            ) : null}
          </div>

          {channels !== null && !hasSavedStyle && !styleFailed ? (
            <div className="field">
              {style === null ? (
                <p className="field-hint">スタイルを確認しています…</p>
              ) : null}
            </div>
          ) : null}
        </div>
      </details>

      <AdvancedSection />

      <section id="details" className="p1-record">
        <details data-testid="intake-record">
          <summary>詳しい記録</summary>
          {style !== null && style.versions.length > 1 ? (
            <div>
              <p className="field-hint">
                以前の版に戻せます（押したときだけ保存されます）。
              </p>
              <ul className="list-plain">
                {style.versions.map((entry) => (
                  <li
                    key={entry.version}
                    data-testid={`style-version-${entry.version}`}
                  >
                    版{entry.version} {entry.name}（{entry.saved_at}）
                    {entry.version !== style.current ? (
                      <button
                        type="button"
                        className="btn-small"
                        disabled={restoring !== null}
                        onClick={() => restoreTo(entry.version)}
                        data-testid={`style-restore-${entry.version}`}
                      >
                        この版に戻す
                      </button>
                    ) : null}
                  </li>
                ))}
              </ul>
              {restored !== null ? (
                <p className="field-hint" data-testid="style-restored">
                  版{restored.target}の内容で新版{restored.version}として保存しました
                </p>
              ) : null}
              {restoreError !== null ? (
                <p className="field-error" data-testid="style-restore-error">
                  {restoreError.code}: {restoreError.detail}
                </p>
              ) : null}
            </div>
          ) : null}
          {style !== null && style.current === null ? (
            <p className="field-hint" data-testid="channel-style-empty">
              このチャンネルに保存されたスタイルはまだありません
            </p>
          ) : null}
          {styleFailed ? (
            <p className="field-hint" data-testid="style-unavailable-record">
              スタイルを確認できませんでした
            </p>
          ) : null}
        </details>
      </section>
    </form>
  );
}
