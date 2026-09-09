"use client";

import { useCallback, useEffect, useState } from "react";
import {
  apiFailure,
  executeApproval,
  getApprovalSessions,
  CockpitApiError,
  outputLabel,
  type ApprovalSessionsPayload,
  type OutputId,
} from "@/lib/api";
import ErrorNotice from "@/components/ErrorNotice";
import { hasMultipleOutputs, useOutputScope } from "@/components/OutputScope";

const PURPOSE_LABEL: Record<string, string> = {
  editorial: "編集承認",
  presentation: "見た目承認",
  privacy: "プライバシー確認",
  rights: "権利確認",
  final: "最終承認",
  publication: "公開承認",
  manual_freeze: "手動確定確認",
};

const SESSION_LABEL: Record<string, string> = {
  "editorial-presentation": "編集・見たて",
  "final-publication": "最終・公開",
};

type ApprovalSessionsProps = {
  episodeId: string;
  actorId?: string;
  fetchImpl?: typeof fetch;
  /** 工程5: explicit output override (tests). When omitted, the shared
   *  output scope (the EpisodeView selector) applies; landscape omits the
   *  output dimension so requests stay byte-identical. */
  outputId?: OutputId;
};

/**
 * Bundled approval sessions (PRD 13.4): pending approvals arrive grouped
 * into at most two normal blocking sessions (plus explained exception
 * stops); one approve action per session decides every item in it, and
 * decided acceptances stay listed so a restart visibly preserves them.
 */
export default function ApprovalSessions({
  episodeId,
  actorId = "cockpit-operator",
  fetchImpl,
  outputId,
}: ApprovalSessionsProps) {
  const [payload, setPayload] = useState<ApprovalSessionsPayload | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ code: string; detail: string } | null>(null);
  const [notFound, setNotFound] = useState(false);
  const outputScope = useOutputScope();
  const selectedOutput: OutputId = outputId ?? outputScope.selected;
  const showOutputNote =
    hasMultipleOutputs(outputScope.outputs) || outputId === "vertical";

  const refresh = useCallback(async () => {
    try {
      setPayload(await getApprovalSessions(episodeId, fetchImpl, selectedOutput));
      setError(null);
      setNotFound(false);
    } catch (cause) {
      // A missing episode is the page-level 404 (EpisodeView surfaces it);
      // the sessions panel stays quiet instead of duplicating the card.
      if (cause instanceof CockpitApiError && cause.status === 404) {
        setNotFound(true);
        return;
      }
      setError(apiFailure(cause));
    }
  }, [episodeId, fetchImpl, selectedOutput]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const approveSession = async (recordIds: string[]) => {
    setBusy(true);
    setError(null);
    try {
      for (const recordId of recordIds) {
        await executeApproval(
          episodeId,
          recordId,
          { decision: "approve", actor_id: actorId },
          fetchImpl,
          selectedOutput,
        );
      }
      await refresh();
    } catch (cause) {
      setError(apiFailure(cause));
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  if (notFound) {
    return null;
  }

  return (
    <section className="card" data-testid="approval-sessions">
      <h2 className="card-title">承認セッション</h2>
      <p className="page-subtitle" style={{ marginBottom: "var(--space-3)" }}>
        互換性のある承認は最大2セッションにまとめられます（編集・見たて / 最終・公開）。
      </p>
      {showOutputNote ? (
        <p className="field-hint" data-testid="approval-output-note">
          {outputLabel(selectedOutput)}の承認を表示しています（横版と縦版の承認は別々です）。
        </p>
      ) : null}
      {error !== null ? <ErrorNotice code={error.code} detail={error.detail} /> : null}
      {payload === null ? (
        <p className="empty-note">承認状態を確認しています…</p>
      ) : (
        <>
          <p data-testid="blocking-session-count">
            ブロック承認セッション: {payload.blocking_session_count}
          </p>
          {payload.sessions.length > 0 ? (
            payload.sessions.map((session) => (
              <div
                className="card"
                key={session.session_key}
                data-testid="approval-session"
                data-session-key={session.session_key}
                style={{ marginTop: "var(--space-3)" }}
              >
                <h3 className="section-title">
                  {SESSION_LABEL[session.session_key] ?? session.session_key}
                  {session.kind === "exception" ? "（個別停止）" : ""}
                </h3>
                <ul className="list-plain">
                  {session.items.map((item) => (
                    <li key={item.record_id} data-testid="approval-item">
                      <span>{PURPOSE_LABEL[item.purpose] ?? item.purpose}</span>
                      <span className="mono">{item.record_id}</span>
                    </li>
                  ))}
                </ul>
                {session.explanation !== null ? (
                  <p className="field-hint" data-testid="session-explanation">
                    {session.explanation}
                  </p>
                ) : null}
                <div className="actions">
                  <button
                    type="button"
                    className="btn-primary"
                    onClick={() => void approveSession(session.items.map((i) => i.record_id))}
                    disabled={busy}
                    data-testid="approve-session-button"
                  >
                    このセッションを承認
                  </button>
                </div>
              </div>
            ))
          ) : (
            <p className="empty-note" data-testid="approvals-empty">
              待機中の承認はありません。
            </p>
          )}
          {payload.decided.length > 0 ? (
            <div style={{ marginTop: "var(--space-3)" }}>
              <h3 className="section-title">承認済み（記録は再起動後も保持）</h3>
              <ul className="list-plain">
                {payload.decided.map((fact) => (
                  <li key={fact.record_id} data-testid="decided-item">
                    <span>{PURPOSE_LABEL[fact.purpose] ?? fact.purpose}: 承認済み</span>
                    <span className="mono">{fact.record_id}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}
