"use client";

import { useState } from "react";
import { apiFailure, revertReviewPlan, type OutputId, type ReviewRevertResult } from "@/lib/api";

type ReviewRevertButtonProps = {
  episodeId: string;
  /** Disabled while another panel action (send/apply) is in flight. */
  disabled?: boolean;
  fetchImpl?: typeof fetch;
  /** 工程5: the output chain this revert restores (default landscape). */
  outputId?: OutputId;
  /** Reported at revert-attempt start/end so the panel can disable its
   *  other actions for the duration (single-flight discipline). */
  onBusyChange?: (busy: boolean) => void;
  onReverted: (result: ReviewRevertResult) => void;
  onError: (failure: { code: string; detail: string }) => void;
};

/**
 * 工程1 undo: one click = one POST /review-chat/revert. The revert itself
 * is idempotent on the backend (an un-launched revert resend relaunches
 * the SAME step), so this button doubles as the retry affordance when the
 * previous revert restored the version but failed to launch the rebuild.
 */
export default function ReviewRevertButton({
  episodeId,
  disabled = false,
  fetchImpl,
  outputId = "landscape",
  onBusyChange,
  onReverted,
  onError,
}: ReviewRevertButtonProps) {
  const [busy, setBusy] = useState(false);

  const click = async () => {
    setBusy(true);
    onBusyChange?.(true);
    try {
      onReverted(await revertReviewPlan(episodeId, fetchImpl, outputId));
    } catch (cause) {
      onError(apiFailure(cause));
    } finally {
      setBusy(false);
      onBusyChange?.(false);
    }
  };

  return (
    <button
      type="button"
      className="btn-small"
      onClick={() => void click()}
      disabled={disabled || busy}
      data-testid="review-revert-button"
    >
      {busy ? "復帰中…" : "前の状態に戻す"}
    </button>
  );
}
