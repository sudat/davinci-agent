"use client";

import { useRef, useState } from "react";
import {
  apiFailure,
  postReviewChat,
  type CheckedMaterials,
  type EpisodeStatus,
  type InvestigationState,
  type OutputId,
  type ProposalKind,
  type ReviewCommandDraft,
  type ReviewRevertResult,
} from "@/lib/api";
import ErrorNotice from "@/components/ErrorNotice";
import RebuildReadout from "@/components/RebuildReadout";
import ReviewDraftCard from "@/components/ReviewDraftCard";
import ReviewResponseNotice from "@/components/ReviewResponseNotice";
import ReviewRevertButton from "@/components/ReviewRevertButton";
import { rebuildPhaseText } from "@/components/useRebuildPhase";
import { useReviewApply } from "@/components/useReviewApply";
import { useNow } from "@/components/useNow";

type ReviewChatPanelProps = {
  episodeId: string;
  getAtSeconds: () => number | null;
  status: EpisodeStatus | null;
  /** Preview probe (2xx) result from the polling view — 完了 needs it. */
  previewOk?: boolean | null;
  fetchImpl?: typeof fetch;
  /** 工程5: the output chain this panel renders (default landscape).
   *  Vertical apply/rebuild/revert requests carry the output dimension;
   *  landscape requests stay byte-identical. */
  outputId?: OutputId;
};

type ChoiceReaction = "choice-a" | "choice-b";

const isChoice = (reaction: unknown): reaction is ChoiceReaction =>
  reaction === "choice-a" || reaction === "choice-b";

const LONG_BUSY_MS = 30_000;
const VERY_LONG_BUSY_MS = 120_000;

/**
 * Review chat (PRD 13.3): natural-language correction -> structured
 * command preview (echoed drafts, shown verbatim with parsed
 * kind/target/delta — SEVERAL when one message named several corrections)
 * -> apply -> lineage-scoped partial rebuild, executed by the detached
 * runner. Ambiguous drafts stay unappliable until rephrased (safety rule).
 *
 * 工程2: a message may also REACT to the previewed set. 「両方違う」 is sent
 * as a plain chat message — the response IS the re-investigated linked set
 * and replaces the drafts. A choice (choice-a/b) records the FACT only and
 * carries no drafts: the previous preview stays on screen and the per-draft
 * 採用 apply becomes the adoption (the backend refuses choices against
 * non-alternatives sets; the typed error surfaces in the error notice).
 *
 * 工程2 rework: the response's proposal_kind routes multi-draft adoption —
 * a command bundle gets the apply-ALL button (its fixes apply together);
 * alternatives get per-draft 採用 + 両方違う. Old payloads (field absent)
 * are bundles.
 *
 * 工程2P: in-flight actions show 送信中…/適用中…/復帰中… (never a fake
 * instant), and a send still waiting after 30s says 長くかかっています,
 * after 120s additionally suggesting a resend (D表: 同期LLM 30秒 /
 * frames付きcodex 120秒のtimeout根拠). There is NO stop button — safe
 * cancellation is not implemented (工程2P範囲外).
 */
