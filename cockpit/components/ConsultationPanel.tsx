"use client";

// allow: SIZE_OK — 573 pure LOC: one consultation-panel concern. The poll
// merge (absorbView, journal row keys), the judgment forms, the slice-2
// rebuild-state line, and the 工程4 optional-generation section (budget
// remainder, scope, permission/decline sends, refusal lines) are one
// interaction unit over the same polled view; splitting the merge from its
// render would separate the U44 reload-restore behavior from the state
// that produces it.
import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  apiFailure,
  CockpitApiError,
  getConsultation,
  isPolicyOutcomeEntry,
  postConsultationJudgment,
  postConsultationMessage,
  postConsultationPanelRetry,
  type Consultation,
  type ConsultationAdoptedPolicy,
  type ConsultationEntry,
  type ConsultationGenerationPanel,
  type ConsultationGenerationState,
  type ConsultationJudgmentInput,
  type ConsultationPayload,
  type ConsultationRebuild,
  type EpisodeStatus,
} from "@/lib/api";
import ConsultationBudgetReadout, {
  latestBudgetOf,
} from "@/components/ConsultationBudgetReadout";
import ConsultationEntryList from "@/components/ConsultationEntryList";
import ConsultationSampleSection, {
  type SampleFeedback,
} from "@/components/ConsultationSampleSection";
import StyleSaveButton from "@/components/StyleSaveButton";
import ErrorNotice from "@/components/ErrorNotice";
import { isConsultationStage } from "@/lib/stageGroups";
import type { StageHint } from "@/lib/stage-steps";
import { useNow } from "@/components/useNow";

const POLL_INTERVAL_MS = 2000;
const STALE_AFTER_MS = 15000;

type ConsultationPanelProps = {
  episodeId: string;
  status: EpisodeStatus | null;
  fetchImpl?: typeof fetch;
  /** 方向画面の左に置く撮影素材プレイヤー（EpisodeViewのPreviewPlayer）。 */
  footageSlot?: ReactNode;
  onStageHint?: (hint: StageHint) => void;
};

/** 採用した方針の2〜3行要約。構成・テンポ・字幕・音のうち実際に入って
 *  いる内容だけを抜く（空欄は出さない）。 */
export function adoptedSummaryLinesOf(
  adopted: ConsultationAdoptedPolicy | null | undefined,
): string[] {
  if (adopted === null || adopted === undefined) return [];
  return [adopted.tempo_policy, adopted.structure, adopted.subtitle_policy, adopted.audio_policy]
    .filter((line): line is string => typeof line === "string" && line.trim() !== "")
    .slice(0, 3);
}

function isFullAuthorizedEntry(entry: ConsultationEntry): boolean {
  if (isPolicyOutcomeEntry(entry)) return false;
  return entry.judgments.some((judgment) => judgment.decision === "full_authorized");
}

/** The CURRENT adoption is authorized — and only then. A new adoption
 *  after an authorization returns the consultation to its own sample
 *  stage (the old authorization authorizes the old policy, not the new
 *  one). Mirrors the backend no-archaeology rule: strip the trailing
 *  full_authorized run; the immediately preceding judgment must be
 *  adopt/revise. A delayed idempotent resend appends nothing, so journal
 *  order always reflects real causality. */
function isCurrentAdoptionAuthorized(consultations: ConsultationEntry[]): boolean {
  const decisions: string[] = [];
  for (const entry of consultations) {
    if (isPolicyOutcomeEntry(entry)) continue;
    for (const judgment of entry.judgments) decisions.push(judgment.decision);
  }
  let end = decisions.length;
  while (end > 0 && decisions[end - 1] === "full_authorized") end--;
  if (end === decisions.length || end === 0) return false;
  const preceding = decisions[end - 1];
  return preceding === "adopt" || preceding === "revise";
}

function isConsultationPayload(value: unknown): value is ConsultationPayload {
  if (typeof value !== "object" || value === null) return false;
  return Array.isArray((value as { consultations?: unknown }).consultations);
}

