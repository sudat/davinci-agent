/**
 * 画面上部の4段階表示（素材 → 方向 → 試し動画 → 全編確認）の定義と、
 * 既存の取得済みpayloadだけから現在段階を導く純粋関数。
 *
 * 導出元は既存フィールドのみ — 新しいbackend項目は使わない:
 * - 相談の有無: consultation payload の message 行の有無
 * - 採用方針の有無: view.policy.adopted（top-level または各行の rider）
 * - 試し動画の有無: consultation samples / preview_first_arrived_at
 * - 全編の許可の有無: full_authorized 判断 / self_check 回答 / 仕上げ段階
 * - chain の状態: status.current_stage（resolve_build/qc/render/publish 等）
 */

import type { EpisodeStatus } from "@/lib/episode-api";
import {
  isPolicyOutcomeEntry,
  isSamplePublished,
  type ConsultationPayload,
  type ConsultationSamplesPayload,
} from "@/lib/consultation-api";

export const STEPS = ["素材", "方向", "試し動画", "全編確認"] as const;

export type EpisodeStage = (typeof STEPS)[number];

/** 仕上げ以降の chain 段階 — ここにいたら全編確認の段階。 */
const LATE_CHAIN_STAGES = ["resolve_build", "qc", "render", "publish"];

/** 試し動画の chain 段階 — ここにいたら試し動画の段階。 */
const SAMPLE_CHAIN_STAGES = ["compile", "preview"];

/** 方向決めの chain 段階 — ここにいたら方向の段階。 */
const DIRECTION_CHAIN_STAGES = ["selection", "plan"];

export type StageSignals = {
  status: EpisodeStatus | null;
  consultations: ConsultationPayload | null;
  samples: ConsultationSamplesPayload | null;
};

function hasAdoptedPolicy(consultations: ConsultationPayload | null): boolean {
  if (consultations === null) return false;
  if (consultations.policy?.adopted != null) return true;
  for (const entry of consultations.consultations) {
    if (isPolicyOutcomeEntry(entry)) continue;
    if (entry.policy?.adopted != null) return true;
  }
  return false;
}

function hasFullAuthorization(consultations: ConsultationPayload | null): boolean {
  if (consultations === null) return false;
  // Same rule as the panel/backend: strip the trailing full_authorized run;
  // only an adopt/revise IMMEDIATELY before it is authorized. A later
  // adoption returns the flow to its own sample stage.
  const decisions: string[] = [];
  for (const entry of consultations.consultations) {
    if (isPolicyOutcomeEntry(entry)) continue;
    for (const judgment of entry.judgments) decisions.push(judgment.decision);
  }
  let end = decisions.length;
  while (end > 0 && decisions[end - 1] === "full_authorized") end--;
  if (end === decisions.length || end === 0) return false;
  const preceding = decisions[end - 1];
  return preceding === "adopt" || preceding === "revise";
}

function consultationCount(consultations: ConsultationPayload | null): number {
  if (consultations === null) return 0;
  let count = 0;
  for (const entry of consultations.consultations) {
    if (!isPolicyOutcomeEntry(entry)) count += 1;
  }
  return count;
}

/**
 * 段階を導く（上位から最初に当てはまったものを返す）。
 * 取得失敗・未取得（null）は「まだその証拠はない」として下位へ譲る —
 * 不明を上位に捏造しない。
 */
export function deriveStage(signals: StageSignals): EpisodeStage {
  const { status, consultations, samples } = signals;
  const stage = status?.current_stage ?? null;

  if (
    hasFullAuthorization(consultations) ||
    status?.self_check != null ||
    (stage !== null && LATE_CHAIN_STAGES.includes(stage))
  ) {
    return "全編確認";
  }

  const sampleList = samples?.samples ?? [];
  if (
    hasAdoptedPolicy(consultations) ||
    sampleList.some(isSamplePublished) ||
    sampleList.length > 0 ||
    status?.preview_first_arrived_at != null ||
    (stage !== null && SAMPLE_CHAIN_STAGES.includes(stage))
  ) {
    return "試し動画";
  }

  if (
    consultationCount(consultations) > 0 ||
    (stage !== null && DIRECTION_CHAIN_STAGES.includes(stage))
  ) {
    return "方向";
  }

  return "素材";
}
