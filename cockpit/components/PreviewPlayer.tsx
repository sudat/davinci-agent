"use client";

import type { RefObject } from "react";
import { previewUrl } from "@/lib/api";

export type PreviewAvailability = "checking" | "available" | "not_generated";

type PreviewPlayerProps = {
  episodeId: string;
  state: PreviewAvailability;
  videoRef: RefObject<HTMLVideoElement | null>;
};

/**
 * Editorial/Presentation preview player over GET /episodes/{id}/preview.
 * "not_generated" is a structured waiting state, never an error.
 */
export default function PreviewPlayer({
  episodeId,
  state,
  videoRef,
}: PreviewPlayerProps) {
  if (state === "checking") {
    return (
      <p data-testid="preview-checking" className="empty-note">
        プレビューの有無を確認しています…
      </p>
    );
  }
  if (state === "not_generated") {
    return (
      <div data-testid="preview-pending" className="preview-pending">
        プレビューはまだ生成されていません
        <p className="field-hint">
          編集判断用の低解像度プレビューが生成されると、ここに再生画面が表示されます。
        </p>
      </div>
    );
  }
  return (
    <video
      data-testid="preview-player"
      className="video-player"
      controls
      preload="metadata"
      src={previewUrl(episodeId)}
      ref={videoRef}
    />
  );
}