/** 工程3 save target: the channel pinned at episode start (poll-derived,
 *  U08 restart-safe). Absent on old data → null (honest no-button line). */
function appliedChannelOf(status: EpisodeStatus | null): string | null {
  if (status === null) return null;
  const applied: unknown = status.applied_style;
  if (typeof applied !== "object" || applied === null) return null;
  const channel: unknown = (applied as { channel?: unknown }).channel;
  return typeof channel === "string" && channel !== "" ? channel : null;
}

function entryKeyOf(entry: ConsultationEntry, index: number): string {  if (isPolicyOutcomeEntry(entry)) {
    const id = [entry.outcome_id, entry.judgment_id, entry.recorded_at].find(
      (value) => typeof value === "string" && value !== "",
    );
    return `policy-outcome:${id ?? `row-${index}`}`;
  }
  return entry.consultation_id;
}

/** Slice-2 rebuild line, derived ONLY from the polled view (`view.rebuild`)
 *  — never from client memory, so a reload restores the same display
 *  (U44). Absent/"none"/unknown renders as nothing: this line exists only
 *  while an adopted policy is being (or was) reflected. */
function rebuildLineOf(rebuild: ConsultationRebuild | null | undefined): string | null {
  if (rebuild === null || rebuild === undefined) return null;
  const status: unknown = rebuild.status;
  if (status === "requested") {
    return "採用した方針を反映する再編集を準備しています";
  }
  if (status === "running") {
    return "再編集を実行しています";
  }
  if (status === "succeeded") {
    const version: unknown = rebuild.target_version;
    return `再編集が完了しました（対象版 ${typeof version === "string" && version !== "" ? version : "不明"}）——結果は下の反映結果行で確認してください`;
  }
  if (status === "failed") {
    const detail: unknown = rebuild.detail;
    return `再編集に失敗しました：${typeof detail === "string" && detail !== "" ? detail : "詳細は不明"}。相談を続けられます`;
  }
  return null;
}

const GENERATION_ERROR_CODES = [
  "generation-model-unverified",
  "generation-budget-exhausted",
  "generation-stale",
] as const;

function isGenerationErrorCode(code: string): boolean {
  return (GENERATION_ERROR_CODES as readonly string[]).includes(code);
}

function generationErrorLineOf(code: string): string {
  if (code === "generation-model-unverified") {
    return "画像の案を作るAIの確認が済んでいません。0生成で続行します（文章のみの相談は続けられます）";
  }
  if (code === "generation-budget-exhausted") {
    return "画像の案の上限に達しました。0生成で続行します（文章のみの相談は続けられます）";
  }
  if (code === "generation-stale") {
    return "表示中の内容が古いため、画像の案は作りませんでした。0生成で続行します（文章のみの相談は続けられます）";
  }
  return "画像の案は作りませんでした。0生成で続行します（文章のみの相談は続けられます）";
}

function generationStateOf(
  payload: ConsultationPayload | null,
): ConsultationGenerationState | null {
  if (payload === null) return null;
  const state: unknown = payload.generation_state;
  if (typeof state !== "object" || state === null) return null;
  return state as ConsultationGenerationState;
}

function refusedReasonOf(state: ConsultationGenerationState | null): string | null {
  if (state === null) return null;
  const reason: unknown = state.refused_reason;
  return typeof reason === "string" && reason !== "" ? reason : null;
}

/**
 * Pre-edit directional consultation (UX 2.5 slice-1): mounted by
 * EpisodeView, visible only while current_stage is in the plan-committed
 * stage groups (stageGroups table — no new stage vocabulary). Message in,
 * proposals out (short summary primary, details collapsed), per-proposal
 * judgment recorded. Polling mirrors 工程2P: fetchedAt on every success,
 * 15s 途切れ banner + focus/visibility requery; proposal arrival is
 * announced via aria-live. Budget is call-count managed (cost cannot be
 * measured directly) and never resets; consultation-budget-exhausted
 * degrades to text-only (honest notice, no retry spinner) and
 * consultation-llm-unavailable says proposals need the production model.
 */
