import type { EpisodeStatus } from "@/lib/episode-api";

/**
 * 工程2P §3.4の4段階モデル: stage名→ operators向け段階名のUI側の静的写像
 * （表）。新基盤ではなく既存phase語彙の平易な言い換えであり、ここで捏造する
 * 進捗はない（未計測は未計測と表示する）。publish の「完了」は公開段階の
 * 意味であり、PREVIEW_READY（試し編集完了）とは別物。
 */
export type StageGroup = {
  label: string;
  stages: string[];
};

export const STAGE_GROUPS: StageGroup[] = [
  { label: "素材の受付と確認", stages: ["intake", "ingest", "normalize", "analyze"] },
  { label: "方針の準備", stages: ["selection", "plan"] },
  { label: "試し編集", stages: ["compile", "preview"] },
  { label: "仕上げ", stages: ["resolve_build", "qc", "render"] },
  { label: "完了", stages: ["publish"] },
];

export function stageGroupOf(stageName: string): string | null {
  const group = STAGE_GROUPS.find((candidate) => candidate.stages.includes(stageName));
  return group?.label ?? null;
}

export type GroupProgress = "done" | "current" | "failed" | "untouched";

export const GROUP_PROGRESS_LABEL: Record<GroupProgress, string> = {
  done: "完了済み",
  current: "現在",
  failed: "止まっています",
  untouched: "未着手",
};

/** Group progress from the stage table's own status values — no invented
 *  state: failed beats running beats succeeded; no rows = 未着手. */
export function groupProgress(status: EpisodeStatus, group: StageGroup): GroupProgress {
  const rows = status.stage_runs.filter((run) => group.stages.includes(run.stage_name));
  if (rows.length === 0) return "untouched";
  if (rows.some((run) => run.status === "failed_blocked")) return "failed";
  if (rows.some((run) => run.status === "running")) return "current";
  if (rows.some((run) => run.status === "succeeded")) return "done";
  return "untouched";
}

/** 経過時間の mm:ss 表示（1時間以上は分で継続、捏造の上限なし）。 */
export function formatElapsed(ms: number): string {
  const totalSeconds = Math.floor(Math.max(0, ms) / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

/** 改訂条件1: PREVIEW_READY は「試し編集完了」——全体の完了ではない。
 *  他の job status は生の値のまま（対応する実測語彙がないため作らない）。 */
export function jobStatusSuffix(jobStatus: string): string | null {
  return jobStatus === "PREVIEW_READY" ? "試し編集完了（全体の完了ではありません）" : null;
}
