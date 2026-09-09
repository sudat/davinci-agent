"use client";

import { useState } from "react";
import {
  apiFailure,
  applyReviewCommand,
  postRebuild,
  revertRebuildReadout,
  type EpisodeStatus,
  type OutputId,
  type RebuildResult,
  type ReviewApplyInput,
  type ReviewApplyResult,
  type ReviewRevertResult,
} from "@/lib/api";
import { useRebuildPhase, rebuildBaselineOf, type RebuildRequestBaseline } from "@/components/useRebuildPhase";

type UseReviewApplyOptions = {
  episodeId: string;
  status: EpisodeStatus | null;
  /** Preview probe (2-byte Range GET) result from the polling view:
   *  true only after THIS run's 成果 is probe-confirmed (工程2P). */
  previewOk?: boolean | null;
  fetchImpl?: typeof fetch;
  onError: (failure: { code: string; detail: string }) => void;
  /** 工程5: the output chain this panel renders. Vertical requests carry
   *  output_id; landscape omits it (backend default, byte-identical). */
  outputId?: OutputId;
};

/**
 * The apply -> rebuild mutation (V44-1 / 工程2): ONE echoed-drafts apply
 * request (the whole saved set, or ONE draft of it — the per-draft
 * adoption echo, outcome `chosen`) followed by the lineage-scoped
 * rebuild request. Owns apply/rebuild state; the revert flow feeds the
 * same state through `adoptRevertReadout`, so the panel keeps ONE phase
 * derivation for both flows. Phase is run-scoped (工程2P) — there is no
 * baseline capture anymore.
 */
export function useReviewApply({
  episodeId,
  status,
  previewOk = null,
  fetchImpl,
  onError,
  outputId = "landscape",
}: UseReviewApplyOptions) {
  const [applyBusy, setApplyBusy] = useState(false);
  const [applyResult, setApplyResult] = useState<ReviewApplyResult | null>(null);
  const [rebuildResult, setRebuildResult] = useState<RebuildResult | null>(null);
  /** Accept-time server generation (pre-request poll snapshot): 完了 derives
   *  only from a NEWER server state, never from a stale pre-rebuild poll. */
  const [rebuildBaseline, setRebuildBaseline] = useState<RebuildRequestBaseline | null>(null);
  const { phase: rebuildPhase } = useRebuildPhase(
    rebuildResult,
    status,
    previewOk,
    rebuildBaseline,
  );

  const applyDrafts = async (input: ReviewApplyInput) => {
    setApplyBusy(true);
    // Snapshot BEFORE the POSTs: this closure's status is the pre-request
    // poll (the 2s poll may still serve it after accept — that stale
    // repeat must never complete THIS request).
    const baseline = rebuildBaselineOf(status);
    const vertical = outputId === "vertical";
    try {
      const applied = await applyReviewCommand(
        episodeId,
        vertical ? { ...input, output_id: outputId } : input,
        fetchImpl,
      );
      setApplyResult(applied);
      const commandIds =
        applied.applied_commands?.map((command) => command.command_id) ?? [
          applied.applied.command_id,
        ];
      const rebuilt = await postRebuild(
        episodeId,
        {
          applied_command: commandIds[0],
          ...(commandIds.length > 1 ? { applied_commands: commandIds } : {}),
          ...(vertical ? { output_id: outputId } : {}),
        },
        fetchImpl,
      );
      setRebuildBaseline(baseline);
      setRebuildResult(rebuilt);
    } catch (cause) {
      onError(apiFailure(cause));
    } finally {
      setApplyBusy(false);
    }
  };

  /** 工程1 undo handling: the revert restored the version as a NEW version
   *  (only the rebuild LAUNCH can fail — P1b honesty); its readout rides
   *  the same phase machinery as the apply flow. */
  const adoptRevertReadout = (reverted: ReviewRevertResult) => {
    setApplyResult(null);
    setRebuildBaseline(rebuildBaselineOf(status));
    setRebuildResult(revertRebuildReadout(reverted));
  };

  /** A fresh preview replaces the previous apply/rebuild readout. */
  const reset = () => {
    setApplyResult(null);
    setRebuildBaseline(null);
    setRebuildResult(null);
  };

  return {
    applyBusy,
    applyResult,
    rebuildResult,
    rebuildPhase,
    applyDrafts,
    adoptRevertReadout,
    reset,
  };
}
