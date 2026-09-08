"use client";

import type { CheckedMaterials, FrameMaterial, ReviewReactionKind } from "@/lib/api";

const MATERIAL_LABELS: ReadonlyArray<[keyof CheckedMaterials, string]> = [
  ["transcript", "周辺の字幕"],
  ["shot", "場面情報"],
];

/** M:SS display position ("0:42"); 付近 covers the sub-second remainder. */
const stillPosition = (atSeconds: number): string => {
  const whole = Math.max(0, Math.floor(atSeconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
};

/** 静止画（フレーム）N枚 with their positions — a STILL is one frame,
 *  never an audio or whole-video (映像/音) verification. */
const stillsLabel = (frames: FrameMaterial[] | undefined): string | null =>
  frames && frames.length > 0
    ? `静止画${frames.length}枚（${frames
        .map((frame) => stillPosition(frame.at_seconds))
        .join("・")}付近）`
    : null;

/**
 * 工程2 response-level honest notices, kept OUT of the draft cards so the
 * 仮説 display (per-draft) and the 確認できたもの display (per response)
 * never merge into one claim. Only the flags'/stills' own materials are
 * listed — video/audio are never checked by 工程2 and never shown here.
 * The stills segment (工程2 rework) lists in 確認できたもの ONLY when
 * `frames_verified` is true (rework round 2 P1-2): frames present without
 * verification are extraction FACTS, not AI-confirmed, and render as a
 * separate subdued 抽出 line instead. Absent `frames` = stills never touched.
 */
export default function ReviewResponseNotice({
  reaction,
  checkedMaterials,
}: {
  reaction: ReviewReactionKind | null;
  checkedMaterials?: CheckedMaterials;
}) {
  if (reaction === "choice-a" || reaction === "choice-b") {
    return (
      <p className="field-hint" data-testid="review-choice-ack">
        選択を記録しました。採用する案のボタンを押してください
      </p>
    );
  }
  const frames = checkedMaterials?.frames;
  const framesVerified = checkedMaterials?.frames_verified === true;
  const extractionNotice =
    frames && frames.length > 0 && !framesVerified
      ? `静止画${frames.length}枚を抽出しましたが、AIでの確認はできていません`
      : null;
  const checked = [
    ...MATERIAL_LABELS.filter(([key]) => checkedMaterials?.[key] === true).map(
      ([, label]) => label,
    ),
    framesVerified ? stillsLabel(frames) : null,
  ].filter((label): label is string => label !== null);
  if (checked.length === 0 && extractionNotice === null) return null;
  if (checked.length === 0) {
    return (
      <p className="field-hint" data-testid="review-frames-extracted">
        {extractionNotice}
      </p>
    );
  }
  return (
    <>
      <p className="field-hint" data-testid="review-checked-materials">
        確認できたもの: {checked.join("・")}
      </p>
      {extractionNotice && (
        <p className="field-hint" data-testid="review-frames-extracted">
          {extractionNotice}
        </p>
      )}
    </>
  );
}