export default function ReviewChatPanel({
  episodeId,
  getAtSeconds,
  status,
  previewOk = null,
  fetchImpl,
  outputId = "landscape",
}: ReviewChatPanelProps) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [revertBusy, setRevertBusy] = useState(false);
  const [error, setError] = useState<{ code: string; detail: string } | null>(null);
  const [sentAt, setSentAt] = useState<number | null>(null);
  const [chatSequence, setChatSequence] = useState<number | null>(null);
  const [investigationState, setInvestigationState] = useState<InvestigationState | undefined>(
    undefined,
  );
  const [choiceReaction, setChoiceReaction] = useState<ChoiceReaction | null>(null);
  const [proposalKind, setProposalKind] = useState<ProposalKind | undefined>(undefined);
  const [checkedMaterials, setCheckedMaterials] = useState<CheckedMaterials | undefined>(
    undefined,
  );
  const [drafts, setDrafts] = useState<ReviewCommandDraft[] | null>(null);
  const [revertNote, setRevertNote] = useState<string | null>(null);
  const applyFlow = useReviewApply({
    episodeId,
    status,
    previewOk,
    fetchImpl,
    onError: setError,
    outputId,
  });
  // One flight flag across send / apply / revert keeps every panel action
  // single-flight (revert reports its own busy via onBusyChange).
  const busyAll = busy || applyFlow.applyBusy || revertBusy;
  const phaseText = rebuildPhaseText(applyFlow.rebuildPhase);

  // Send-wait clock: wall time since THIS send started (D表 thresholds).
  const busyStartRef = useRef<number | null>(null);
  if (busy && busyStartRef.current === null) busyStartRef.current = Date.now();
  if (!busy) busyStartRef.current = null;
  const now = useNow(busy);
  const busyMs =
    busy && busyStartRef.current !== null ? Math.max(0, now - busyStartRef.current) : 0;
  const longBusy = busyMs >= LONG_BUSY_MS;
  const veryLongBusy = busyMs >= VERY_LONG_BUSY_MS;

  const send = async (messageText: string) => {
    const trimmed = messageText.trim();
    if (trimmed === "") return;
    setBusy(true);
    setError(null);
    applyFlow.reset();
    setRevertNote(null);
    setChoiceReaction(null);
    setProposalKind(undefined);
    setCheckedMaterials(undefined);
    setInvestigationState(undefined);
    const atSeconds = getAtSeconds();
    setSentAt(atSeconds);
    try {
      const result = await postReviewChat(
        episodeId,
        { text: trimmed, at_seconds: atSeconds },
        fetchImpl,
      );
      // Absent field = bundle on old payloads (backend `effective_proposal_kind`);
      // a choice ack carries it too — it names the kind of the still-previewed set.
      setProposalKind(result.proposal_kind ?? "command-bundle");
      if (isChoice(result.reaction)) {
        // Choice ack: NO new drafts exist (the backend records the FACT
        // only). The previewed set stays unconsumed and on screen — its
        // per-draft apply is the adoption — and chatSequence still names
        // THAT set (the ack's own sequence is a chat entry, not a set).
        setChoiceReaction(result.reaction);
        return;
      }
      setChatSequence(result.sequence);
      setInvestigationState(result.investigation_state);
      setCheckedMaterials(result.checked_materials);
      setDrafts(result.drafts ?? [result.draft]);
    } catch (cause) {
      setError(apiFailure(cause));
    } finally {
      setBusy(false);
    }
  };

  /** One apply = the echoed drafts (all, or ONE for 工程2 adoption) + the
   *  lineage-scoped rebuild request. `sequence` names the SERVER-SAVED
   *  proposal set (reference, not content authority). */
  const applySelected = (selected: ReviewCommandDraft[]) => {
    if (selected.length === 0) return;
    setError(null);
    setRevertNote(null);
    void applyFlow.applyDrafts({
      text: selected[0].text,
      at_seconds: sentAt,
      drafts: selected,
      ...(chatSequence !== null ? { sequence: chatSequence } : {}),
    });
  };

  // Investigated (feelings-route) drafts stay flagged until the operator
  // applies them — the explicit apply of the echoed draft IS the backend's
  // confirmation (apply_command accepts investigated drafts), so they must
  // not block the apply button like truly ambiguous drafts do.
  const adoptable = (draft: ReviewCommandDraft) =>
    !draft.needs_confirmation || draft.investigated === true;
  const anyAmbiguous = drafts?.some(
    (draft) => draft.needs_confirmation && draft.investigated !== true,
  ) ?? false;
  const multi = (drafts?.length ?? 0) > 1;
  // 工程2 rework action matrix (single draft = the plain apply in every kind):
  // bundle multi → apply-ALL (fixes apply together, never one-by-one);
  // alternatives multi → per-draft adoption + 両方違う (mutually exclusive).
  const multiAlternatives = multi && proposalKind === "alternatives";

  /** 工程1 undo handling: the revert always restores the version as a NEW
   *  version; only the rebuild LAUNCH can fail afterwards (P1b honesty).
   *  The note must say exactly that — and the revert button doubles as the
   *  retry affordance (a resend relaunches the same step, idempotently). */
  const handleReverted = (reverted: ReviewRevertResult) => {
    setRevertNote(
      reverted.rebuild.scheduled === false
        ? `版は戻っています（${reverted.new_version}）。再構築の起動に失敗しました`
        : `版を戻しました（${reverted.new_version}）`,
    );
    setDrafts(null);
    setChatSequence(null);
    applyFlow.adoptRevertReadout(reverted);
  };

  const handleRevertBusy = (next: boolean) => {
    setRevertBusy(next);
    if (next) setError(null);
  };

  // The undo entry stays reachable at all times — consecutive reverts can
  // keep walking back, and a failed launch can be retried by resending.
  const revertButton = (
    <ReviewRevertButton
      episodeId={episodeId}
      disabled={busyAll}
      fetchImpl={fetchImpl}
      outputId={outputId}
      onBusyChange={handleRevertBusy}
      onReverted={handleReverted}
      onError={(failure) => setError(failure)}
    />
  );

  return (
    <section className="card" data-testid="review-chat-panel">
      <h2 className="card-title">修正チャット</h2>
      <p className="page-subtitle" style={{ marginBottom: "var(--space-3)" }}>
        自然言語で修正を伝えると、構造化コマンドの解釈プレビューを返します。確認して適用すると、影響stageのみの部分rebuildが実行されます。
      </p>
      {error !== null ? <ErrorNotice code={error.code} detail={error.detail} /> : null}
      <label className="field" htmlFor="review-chat-input">
        修正指示（自然言語）
        <textarea
          id="review-chat-input"
          rows={2}
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="例: この後2秒残して"
          data-testid="review-chat-input"
        />
      </label>
      <div className="actions">
        <button
          type="button"
          className="btn-primary"
          onClick={() => void send(text)}
          disabled={busyAll || text.trim() === ""}
          data-testid="review-chat-send"
        >
          {busy ? "送信中…" : "送信"}
        </button>
      </div>
      {longBusy ? (
        <p className="field-hint" data-testid="chat-long-warn" aria-live="polite">
          長くかかっています
          {veryLongBusy ? "。もう一度送信（同じ内容の再送）も検討できます" : ""}
        </p>
      ) : null}
      <ReviewResponseNotice reaction={choiceReaction} checkedMaterials={checkedMaterials} />
      {drafts !== null && drafts.length > 0 ? (
        drafts.map((draft, index) => (
          <ReviewDraftCard
            key={draft.command_id}
            draft={draft}
            index={index}
            multi={multi}
            investigationState={investigationState}
            adoptable={adoptable(draft)}
            busy={busyAll}
            onAdopt={multiAlternatives ? () => applySelected([draft]) : undefined}
          />
        ))
      ) : null}
      {drafts !== null && drafts.length > 0 ? (
        <div className="actions">
          {multiAlternatives ? (
            <button
              type="button"
              className="btn-small"
              onClick={() => void send("両方違う")}
              disabled={busyAll}
              data-testid="review-both-different"
            >
              {busyAll ? "送信中…" : "両方違う"}
            </button>
          ) : (
            <button
              type="button"
              className="btn-primary"
              onClick={() => applySelected(drafts)}
              disabled={busyAll || anyAmbiguous}
              data-testid={multi ? "review-apply-all-button" : "review-apply-button"}
            >
              {busyAll
                ? "適用中…"
                : multi
                  ? "この修正をすべて適用"
                  : "この修正を適用"}
            </button>
          )}
        </div>
      ) : null}
      <div className="actions">{revertButton}</div>
      <RebuildReadout
        revertNote={revertNote}
        rebuildResult={applyFlow.rebuildResult}
        applyResult={applyFlow.applyResult}
        phaseText={phaseText}
      />
    </section>
  );
}
