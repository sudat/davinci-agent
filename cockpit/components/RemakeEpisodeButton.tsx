"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  apiFailure,
  createEpisode,
  getEpisodeBrief,
} from "@/lib/api";
import type { FetchLike } from "@/lib/http";
import ErrorNotice from "@/components/ErrorNotice";

type RemakeEpisodeButtonProps = {
  episodeId: string;
  /** Same material's source folder. Null when the page cannot know it
   *  (no read endpoint) — then this is a plain link to /new-episode. */
  sourceFolder: string | null;
  fetchImpl?: FetchLike;
};

/**
 * 「同じ素材で最初から作り直す」: a NEW episode from the same material
 * (the backend will soon allow a new episode_id per creation instead of
 * the 1:1 folder binding). The saved brief rides along; nothing is ever
 * deleted. A current-backend duplicate refusal (episode-exists 409)
 * surfaces as the shared error notice with one honest line — the button
 * stays present (feature-detected, never a dead end).
 */
export default function RemakeEpisodeButton({
  episodeId,
  sourceFolder,
  fetchImpl,
}: RemakeEpisodeButtonProps) {
  if (sourceFolder === null) {
    return (
      <div className="actions">
        <Link href="/new-episode" className="btn-small" data-testid="episode-remake">
          同じ素材で最初から作り直す
        </Link>
      </div>
    );
  }
  return (
    <RemakeEpisodeButtonAction
      episodeId={episodeId}
      sourceFolder={sourceFolder}
      fetchImpl={fetchImpl}
    />
  );
}

function RemakeEpisodeButtonAction({
  episodeId,
  sourceFolder,
  fetchImpl,
}: {
  episodeId: string;
  sourceFolder: string;
  fetchImpl?: FetchLike;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ code: string; detail: string } | null>(null);

  const folder = sourceFolder;
  const remake = () => {
    void (async () => {
      if (busy) return;
      setBusy(true);
      setError(null);
      try {
        const impl = fetchImpl ?? fetch;
        const brief = await getEpisodeBrief(episodeId, impl);
        const result = await createEpisode(
          { source_folder: folder, brief_text: brief.brief_text },
          impl,
        );
        router.push(`/episodes/${result.episode_id}`);
      } catch (cause) {
        setError(apiFailure(cause));
      } finally {
        setBusy(false);
      }
    })();
  };

  return (
    <div>
      {error !== null ? (
        <>
          <ErrorNotice code={error.code} detail={error.detail} />
          {error.code === "episode-exists" ? (
            <p className="field-hint" data-testid="episode-remake-duplicate">
              今のバックエンドでは同じ素材の作り直しはまだ受け付けていません。素材を選び直して作る場合は「新しいエピソード」から始められます。
            </p>
          ) : null}
        </>
      ) : null}
      <div className="actions">
        <button
          type="button"
          className="btn-small"
          onClick={remake}
          disabled={busy}
          data-testid="episode-remake"
        >
          {busy ? "作成中…" : "同じ素材で最初から作り直す"}
        </button>
      </div>
    </div>
  );
}
