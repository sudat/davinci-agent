/**
 * 試し編集（preview）と今回の実行/対象版の対応（binding）の純粋導出。
 *
 * Honest-vocabulary discipline（不明は不明・推測しない）:
 * - 比較は文字列一致のみ。版の新旧順序は推測しない。
 * - 欠けた欄（null）を補完して判断に使わない — 材料が1つでも欠ければ unknown。
 * - probe失敗は not_generated に偽装しない — probe_failed として区別する。
 */
import type { EpisodePreviewProbe, EpisodeStatus } from "@/lib/episode-api";

/** 1回のprobe観測。成功（available true/false の両方を含む）か失敗か。
 *  失敗（network断・404以外の想定外HTTP status）は「確認できません」と
 *  表示するため、not_generated とは区別して保持する。 */
export type PreviewProbeObservation =
  | { readonly kind: "probed"; readonly probe: EpisodePreviewProbe }
  | { readonly kind: "failed" };

/** stale の内訳（表示文に使う事実のみ）。版違いか、版は同じで実行違いか。 */
export type PreviewStaleReason =
  | {
      readonly case: "version_changed";
      readonly probeVersion: string;
      readonly currentVersion: string;
    }
  | { readonly case: "run_only" };

export type PreviewBinding =
  | { readonly kind: "checking" }
  | { readonly kind: "probe_failed" }
  | { readonly kind: "not_generated" }
  | { readonly kind: "unknown" }
  | { readonly kind: "current"; readonly targetVersion: string }
  | { readonly kind: "stale"; readonly reason: PreviewStaleReason };

/** 導出は固定順。1. probe未取得 or status未取得 → checking。2. probe失敗 →
 *  probe_failed。3. available=false → not_generated。4. binding材料
 *  （run_id / target_version / content_hash / current_run /
 *  current_target_version）が1つでも欠け → unknown。5. run一致 かつ
 *  対象版一致 → current。6. それ以外 → stale。 */
export function derivePreviewBinding(
  observation: PreviewProbeObservation | null,
  status: EpisodeStatus | null,
): PreviewBinding {
  if (observation === null || status === null) return { kind: "checking" };
  if (observation.kind === "failed") return { kind: "probe_failed" };
  if (!observation.probe.available) return { kind: "not_generated" };
  const probe = observation.probe;
  const currentRun = status.current_run ?? null;
  const currentVersion = status.current_target_version ?? null;
  if (
    probe.run_id === null ||
    probe.target_version === null ||
    probe.content_hash === null ||
    currentRun === null ||
    currentVersion === null
  ) {
    return { kind: "unknown" };
  }
  if (probe.run_id === currentRun && probe.target_version === currentVersion) {
    return { kind: "current", targetVersion: probe.target_version };
  }
  if (probe.target_version !== currentVersion) {
    return {
      kind: "stale",
      reason: {
        case: "version_changed",
        probeVersion: probe.target_version,
        currentVersion,
      },
    };
  }
  return { kind: "stale", reason: { case: "run_only" } };
}

/** 完了判定への三値写像: current のときだけ true。stale / not_generated は
 *  「今回の成果ではない」ので false。checking / unknown / probe_failed は
 *  判定材料がなく null — 完了 claim を出さない。 */
export function previewAllowsCompletion(binding: PreviewBinding): boolean | null {
  switch (binding.kind) {
    case "current":
      return true;
    case "stale":
    case "not_generated":
      return false;
    case "checking":
    case "unknown":
    case "probe_failed":
      return null;
  }
}

/** binding行の表示文（プレビューカード直下の1行）。欠け欄の補完は起きない:
 *  checking / unknown / probe_failed は事実を名乗るのみ。 */
export function previewBindingLine(binding: PreviewBinding): string {
  switch (binding.kind) {
    case "checking":
      return "今回の実行との対応を確認しています…";
    case "probe_failed":
      return "確認できません（試し編集の情報を取得できません）";
    case "not_generated":
      return "試し編集はまだ生成されていません";
    case "unknown":
      return "不明（対象となる実行を特定できません）";
    case "current":
      return `今回の実行の試し編集です（対象版 ${binding.targetVersion}）`;
    case "stale":
      return binding.reason.case === "version_changed"
        ? `前回の実行のものです（再生成待ち。表示中 ${binding.reason.probeVersion}／今回 ${binding.reason.currentVersion}）`
        : "前回の実行のものです（今回の実行分を再生成待ち）";
  }
}
