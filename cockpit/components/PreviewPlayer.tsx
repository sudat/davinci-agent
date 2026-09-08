"use client";

import type { RefObject } from "react";
import { previewUrl } from "@/lib/api";

export type PreviewAvailability = "checking" | "available" | "not_generated";

type PreviewPlayerProps = {
  episodeId: string;
  state: PreviewAvailability;
  /** probeが検証したcontent hash。既知ならURLにcontent_hashを付け、
   *  要素をhashでkeyingする — hashが変わればvideoを実reloadedする
   *  （前回実行の.encodeをstale再生しない）。null = 不明（旧バックエンド
   *  等）→ 従来どおりの固定URL。hashは捏造しない。呼び出し側はprobe失敗
   *  時にnullへ戻さず最後の再生可能hashを渡し続ける — key/srcが変わると
   *  video要素が作り直され再生位置を失う。 */
  contentHash: string | null;
  videoRef: RefObject<HTMLVideoElement | null>;
};

/**
 * Editorial/Presentation preview player over GET /episodes/{id}/preview.
 * "not_generated" is a structured waiting state, never an error.
 */
export default function PreviewPlayer({
  episodeId,
  state,
  contentHash,
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
      key={contentHash ?? "unverified"}
      data-testid="preview-player"
      className="video-player"
      controls
      preload="metadata"
      src={
        contentHash === null
          ? previewUrl(episodeId)
          : `${previewUrl(episodeId)}?content_hash=${encodeURIComponent(contentHash)}`
      }
      ref={videoRef}
    />
  );
}