export default function ConsultationPanel({
  episodeId,
  status,
  fetchImpl,
  footageSlot = null,
  onStageHint,
}: ConsultationPanelProps) {
  const [payload, setPayload] = useState<ConsultationPayload | null>(null);
  const [hasPublishedSample, setHasPublishedSample] = useState(false);
  const [error, setError] = useState<{ code: string; detail: string } | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [fetchedAt, setFetchedAt] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [announcement, setAnnouncement] = useState<string | null>(null);
  const [generationOpen, setGenerationOpen] = useState(false);
  const [panelsMax, setPanelsMax] = useState(1);
  const [generationError, setGenerationError] = useState<{
    code: string;
    detail: string;
  } | null>(null);
  const eligible = status !== null && isConsultationStage(status.current_stage);
  const now = useNow(eligible);
  const openedAtRef = useRef<number | null>(null);
  if (openedAtRef.current === null) openedAtRef.current = Date.now();
  const knownProposalIdsRef = useRef<Set<string> | null>(null);
  const requeryRef = useRef<(() => void) | null>(null);

  /** Merge a view (GET list, or the whole view a judgment POST returns)
   *  into state: journal rows upsert by key, per-view `policy_outcomes`
   *  riders merge into the journal as their own rows, and the episode-level
   *  policy/rebuild state takes the latest view row that carries them (the
   *  wire rides them on each view row). Proposal arrival is announced via
   *  aria-live only for genuinely NEW proposals. */
  const absorbView = useCallback((view: ConsultationPayload) => {
    setPayload((prev) => {
      const byKey = new Map<string, ConsultationEntry>();
      for (const entry of prev?.consultations ?? []) {
        byKey.set(entryKeyOf(entry, byKey.size), entry);
      }
      let nextPolicy: ConsultationPayload["policy"];
      let nextRebuild: ConsultationPayload["rebuild"];
      let nextGenerationState: ConsultationPayload["generation_state"];
      let carriesGenerationState = false;
      for (const entry of view.consultations) {
        byKey.set(entryKeyOf(entry, byKey.size), entry);
        if (!isPolicyOutcomeEntry(entry)) {
          for (const outcome of entry.policy_outcomes ?? []) {
            byKey.set(entryKeyOf(outcome, byKey.size), outcome);
          }
          if (entry.policy !== undefined && entry.policy !== null) {
            nextPolicy = entry.policy;
          }
          if (entry.rebuild !== undefined && entry.rebuild !== null) {
            nextRebuild = entry.rebuild;
          }
        }
      }
      if (view.generation_state !== undefined) {
        nextGenerationState = view.generation_state;
        carriesGenerationState = true;
      }
      const next: ConsultationPayload = { consultations: [...byKey.values()] };
      const policy = nextPolicy ?? prev?.policy;
      const rebuild = nextRebuild ?? prev?.rebuild;
      if (policy !== undefined) next.policy = policy;
      if (rebuild !== undefined) next.rebuild = rebuild;
      const generationState = carriesGenerationState
        ? nextGenerationState
        : prev?.generation_state;
      if (generationState !== undefined) next.generation_state = generationState;
      return next;
    });
    const known = knownProposalIdsRef.current;
    const ids = view.consultations.flatMap((entry) =>
      isPolicyOutcomeEntry(entry)
        ? []
        : entry.proposals.map((proposal) => proposal.proposal_id),
    );
    if (known === null) {
      knownProposalIdsRef.current = new Set(ids);
      return;
    }
    const fresh = ids.filter((id) => !known.has(id));
    if (fresh.length > 0) {
      for (const id of fresh) known.add(id);
      setAnnouncement(`新しい提案が届きました（${fresh.length}件）`);
    }
  }, []);

  const applyPayload = useCallback(
    (next: ConsultationPayload) => {
      absorbView(next);
    },
    [absorbView],
  );

  /** Upsert single message entries (the message POST returns one updated
   *  entry); episode-level policy/rebuild already on screen are kept. */
  const absorb = useCallback(
    (entries: Consultation[]) => {
      absorbView({ consultations: entries });
    },
    [absorbView],
  );

  useEffect(() => {
    if (!eligible) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const load = async () => {
      try {
        const next: unknown = await getConsultation(episodeId, fetchImpl);
        if (cancelled) return;
        if (!isConsultationPayload(next)) {
          setError({ code: "unexpected-response", detail: "相談の応答の形式が期待と違います" });
        } else {
          applyPayload(next);
          setError(null);
          setFetchedAt(Date.now());
        }
      } catch (cause) {
        if (cancelled) return;
        if (cause instanceof CockpitApiError && cause.status === 404) {
          setNotFound(true);
          return;
        }
        const failure = apiFailure(cause);
        // Poll connectivity failures stay off the generic notice — the
        // 15s 途切れ banner below is this panel's honest signal (the parent
        // view announces connectivity). The two typed degradation codes
        // surface wherever they arrive.
        if (
          failure.code === "consultation-budget-exhausted" ||
          failure.code === "consultation-llm-unavailable"
        ) {
          setError(failure);
        }
      }
      if (cancelled) return;
      timer = setTimeout(() => void load(), POLL_INTERVAL_MS);
    };

    const requery = () => {
      if (cancelled) return;
      if (timer !== undefined) clearTimeout(timer);
      void load();
    };
    requeryRef.current = requery;

    void load();
    const onVisibility = () => {
      if (document.visibilityState === "visible") requery();
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", requery);
    return () => {
      cancelled = true;
      requeryRef.current = null;
      if (timer !== undefined) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("focus", requery);
    };
  }, [episodeId, eligible, fetchImpl, applyPayload]);

  const stale =
    eligible &&
    !notFound &&
    now - (fetchedAt ?? openedAtRef.current ?? now) > STALE_AFTER_MS;

  const recordFailure = (cause: unknown, what: string) => {
    const failure = apiFailure(cause);
    setError(failure);
    setAnnouncement(`${what}（${failure.code}）`);
  };

  const recordGenerationFailure = (cause: unknown) => {
    const failure = apiFailure(cause);
    if (isGenerationErrorCode(failure.code)) {
      setGenerationError(failure);
      setAnnouncement(`画像の案は作りませんでした（${failure.code}）。0生成で続行します`);
      return;
    }
    recordFailure(cause, "相談を送信できませんでした");
  };

  const send = async () => {
    const trimmed = message.trim();
    if (trimmed === "" || busy) return;
    setBusy(true);
    setError(null);
    try {
      const entry = await postConsultationMessage(episodeId, { message: trimmed }, fetchImpl);
      absorb([entry]);
      setMessage("");
      setFetchedAt(Date.now());
    } catch (cause) {
      recordFailure(cause, "相談を送信できませんでした");
    } finally {
      setBusy(false);
    }
  };

  const sendWithGeneration = async () => {
    const trimmed = message.trim();
    if (trimmed === "" || busy) return;
    setBusy(true);
    setError(null);
    setGenerationError(null);
    try {
      const entry = await postConsultationMessage(
        episodeId,
        {
          message: trimmed,
          generation: {
            permission: { granted: true, panels_max: panelsMax, images_max: panelsMax },
          },
        },
        fetchImpl,
      );
      absorb([entry]);
      setMessage("");
      setFetchedAt(Date.now());
    } catch (cause) {
      recordGenerationFailure(cause);
    } finally {
      setBusy(false);
    }
  };

  const sendDecline = async () => {
    const trimmed = message.trim();
    if (trimmed === "" || busy) return;
    setBusy(true);
    setError(null);
    try {
      const entry = await postConsultationMessage(episodeId, { message: trimmed }, fetchImpl);
      absorb([entry]);
      setMessage("");
      setFetchedAt(Date.now());
      setGenerationOpen(false);
    } catch (cause) {
      recordFailure(cause, "相談を送信できませんでした");
    } finally {
      setBusy(false);
    }
  };

  const sendPanelRetry = (consultationId: string, proposalId: string, panel: ConsultationGenerationPanel) => {
    void (async () => {
      if (busy) return;
      const baseCreatedAt: unknown = panel.base_created_at;
      if (typeof baseCreatedAt !== "string" || baseCreatedAt === "") {
        setGenerationError({
          code: "generation-panel-base-unknown",
          detail: "このコマの記録が古いため再依頼できません（一覧を更新してください）",
        });
        return;
      }
      setBusy(true);
      setError(null);
      setGenerationError(null);
      try {
        const entry = await postConsultationPanelRetry(episodeId, {
          consultationId,
          proposalId,
          baseCreatedAt,
          panelId: panel.panel_id,
        }, fetchImpl);
        absorb([entry]);
        setFetchedAt(Date.now());
      } catch (cause) {
        recordGenerationFailure(cause);
      } finally {
        setBusy(false);
      }
    })();
  };

  const submitJudgment = (input: ConsultationJudgmentInput) => {
    void (async () => {
      if (busy) return;
      setBusy(true);
      setError(null);
      try {
        const { status, view } = await postConsultationJudgment(episodeId, input, fetchImpl);
        absorbView(view);
        setFetchedAt(Date.now());
        if (status === 202) {
          setAnnouncement("採用した方針を反映する再編集を準備しています");
        } else {
          setAnnouncement("判断を記録しました");
        }
      } catch (cause) {
        recordFailure(cause, "判断を記録できませんでした");
      } finally {
        setBusy(false);
      }
    })();
  };

  const quickAdopt = (consultationId: string, proposalId: string) => {
    submitJudgment({
      consultation_id: consultationId,
      proposal_id: proposalId,
      decision: "adopt",
      scope: { composition: true, appearance: true, audio: true },
      note: null,
    });
  };

  const adopted = payload?.policy?.adopted ?? null;
  const adoptedSummary = adoptedSummaryLinesOf(adopted);
  const fullAuthorized = isCurrentAdoptionAuthorized(payload?.consultations ?? []);
  const messageEntries =
    payload?.consultations.filter((entry) => !isPolicyOutcomeEntry(entry)) ?? [];
  const hasConsultation = messageEntries.length > 0;

  useEffect(() => {
    onStageHint?.({
      hasConsultation,
      adopted: adopted !== null,
      hasPublishedSample,
      fullAuthorized,
    });
  }, [onStageHint, hasConsultation, adopted, hasPublishedSample, fullAuthorized]);

  if (!eligible || notFound) return null;

  const budgetExhausted = error?.code === "consultation-budget-exhausted";
  const llmUnavailable = error?.code === "consultation-llm-unavailable";
  const latestBudget = latestBudgetOf(payload);
  const budgetRemaining =
    latestBudget !== null
      ? latestBudget.llm_calls_limit - latestBudget.llm_calls_used
      : null;
  const budgetNearLimit =
    budgetRemaining !== null &&
    budgetRemaining >= 0 &&
    budgetRemaining <= 2 &&
    !budgetExhausted;
  const rebuildLine = rebuildLineOf(payload?.rebuild);
  const generationState = generationStateOf(payload);
  const refusedReason = refusedReasonOf(generationState);
  const generationRemaining =
    latestBudget !== null ? latestBudget.llm_calls_limit - latestBudget.llm_calls_used : null;

  const noticeBlock = (
    <>
      {budgetExhausted ? (
        <div className="error-notice" role="alert" data-testid="consultation-budget-exhausted">
          <p>このエピソードの相談回数が上限に達しました。</p>
          <p className="field-hint">
            相談は提案なし・文章のみになります（回数はリセットされません）。送り直しても回復しません。
          </p>
        </div>
      ) : llmUnavailable ? (
        <div className="error-notice" role="alert" data-testid="consultation-llm-unavailable">
          <p>提案を作るには本番用AIの実行環境が必要です。今は利用できないため、提案は表示できません。</p>
        </div>
      ) : error !== null ? (
        <ErrorNotice code={error.code} detail={error.detail} />
      ) : null}
      {stale ? (
        <div data-testid="consultation-stale-banner">
          <p className="field-hint">相談の更新が途切れています（再接続中）</p>
          <button
            type="button"
            className="btn-small"
            data-testid="consultation-stale-requery"
            onClick={() => requeryRef.current?.()}
          >
            再照会
          </button>
        </div>
      ) : null}
    </>
  );

  const budgetBlock = (
    <>
      {budgetNearLimit ? (
        <p className="field-hint" data-testid="consultation-budget-warning">
          この動画で試せる回数が残り少なくなっています（残り{budgetRemaining}回）
        </p>
      ) : null}
      {latestBudget !== null && budgetNearLimit ? (
        <details data-testid="consultation-budget-details">
          <summary>利用状況の詳細</summary>
          <ConsultationBudgetReadout budget={latestBudget} degraded={budgetExhausted} />
        </details>
      ) : null}
    </>
  );

  const generationBlock = (
    <div data-testid="generation-permission">
      <button
        type="button"
        className="btn-small"
        aria-expanded={generationOpen}
        onClick={() => setGenerationOpen((prev) => !prev)}
        data-testid="generation-section-toggle"
      >
        画像で雰囲気を見る
      </button>
      {generationOpen ? (
        <div>
          <p className="field-hint">
            画像の案は見本であり、試し編集ではありません。作らなくても相談は続けられます。
          </p>
          {latestBudget !== null && generationRemaining !== null ? (
            <p className="field-hint" data-testid="generation-budget-remaining">
              画像の案に使える残り（AIの呼び出し回数の残り）: {generationRemaining} /{" "}
              {latestBudget.llm_calls_limit}回（パネル数 {panelsMax}枚まで）
            </p>
          ) : (
            <p className="field-hint" data-testid="generation-budget-remaining">
              画像の案に使える残りはまだ分かりません（相談の記録がありません）
            </p>
          )}
          <div className="inline-row" role="group" aria-label="画像の案の枚数">
            <span className="field-hint">パネル数:</span>
            {[1, 2, 3].map((count) => (
              <label key={count}>
                <input
                  type="radio"
                  name="generation-panels-max"
                  checked={panelsMax === count}
                  onChange={() => setPanelsMax(count)}
                  data-testid={`generation-panels-${count}`}
                />
                {count}枚
              </label>
            ))}
          </div>
          {refusedReason !== null ? (
            <p className="field-hint" data-testid="consultation-generation-refused">
              画像の案は作れませんでした：{refusedReason}。0生成で続行します（文章のみの相談は続けられます）
            </p>
          ) : null}
          {generationError !== null ? (
            <div className="error-notice" role="alert" data-testid="consultation-generation-error">
              <p>{generationErrorLineOf(generationError.code)}</p>
              <p className="field-hint">（{generationError.code}）送り直しても回復しない場合は文章のみで続けられます。</p>
            </div>
          ) : null}
          <div className="actions">
            <button
              type="button"
              className="btn-primary"
              onClick={() => void sendWithGeneration()}
              disabled={busy || message.trim() === ""}
              data-testid="generation-request"
            >
              {busy ? "送信中…" : "許可して依頼"}
            </button>
            <button
              type="button"
              onClick={() => void sendDecline()}
              disabled={busy || message.trim() === ""}
              data-testid="generation-decline"
            >
              生成しない（見本だけ）
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );

  const messageBlock = (
    <>
      <label className="field" htmlFor="consultation-message-input">
        {hasConsultation ? "少し変えたい" : "どんな雰囲気にしたいですか？"}
        <textarea
          id="consultation-message-input"
          rows={3}
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          placeholder="例: Aを基本に、Bの書体を使いたい"
          data-testid="consultation-message-input"
        />
      </label>
      <div className="actions">
        <button
          type="button"
          className="btn-primary"
          onClick={() => void send()}
          disabled={busy || message.trim() === ""}
          data-testid="consultation-send"
        >
          {busy ? "送信中…" : "希望を伝える"}
        </button>
      </div>
      <p className="field-hint">提案が届くまで時間がかかります。届けば自動で表示されます。</p>
    </>
  );

  const liveBlock = (
    <div aria-live="polite">
      {announcement !== null ? (
        <p className="field-hint" data-testid="consultation-announcement">
          {announcement}
        </p>
      ) : null}
      {rebuildLine !== null ? (
        <p className="field-hint" data-testid="consultation-rebuild-state">
          {rebuildLine}
        </p>
      ) : null}
    </div>
  );

  const journalDetails = (
    <details data-testid="consultation-journal-details">
      <summary>相談の記録</summary>
      <ConsultationEntryList
        payload={payload}
        busy={busy}
        episodeId={episodeId}
        onJudgment={submitJudgment}
        onPanelRetry={sendPanelRetry}
        interactive={false}
      />
    </details>
  );

  const feedback: SampleFeedback = {
    text: message,
    busy,
    onChange: setMessage,
    onSubmit: () => {
      void send();
    },
  };

  const sampleBlock =
    adopted !== null ? (
      <ConsultationSampleSection
        episodeId={episodeId}
        consultationId={adopted.consultation_id}
        judgmentId={adopted.judgment_id}
        scope={adopted.scope}
        fetchImpl={fetchImpl}
        onView={absorbView}
        adoptedSummary={adoptedSummary}
        feedback={feedback}
        onSamplesChange={({ hasPublished }: { hasPublished: boolean }) =>
          setHasPublishedSample(hasPublished)
        }
      />
    ) : null;

  const styleSaveBlock =
    adopted !== null ? (
      <StyleSaveButton
        episodeId={episodeId}
        adopted={adopted}
        channelId={appliedChannelOf(status)}
        fetchImpl={fetchImpl}
      />
    ) : null;

  if (fullAuthorized) {
    return (
      <section className="card" data-testid="consultation-panel">
        <h2 className="card-title">採用した方針</h2>
        {adoptedSummary.map((line, index) => (
          <p key={index}>{line}</p>
        ))}
        {noticeBlock}
        {journalDetails}
      </section>
    );
  }

  if (adopted !== null && hasPublishedSample) {
    return (
      <section className="card" data-testid="consultation-panel">
        <h2 className="card-title">試し動画を確認する</h2>
        {noticeBlock}
        {sampleBlock}
        {liveBlock}
        {styleSaveBlock}
        {journalDetails}
        {budgetBlock}
      </section>
    );
  }

  return (
    <section className="card direction-stage" data-testid="consultation-panel">
      <h2 className="card-title">編集の方向を決める</h2>
      <p className="page-subtitle">
        やりたいイメージを教えてください。提案の中から選ぶか、言葉で直してください。
      </p>
      {noticeBlock}
      <div className="direction-grid">
        {footageSlot !== null ? (
          <div className="direction-footage">
            <p className="footage-badge">撮影素材の見本</p>
            {footageSlot}
          </div>
        ) : null}
        <div className="direction-chat">
          <ConsultationEntryList
            payload={payload}
            busy={busy}
            episodeId={episodeId}
            onJudgment={submitJudgment}
            onPanelRetry={sendPanelRetry}
            onQuickAdopt={quickAdopt}
          />
          {messageBlock}
          {generationBlock}
        </div>
      </div>
      {liveBlock}
      {sampleBlock}
      {styleSaveBlock}
      {budgetBlock}
    </section>
  );
}
