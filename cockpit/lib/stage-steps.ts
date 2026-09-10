/**
 * Episode screen steps (area B contract — agent A imports the step header
 * from THIS path: `@/lib/stage-steps`, `EPISODE_STEPS` + `currentStage`).
 *
 * The four operator-facing steps never grow new vocabulary: 素材 (footage
 * in, no consultation yet) → 方向 (consultation, no adoption) → 試し動画
 * (adopted + newest published sample) → 全編確認 (full authorized).
 * Derived only from existing consultation state — no new API, no new
 * workflow. Adopted-but-sampleless stays 方向 (the sample request block
 * lives there until the first publish lands).
 */

export { STEPS as EPISODE_STEPS } from "@/lib/episode-stage";
export type { EpisodeStage as EpisodeStep } from "@/lib/episode-stage";

export type StageHint = {
  hasConsultation: boolean;
  adopted: boolean;
  hasPublishedSample: boolean;
  fullAuthorized: boolean;
};

export function currentStage(hint: StageHint | null, consultationEligible: boolean): import("@/lib/episode-stage").EpisodeStage {
  if (hint?.fullAuthorized === true) return "全編確認";
  if (hint !== null) {
    if (hint.adopted && hint.hasPublishedSample) return "試し動画";
    if (hint.hasConsultation || hint.adopted || consultationEligible) return "方向";
    return "素材";
  }
  return consultationEligible ? "方向" : "素材";
}
