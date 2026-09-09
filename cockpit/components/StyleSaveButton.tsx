"use client";

import { useState } from "react";
import {
  apiFailure,
  saveChannelStyle,
  type ConsultationAdoptedPolicy,
} from "@/lib/api";
import type { FetchLike } from "@/lib/http";

type StyleSaveButtonProps = {
  episodeId: string;
  adopted: ConsultationAdoptedPolicy;
  channelId: string | null;
  fetchImpl?: FetchLike;
};

type SaveDone = { version: number; idempotent: boolean };

/**
 * 工程3 explicit style save (U07): rendered ONLY beside an ADOPTED policy,
 * fired ONLY by this button — adoption itself (今回だけ) never writes.
 * Posts the adopted policy's summary fields with a source pointer; 201
 * reports a fresh 版N, 200 reports already-latest. Unknown channel renders
 * an honest line instead of the button (never a guessed channel).
 */
export default function StyleSaveButton({
  episodeId,
  adopted,
  channelId,
  fetchImpl,
}: StyleSaveButtonProps) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<SaveDone | null>(null);
  const [error, setError] = useState<{ code: string; detail: string } | null>(null);

  if (channelId === null) {
    return (
      <p className="field-hint" data-testid="style-save-no-channel">
        保存先のチャンネルが不明のため保存できません（開始時にチャンネルが記録されませんでした）
      </p>
    );
  }

  const save = () => {
    void (async () => {
      const trimmed = name.trim();
      if (trimmed === "" || busy) return;
      setBusy(true);
      setError(null);
      try {
        const result = await saveChannelStyle(
          channelId,
          {
            name: trimmed,
            audience_message: adopted.audience_message,
            structure: adopted.structure,
            duration_estimate: adopted.duration_estimate,
            candidate_scenes: adopted.candidate_scenes,
            subtitle_policy: adopted.subtitle_policy,
            audio_policy: adopted.audio_policy,
            tempo_policy: adopted.tempo_policy,
            reference_mapping: adopted.reference_mapping,
            unused_reasons: adopted.unused_reasons,
            unconfirmed: adopted.unconfirmed,
            note: adopted.note,
            source: {
              episode_id: episodeId,
              judgment_id: adopted.judgment_id,
              proposal_id: adopted.proposal_id,
            },
          },
          fetchImpl ?? fetch,
        );
        setDone({ version: result.version, idempotent: result.idempotent });
      } catch (cause) {
        setError(apiFailure(cause));
      } finally {
        setBusy(false);
      }
    })();
  };

  return (
    <div data-testid="style-save-area">
      <p className="field-hint">
        採用は今回の回だけに使われます（チャンネルの決まりは変わりません）。今後の回でも使いたいときだけ、名前を付けて保存してください。
      </p>
      <p className="field-hint" data-testid="style-save-channel">
        保存先チャンネル: {channelId}
      </p>
      <label className="field" htmlFor="style-save-name">
        スタイル名
        <input
          id="style-save-name"
          type="text"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="例: いつもの台所回"
          data-testid="style-save-name"
        />
      </label>
      <div className="actions">
        <button
          type="button"
          className="btn-small"
          onClick={save}
          disabled={busy || name.trim() === ""}
          data-testid="style-save-button"
        >
          {busy ? "保存中…" : "今後のスタイルとして保存"}
        </button>
      </div>
      {done !== null ? (
        <p className="field-hint" data-testid="style-saved">
          {done.idempotent
            ? `すでに最新のスタイルとして保存済みです（版${done.version}）`
            : `スタイルに保存しました（版${done.version}）`}
        </p>
      ) : null}
      {error !== null ? (
        <p className="field-error" role="alert" data-testid="style-save-error">
          {error.code}: {error.detail}
        </p>
      ) : null}
    </div>
  );
}
