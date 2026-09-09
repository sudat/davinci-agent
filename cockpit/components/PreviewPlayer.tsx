"use client";

import type { RefObject } from "react";
import { previewVideoSrc, type OutputId } from "@/lib/api";

/** Low-resolution preview canvas per output (backend
 *  services/outputs/geometry.py::preview_size_for): landscape 640x360,
 *  vertical 360x640. The element carries the real preview dimensions. */
export const PREVIEW_SIZES: Record<OutputId, { width: number; height: number }> = {
  landscape: { width: 640, height: 360 },
  vertical: { width: 360, height: 640 },
};

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
  /** 工程5: the output this player shows (default landscape). The video
   *  src carries ?output=vertical only for vertical; landscape srcs stay
   *  byte-identical. Vertical elements carry the real 360x640 preview
   *  dimensions; landscape renders exactly as before (no size attrs). */
  output?: OutputId;
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
  output = "landscape",
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
  const vertical = output === "vertical";
  return (
    <video
      key={`${output}/${contentHash ?? "unverified"}`}
      data-testid="preview-player"
      data-output={output}
      className="video-player"
      controls
      preload="metadata"
      src={previewVideoSrc(episodeId, output, contentHash)}
      ref={videoRef}
      {...(vertical
        ? {
            width: PREVIEW_SIZES.vertical.width,
            height: PREVIEW_SIZES.vertical.height,
            style: { maxWidth: PREVIEW_SIZES.vertical.width },
          }
        : {})}
    />
  );
}
