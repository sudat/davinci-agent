"use client";

import { useEffect, useState } from "react";
import {
  apiFailure,
  CockpitApiError,
  getFinishingStatus,
  type FinishingDomainStatus,
  type FinishingStatusPayload,
} from "@/lib/api";
import ErrorNotice from "@/components/ErrorNotice";

const DOMAIN_LABEL: Record<string, string> = {
  editorial_construction: "編集組み立て",
  subtitle: "字幕",
  audio_finishing: "音声仕上げ",
  color_finishing: "色補正",
  framing_motion: "フレーミング／拡大縮小",
  graphics_presentation: "画面表示（テロップ等）",
  delivery_qc: "納品前品質確認",
};

const POLL_INTERVAL_MS = 2000;

const STATUS_LABEL: Record<string, string> = {
  applied: "適用済み",
  intentionally_not_needed: "意図的に不要",
  manual_fallback_required: "手動対応必要",
  blocked: "ブロック中",
};

function statusChipClass(status: string): string {
  switch (status) {
    case "applied":
      return "finishing-chip finishing-chip-applied";
    case "intentionally_not_needed":
      return "finishing-chip finishing-chip-intentionally-not-needed";
    case "manual_fallback_required":
      return "finishing-chip finishing-chip-manual";
    case "blocked":
      return "finishing-chip finishing-chip-blocked";
    default:
      return "finishing-chip finishing-chip-unknown";
  }
}

function statusExplanation(status: string): string | null {
  switch (status) {
    case "applied":
      return null;
    case "intentionally_not_needed":
      return "今回はこの項目を実施していません。";
    case "manual_fallback_required":
      return "この項目は手動対応が必要です。";
    case "blocked":
      return "この項目で処理が止まっています。";
    default:
      return "記録された状態を確認してください。";
  }
}

function actionLineOf(status: string): string | null {
  switch (status) {
    case "manual_fallback_required":
      return "内容を確認して、手動で対応してください。";
    case "blocked":
      return "止まっている理由を確認して、対応してください。";
    default:
      return null;
  }
}

const KNOWN_STATUS = new Set([
  "applied",
  "intentionally_not_needed",
  "manual_fallback_required",
  "blocked",
]);

function needsAction(entry: FinishingDomainStatus): boolean {
  return (
    entry.status === "manual_fallback_required" ||
    entry.status === "blocked" ||
    !KNOWN_STATUS.has(entry.status)
  );
}

function ActionRow({ entry }: { entry: FinishingDomainStatus }) {
  const label = DOMAIN_LABEL[entry.domain] ?? entry.domain;
  const chipLabel = STATUS_LABEL[entry.status] ?? entry.status;
  const action = actionLineOf(entry.status);
  return (
    <li data-testid="finishing-domain-row" data-domain={entry.domain} data-status={entry.status}>
      <div className="finishing-row">
        <span className="finishing-domain">{label}</span>
        <span className={statusChipClass(entry.status)} data-testid="finishing-chip" data-status={entry.status}>
          {chipLabel}
        </span>
      </div>
      {action !== null ? (
        <p className="finishing-action" data-testid="finishing-action">
          {action}
        </p>
      ) : (
        <p className="finishing-status-explanation">{statusExplanation(entry.status)}</p>
      )}
      {entry.justification !== null ? (
        <details className="finishing-evidence">
          <summary>記録された理由（原文）</summary>
          <p className="finishing-justification" data-testid="finishing-justification">
            {entry.justification}
          </p>
        </details>
      ) : null}
    </li>
  );
}

type FinishingDomainPanelProps = {
  episodeId: string;
  fetchImpl?: typeof fetch;
};

function FinishingRow({ entry }: { entry: FinishingDomainStatus }) {
  const label = DOMAIN_LABEL[entry.domain] ?? entry.domain;
  const chipLabel = STATUS_LABEL[entry.status] ?? entry.status;
  const explanation = statusExplanation(entry.status);
  return (
    <li data-testid="finishing-domain-row" data-domain={entry.domain} data-status={entry.status}>
      <div className="finishing-row">
        <span className="finishing-domain">{label}</span>
        <span className={statusChipClass(entry.status)} data-testid="finishing-chip" data-status={entry.status}>
          {chipLabel}
        </span>
      </div>
      {explanation !== null ? <p className="finishing-status-explanation">{explanation}</p> : null}
      {entry.justification !== null ? (
        <details className="finishing-evidence">
          <summary>記録された理由（原文）</summary>
          <p className="finishing-justification" data-testid="finishing-justification">
            {entry.justification}
          </p>
        </details>
      ) : null}
    </li>
  );
}

/**
 * Read-only finishing domains status (task finishing-panel): 7 rows, each
 * showing the Japanese label + raw domain id, a status chip with distinct
 * visual treatment per status, and the recorded justification as-is.
 * Absent file → honest "not yet run" note; 404 episode → hidden (episode
 * existence is surfaced by the status poll); 422 malformed → error envelope.
 */
export default function FinishingDomainPanel({
  episodeId,
  fetchImpl,
}: FinishingDomainPanelProps) {
  const [payload, setPayload] = useState<FinishingStatusPayload | null>(null);
  const [error, setError] = useState<{ code: string; detail: string } | null>(null);
  const [notFound, setNotFound] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const next = await getFinishingStatus(episodeId, fetchImpl);
        if (cancelled) return;
        setPayload(next);
        setError(null);
        setNotFound(false);
        if (!next.available) {
          timer = setTimeout(() => void load(), POLL_INTERVAL_MS);
        }
      } catch (cause) {
        if (cancelled) return;
        if (cause instanceof CockpitApiError && cause.status === 404) {
          setNotFound(true);
          return;
        }
        setError(apiFailure(cause));
      }
    };
    void load();
    return () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [episodeId, fetchImpl]);

  if (notFound) return null;

  const actionEntries =
    payload !== null && payload.available
      ? payload.domains.filter(needsAction)
      : [];
  if (payload !== null && payload.available && actionEntries.length === 0) {
    return null;
  }
  if (payload !== null && !payload.available) {
    return null;
  }

  return (
    <section
      className="card"
      data-testid="finishing-domain-panel"
      aria-live="polite"
      aria-busy={payload === null && error === null}
    >
      <h2 className="card-title">仕上げ項目の状況</h2>
      <p className="field-hint finishing-intro">
        手動での対応が必要な項目があります。内容を確認してください。
      </p>
      {error !== null ? (
        <ErrorNotice code={error.code} detail={error.detail} />
      ) : payload === null ? (
        <p className="empty-note" data-testid="finishing-loading">
          仕上げ状況を確認しています…
        </p>
      ) : (
        <>
          <ul className="finishing-list" data-testid="finishing-action-list">
            {actionEntries.map((entry) => (
              <ActionRow key={entry.domain} entry={entry} />
            ))}
          </ul>
          <details data-testid="finishing-all-domains">
            <summary>詳しい記録（すべての項目）</summary>
            <ul className="finishing-list">
              {payload.domains.map((entry) => (
                <FinishingRow key={entry.domain} entry={entry} />
              ))}
            </ul>
          </details>
        </>
      )}
    </section>
  );
}
