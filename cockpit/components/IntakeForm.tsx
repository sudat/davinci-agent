"use client";

import { useRef, useState, type DragEvent } from "react";
import { useRouter } from "next/navigation";
import {
  canCreateEpisode,
  validateIntake,
  type IntakeFieldErrors,
} from "@/lib/intake";
import { apiFailure, createEpisode } from "@/lib/api";
import AdvancedSection from "@/components/AdvancedSection";
import ReferenceList from "@/components/ReferenceList";
import ErrorNotice from "@/components/ErrorNotice";

type ApiErrorState = { code: string; detail: string };

/**
 * PRD 13.2 first screen: source folder + natural-language brief are the
 * only required inputs; everything else is defaulted or collapsed. Internal
 * artifact/job IDs are never surfaced here — Create posts exactly
 * {source_folder, brief_text} and navigates to the episode status view.
 */
export default function IntakeForm() {
  const router = useRouter();
  const [sourceFolder, setSourceFolder] = useState("");
  const [briefText, setBriefText] = useState("");
  const [channelProfile, setChannelProfile] = useState("default");
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

  const onSubmit = async () => {
    setTouched(true);
    setApiError(null);
    if (!canCreateEpisode(values)) return;
    setSubmitting(true);
    try {
      const result = await createEpisode({
        source_folder: sourceFolder.trim(),
        brief_text: briefText.trim(),
      });
      router.push(`/episodes/${result.episode_id}`);
    } catch (cause) {
      setApiError(apiFailure(cause));
      setSubmitting(false);
    }
  };

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        void onSubmit();
      }}
    >
      {apiError !== null ? (
        <ErrorNotice code={apiError.code} detail={apiError.detail} />
      ) : null}

      <section className="card">
        <div className="field">
          <label htmlFor="source-folder">ソースフォルダ</label>
          <input
            ref={sourceInputRef}
            id="source-folder"
            name="source_folder"
            type="text"
            placeholder="/Volumes/Camera/2026-08-20_shoot"
            value={sourceFolder}
            onChange={(event) => setSourceFolder(event.target.value)}
            aria-describedby="source-folder-hint"
          />
          {fieldErrors.sourceFolder !== undefined ? (
            <p className="field-error">{fieldErrors.sourceFolder}</p>
          ) : (
            <p className="field-hint" id="source-folder-hint">
              素材フォルダのローカルパス
            </p>
          )}
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
            フォルダのパスをここにドロップ、または上に入力してください
          </div>
        </div>

        <div className="field">
          <label htmlFor="brief-text">この動画は何について？</label>
          <textarea
            id="brief-text"
            name="brief_text"
            placeholder="例: 台所の収納を改善する回。Before/Afterを見せて、コストと手間の内訳を最後にまとめる。"
            value={briefText}
            onChange={(event) => setBriefText(event.target.value)}
          />
          {fieldErrors.briefText !== undefined ? (
            <p className="field-error">{fieldErrors.briefText}</p>
          ) : (
            <p className="field-hint">自然言語で自由に書いてください。</p>
          )}
        </div>

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
            onChange={(event) => setChannelProfile(event.target.value)}
          >
            <option value="default">デフォルト</option>
          </select>
        </div>

        <div className="field">
          <label htmlFor="reference-input">任意の参照</label>
          <ReferenceList
            items={references}
            onAdd={(value) => setReferences((prev) => [...prev, value])}
            onRemove={(index) =>
              setReferences((prev) => prev.filter((_, i) => i !== index))
            }
          />
        </div>
      </section>

      <AdvancedSection />

      <div className="actions">
        <button
          type="submit"
          className="btn-primary"
          disabled={!submittable || submitting}
          data-testid="create-button"
        >
          {submitting ? "作成中…" : "動画を作成"}
        </button>
      </div>
    </form>
  );
}
