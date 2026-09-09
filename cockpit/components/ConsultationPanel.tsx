"use client";

// allow: SIZE_OK — 304 pure LOC: one consultation-panel concern. The poll
// merge (absorbView, journal row keys), the judgment forms, and the
// slice-2 rebuild-state line are one interaction unit over the same polled
// view; splitting the merge from its render would separate the U44
// reload-restore behavior from the state that produces it.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  apiFailure,
  CockpitApiError,
  getConsultation,
  isPolicyOutcomeEntry,
  postConsultationJudgment,
  postConsultationMessage,
  type Consultation,
  type ConsultationEntry,
  type ConsultationJudgmentInput,
  type ConsultationPayload,
  type ConsultationRebuild,
  type EpisodeStatus,
} from "@/lib/api";
import ConsultationBudgetReadout, {
  latestBudgetOf,
} from "@/components/ConsultationBudgetReadout";
import ConsultationEntryList from "@/components/ConsultationEntryList";
import StyleSaveButton from "@/components/StyleSaveButton";
import ErrorNotice from "@/components/ErrorNotice";
import { isConsultationStage } from "@/lib/stageGroups";
import { useNow } from "@/components/useNow";

const POLL_INTERVAL_MS = 2000;
const STALE_AFTER_MS = 15000;

type ConsultationPanelProps = {
  episodeId: string;
  status: EpisodeStatus | null;
  fetchImpl?: typeof fetch;
};

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
}: ConsultationPanelProps) {
  const [payload, setPayload] = useState<ConsultationPayload | null>(null);
  const [error, setError] = useState<{ code: string; detail: string } | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [fetchedAt, setFetchedAt] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [announcement, setAnnouncement] = useState<string | null>(null);
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
      const next: ConsultationPayload = { consultations: [...byKey.values()] };
      const policy = nextPolicy ?? prev?.policy;
      const rebuild = nextRebuild ?? prev?.rebuild;
      if (policy !== undefined) next.policy = policy;
      if (rebuild !== undefined) next.rebuild = rebuild;
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

  if (!eligible || notFound) return null;

  const budgetExhausted = error?.code === "consultation-budget-exhausted";
  const llmUnavailable = error?.code === "consultation-llm-unavailable";
  const latestBudget = latestBudgetOf(payload);
  const rebuildLine = rebuildLineOf(payload?.rebuild);

  return (
    <section className="card" data-testid="consultation-panel">
      <h2 className="card-title">編集の方針相談</h2>
      <p className="page-subtitle" style={{ marginBottom: "var(--space-3)" }}>
        編集を始める前に、このエピソードの方針を文章で相談できます。届いた提案を確認して、1件ずつ判断を記録してください。
      </p>
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
      {latestBudget !== null ? (
        <ConsultationBudgetReadout budget={latestBudget} degraded={budgetExhausted} />
      ) : null}
      <label className="field" htmlFor="consultation-message-input">
        相談の内容
        <textarea
          id="consultation-message-input"
          rows={3}
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          placeholder="例: 普段の作りみたいに、冒頭は引きの映像から見せたい"
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
          {busy ? "送信中…" : "送信"}
        </button>
      </div>
      <p className="field-hint">提案が届くまで時間がかかります。届けば自動で表示されます。</p>
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
      <ConsultationEntryList payload={payload} busy={busy} onJudgment={submitJudgment} />
      {payload?.policy?.adopted !== null && payload?.policy?.adopted !== undefined ? (
        <StyleSaveButton
          episodeId={episodeId}
          adopted={payload.policy.adopted}
          channelId={appliedChannelOf(status)}
          fetchImpl={fetchImpl}
        />
      ) : null}
    </section>
  );
}
