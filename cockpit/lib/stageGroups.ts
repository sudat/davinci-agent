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

/** 各工程が実際に行う作業の平易名（待機表示の両欄が共用する単一語彙）。
 *  導出元（捏造しないための根拠）: ingest=実素材の登録・分類・受入判定
 *  （real_episode.py ingest_real_episode）、normalize=CFR編集用素材への統一
 *  （real_chain.py _normalize の cfr30 mezzanine）、analyze=3種の実解析器
 *  （ASR・対話・映像、real_analyze.py run_real_analyzers）、selection=候補から
 *  使う場面の選定（real_director.py select_and_reconcile／episode_runner_
 *  rebuild.py stage_selection の方針下での監督再実行）、plan=構成の決定
 *  （採用版の確定）、compile=版の計画＋中間表現の実体化（stage_compile
 *  "Materialize the head version's plan + IR"）、preview=試し映像の再描画・
 *  再公開（stage_preview "Re-render the review-plane preview"）、
 *  resolve_build/qc/render=仕上げ（Resolve組み立て・仕上がり確認・本番書き出し、
 *  Data Plane の Resolve Adapter→Final Render→QC の順）、publish=公開。
 *  段階ラベル（STAGE_GROUPS）とは別物であり、そちらは変えない。 */
export const STAGE_WORK_NAMES: Record<string, string> = {
  ingest: "素材の登録",
  normalize: "形式の統一",
  analyze: "素材の分析",
  selection: "使う場面の選定",
  plan: "構成の決定",
  compile: "編集指示の具体化",
  preview: "試し映像の作成",
  resolve_build: "本番編集の組み立て",
  qc: "仕上がりの確認",
  render: "本番映像の書き出し",
  publish: "公開",
};

export function stageWorkName(stageName: string): string {
  return STAGE_WORK_NAMES[stageName] ?? stageName;
}

export type GroupProgress = "done" | "current" | "failed" | "untouched";

export const GROUP_PROGRESS_LABEL: Record<GroupProgress, string> = {
  done: "完了済み",
  current: "現在",
  failed: "止まっています",
  untouched: "未着手",
};

/** The run whose rows are CURRENT activity (codex P1-2: 現行runと履歴の分離):
 * a pending reservation's unspawned run (null → nothing is active), else
 * the rebuild current_run, else the run of the chronologically-last row
 * (the initial chain). A run's EXISTENCE never proves activity — only its
 * running rows do. */
export function activeRunId(status: EpisodeStatus): string | null {
  const pending = status.pending_rebuild;
  if (pending !== null && pending !== undefined) return pending.run_id ?? null;
  if (typeof status.current_run === "string" && status.current_run !== "") {
    return status.current_run;
  }
  return status.stage_runs.at(-1)?.run_id ?? null;
}

type StageRunRow = EpisodeStatus["stage_runs"][number];

/** Per stage, the LATEST truthful row: the active run's row overrides
 * history; a historical stage keeps its own last row (rows append
 * chronologically). Old-run failures/residue can never override what the
 * current run actually did, and vice versa. */
export function perStageLatestRows(status: EpisodeStatus): Map<string, StageRunRow> {
  const active = activeRunId(status);
  const truth = new Map<string, StageRunRow>();
  for (const run of status.stage_runs) {
    if (active !== null && run.run_id !== active) continue;
    truth.set(run.stage_name, run);
  }
  for (const run of status.stage_runs) {
    if (truth.has(run.stage_name)) continue;
    if (active !== null && run.run_id === active) continue;
    truth.set(run.stage_name, run);
  }
  return truth;
}

/** codex P1（2026-09-08 14:32Z）: 現在の作業の導出源は現行runの「実行中」行
 * のみ。job.current_stage はrebuild中も更新されず「到達済み段階」を指し続け、
 * 実行中工程がそれを追い越すことがある。工程名・開始時刻・経過をこの行たち
 * からだけ出し、両者を同一視しない。 */
export function runningStageRows(status: EpisodeStatus): StageRunRow[] {
  const active = activeRunId(status);
  return active === null
    ? []
    : status.stage_runs.filter((row) => row.run_id === active && row.status === "running");
}

/** 停止/終端時の時計の意味を固定する源: 現行runの行のうち first_started_at
 * が実在する最後の行（行は時系列追記）。停止後の表示は成長する経過ではなく
 * 実測の開始時刻そのもの（最後の実行開始）とする。現行runを特定できない
 * （active null: 未起動の予約のみ等）ときは行を選ばない — 旧runの開始時刻を
 * 今回の実行開始として表示してはならない。呼び出し側は明示の不明表示にする。 */
export function lastStartedRowOfActiveRun(status: EpisodeStatus): StageRunRow | null {
  const active = activeRunId(status);
  if (active === null) return null;
  let last: StageRunRow | null = null;
  for (const row of status.stage_runs) {
    if (row.run_id !== active) continue;
    if (!Number.isNaN(Date.parse(row.first_started_at ?? ""))) last = row;
  }
  return last;
}

/** Group progress from each stage's LATEST truthful row — no invented
 * state: current-run failure beats active running beats succeeded; a
 * dead run's residue never marks the group active or failed. */
export function groupProgress(status: EpisodeStatus, group: StageGroup): GroupProgress {
  const active = activeRunId(status);
  let anySucceeded = false;
  let anyActiveRunning = false;
  for (const [stage, row] of perStageLatestRows(status)) {
    if (!group.stages.includes(stage)) continue;
    if (row.status === "failed_blocked") return "failed";
    if (row.status === "succeeded") anySucceeded = true;
    if (row.status === "running" && row.run_id === active) anyActiveRunning = true;
  }
  if (anyActiveRunning) return "current";
  if (anySucceeded) return "done";
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

/** UX 2.5 slice-1: 方針相談（編集前の方向性相談）が意味を持つのは
 *  plan確定（PLAN_COMMITTED）まで = 上位2グループ（素材の受付と確認・
 *  方針の準備）の段階のみ。可否判定もこの表からだけ導く（新規の段階
 *  語彙を作らない）。 */
const CONSULTATION_STAGE_GROUP_LABELS = ["素材の受付と確認", "方針の準備"];

export function isConsultationStage(stageName: string): boolean {
  return STAGE_GROUPS.filter((group) =>
    CONSULTATION_STAGE_GROUP_LABELS.includes(group.label),
  ).some((group) => group.stages.includes(stageName));
}
