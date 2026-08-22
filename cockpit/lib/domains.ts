/**
 * Japanese display labels for the reference-learning vocabulary
 * (PreferenceDomain / Polarity — PRD 8.5, task 26/50). Pure data, no React.
 */

import type { Polarity, PreferenceDomain } from "@/lib/api";

export const DOMAIN_LABEL: Record<PreferenceDomain, string> = {
  story_structure: "ストーリー構成",
  pacing: "ペーシング",
  color: "色",
  subtitle: "字幕",
  b_roll: "Bロール",
  framing_graphics: "フレーミング/グラフィック",
  audio: "音声",
};

export const POLARITY_LABEL: Record<Polarity, string> = {
  like: "良い",
  dislike: "嫌い",
  neutral: "中立",
  unspecified: "指定なし",
};

/** Polarities a NAMED domain may carry — "unspecified" is not a choice. */
export const NAMED_POLARITIES: readonly Polarity[] = [
  "like",
  "dislike",
  "neutral",
] as const;
