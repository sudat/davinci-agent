"use client";

import { useMemo } from "react";
import type { EpisodeStatus, RebuildResult } from "@/lib/api";

export type RebuildPhase =
  | "done"
  | "running"
  | "scheduled"
  | "recorded"
  | "awaiting_confirmation"
  | "failed";

export const REBUILD_PHASE_LABEL: Record<Exclude<RebuildPhase, "recorded">, string> = {
  scheduled: "再build予約済み（起動待ち）",
  running: "再build実行中",
  awaiting_confirmation: "試し編集完了・確認してください",
  done: "再build完了（試し編集の更新を確認済み）",
  failed: "再buildが止まっています（確認が必要です）",
};

/**
 * The rendered phase line: label + scheduling hint. `null` and `recorded`
 * render nothing here — call sites show their own honest fallback text
 * (e.g. 「再build未実行（コマンドは記録済み）」).
 */
export function rebuildPhaseText(phase: RebuildPhase | null): string | null {
  if (phase === null || phase === "recorded") return null;
  return REBUILD_PHASE_LABEL[phase];
}

function previewArrivedThisRun(
  status: EpisodeStatus,
  runRows: EpisodeStatus["stage_runs"],
): boolean {
  if (status.preview_first_arrived_at !== undefined) return true;
  return runRows.some(
    (row) =>
      row.stage_name === "preview" &&
      typeof row.first_output_arrived_at === "string" &&
      row.first_output_arrived_at !== "",
  );
}

/**
 * 工程2P run-scoped derivation (step2p-design.md §B, codex条件1/4): the
 * phase comes ONLY from the rebuild chain (an unspawned latest reservation
 * = 予約済み, even when old-run rows are still around — U30) and from
 * stage rows whose run_id equals the CURRENT run. 成果 = THIS run's
 * preview first-output arrival; 完了 additionally needs the preview probe
 * (2xx). The old baseline-count comparison is REMOVED — legacy payloads
 * without run scoping can never claim 完了 from unscoped success rows.
 *
 * Stale-poll honesty (V44-1 live lane): POST /rebuild resolves instantly
 * while the 2s status poll still serves the PRE-rebuild server state
 * (old current_run + old preview arrival + old bound probe). Combining
 * that stale server state with the fresh client rebuildResult used to
 * derive done for ~2s. 完了 now requires server-recorded evidence of
 * THIS rebuild run — a newer current_run or a newer rebuild-requests
 * chain entry than the accept-time baseline (useReviewApply snapshots it
 * from the pre-request poll). The client result alone drives at most the
 * transient accepted state (予約済み) or in-progress, never completion.
 */
/** U30 hydration (codex P1-1): a reload starts with rebuildResult=null, but
 * the SERVER chain is the authority — a live reservation or a spawned run
 * must still restore the phase line. Terminal-only history (no reservation,
 * no current run) stays hydrated only while the backend keeps naming the
 * run; the legacy no-scope fallback below is never reached from hydration. */
function hasServerRebuildChain(status: EpisodeStatus | null): boolean {
  if (status === null) return false;
  if (status.pending_rebuild !== null && status.pending_rebuild !== undefined) {
    return true;
  }
  return typeof status.current_run === "string" && status.current_run !== "";
}

/** Accept-time server generation for THIS rebuild request: what the
 *  polled status named when POST /rebuild was accepted. A stale pre-rebuild
 *  poll repeats exactly this generation; only a NEWER server state (a
 *  different current run, or a newer rebuild-requests chain entry) proves
 *  the server has recorded THIS request. null fields = none/unknown. */
export type RebuildRequestBaseline = {
  readonly currentRun: string | null;
  readonly latestSequence: number | null;
};

/** Snapshot the accept-time baseline from the pre-request poll. */
export function rebuildBaselineOf(status: EpisodeStatus | null): RebuildRequestBaseline {
  const sequences = (status?.rebuild_requests ?? []).map((entry) => entry.sequence);
  return {
    currentRun: status?.current_run ?? null,
    latestSequence: sequences.length > 0 ? Math.max(...sequences) : null,
  };
}

function latestChainSequence(status: EpisodeStatus): number | null {
  const sequences = (status.rebuild_requests ?? []).map((entry) => entry.sequence);
  return sequences.length > 0 ? Math.max(...sequences) : null;
}

/**
 * Server-recorded evidence that THIS rebuild request moved. With a
 * baseline: the server names a different current run, or its
 * rebuild-requests chain grew past the accept moment. Without a baseline
 * (direct/legacy callers): a spawned chain entry must name the current
 * run — bare rows and the global preview arrival alone never prove THIS
 * rebuild, so an old run's succeeded preview can never complete a new
 * request (no cross-run mixing).
 */
function serverProvesThisRebuild(
  status: EpisodeStatus,
  runId: string,
  baseline: RebuildRequestBaseline | null,
): boolean {
  if (baseline !== null) {
    if (runId !== baseline.currentRun) return true;
    const latest = latestChainSequence(status);
    if (latest !== null && (baseline.latestSequence === null || latest > baseline.latestSequence)) {
      return true;
    }
    return false;
  }
  return (status.rebuild_requests ?? []).some(
    (entry) => entry.spawned && entry.run_id === runId,
  );
}

export function deriveRebuildPhase(
  rebuildResult: RebuildResult | null,
  status: EpisodeStatus | null,
  previewOk: boolean | null,
  baseline?: RebuildRequestBaseline | null,
): RebuildPhase | null {
  if (rebuildResult === null && !hasServerRebuildChain(status)) return null;
  if (rebuildResult !== null && !rebuildResult.scheduled) return "recorded";
  if (status === null) return "scheduled";
  if (status.pending_rebuild !== null && status.pending_rebuild !== undefined) {
    return "scheduled";
  }
  const runId = status.current_run ?? null;
  if (runId === null) {
    if (status.stage_runs.some((row) => row.status === "running")) return "running";
    return "scheduled";
  }
  const runRows = status.stage_runs.filter((row) => row.run_id === runId);
  if (runRows.some((row) => row.status === "running")) return "running";
  if (runRows.some((row) => row.status === "failed_blocked")) return "failed";
  if (rebuildResult !== null && !serverProvesThisRebuild(status, runId, baseline ?? null)) {
    // 受付済みだがpolled statusがまだTHIS runを示していないstale poll:
    // 進行中どまり — 完了も試し編集の到達claimも出さない。run無し
    // legacy行の実行中だけは活動として実行中のまま。
    if (
      status.stage_runs.some((row) => row.status === "running" && row.run_id === undefined)
    ) {
      return "running";
    }
    return "scheduled";
  }
  if (previewArrivedThisRun(status, runRows)) {
    return previewOk === true ? "done" : "awaiting_confirmation";
  }
  return "running";
}

/**
 * Derive the rebuild progress phase from the rebuild readout plus polled
 * job status and the preview probe result. `recorded` means the command/
 * rebuild request is recorded but not runner-scheduled.
 */
export function useRebuildPhase(
  rebuildResult: RebuildResult | null,
  status: EpisodeStatus | null,
  previewOk: boolean | null,
  baseline?: RebuildRequestBaseline | null,
): { phase: RebuildPhase | null } {
  const phase = useMemo<RebuildPhase | null>(
    () => deriveRebuildPhase(rebuildResult, status, previewOk, baseline ?? null),
    [rebuildResult, status, previewOk, baseline],
  );
  return { phase };
}
