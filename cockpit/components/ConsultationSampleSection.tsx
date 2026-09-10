"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  apiFailure,
  CockpitApiError,
  getConsultationSamples,
  isSamplePublished,
  isSampleWorking,
  postConsultationJudgment,
  postConsultationSample,
  samplePreviewUrl,
  type ConsultationPayload,
  type ConsultationSample,
  type ConsultationScope,
} from "@/lib/api";
import ErrorNotice from "@/components/ErrorNotice";

const SAMPLE_POLL_INTERVAL_MS = 2000;

// 冒頭30秒相当のRecord窓フォールバック（24fps想定）。実尺はサーバーが
// IRのレートで確定し、範囲外は422で正直に表示される。

function newOperationId(): string {
  try {
    const runtimeCrypto: unknown =
      typeof globalThis !== "undefined"
        ? (globalThis as { crypto?: unknown }).crypto
        : undefined;
    if (
      typeof runtimeCrypto === "object" &&
      runtimeCrypto !== null &&
      typeof (runtimeCrypto as { randomUUID?: unknown }).randomUUID === "function"
    ) {
      return (runtimeCrypto as { randomUUID: () => string }).randomUUID();
    }
  } catch {
    // fall through to the Math.random fallback below
  }
  return `op-${Date.now()}-${Math.floor(Math.random() * 1000000)}`;
}

function requestStateLine(state: string): string {
  if (state === "published") return "試し動画を保存しました";
  if (isSampleWorking(state)) return "作成中です";
  return `状態を確認しています（${state}）`;
}

function sampleStatusLine(sample: ConsultationSample): string {
  if (isSamplePublished(sample)) return "完成";
  if (isSampleWorking(sample.status)) return "作成中です";
  return `状態を確認しています（${sample.status}）`;
}

export default function ConsultationSampleSection({
  episodeId,
  consultationId,
  judgmentId,
  scope,
  fetchImpl,
  onView,
}: {
  episodeId: string;
  consultationId: string;
  judgmentId: string;
  scope: ConsultationScope;
  fetchImpl?: typeof fetch;
  onView: (view: ConsultationPayload) => void;
}) {
  const [samples, setSamples] = useState<ConsultationSample[] | null>(null);
  const [supported, setSupported] = useState(true);
  const [requestBusy, setRequestBusy] = useState(false);
  const [requestState, setRequestState] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<{
    code: string;
    detail: string;
  } | null>(null);
  const [authorizeBusy, setAuthorizeBusy] = useState(false);
  const [authorizedSampleId, setAuthorizedSampleId] = useState<string | null>(null);
  const [authorizeError, setAuthorizeError] = useState<{
    code: string;
    detail: string;
  } | null>(null);
  const [announcement, setAnnouncement] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const payload = await getConsultationSamples(episodeId, fetchImpl);
      setSamples(payload.samples);
    } catch (cause) {
      if (cause instanceof CockpitApiError && cause.status === 404) {
        setSupported(false);
      }
    }
  }, [episodeId, fetchImpl]);

  useEffect(() => {
    if (!supported) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      await load();
      if (cancelled) return;
      timer = setTimeout(() => void poll(), SAMPLE_POLL_INTERVAL_MS);
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [supported, load]);

  if (!supported) return null;

  const published = (samples ?? []).filter(isSamplePublished);
  const displayed = published.length > 0 ? published[published.length - 1] : null;

  const requestSample = () => {
    void (async () => {
      if (requestBusy) return;
      setRequestBusy(true);
      setRequestError(null);
      try {
        const result = await postConsultationSample(
          episodeId,
          {
            consultation_id: consultationId,
            judgment_id: judgmentId,
            operation_id: newOperationId(),
          },
          fetchImpl,
        );
        setRequestState(result.state);
        setAnnouncement(requestStateLine(result.state));
        await load();
      } catch (cause) {
        const failure = apiFailure(cause);
        setRequestError(failure);
        setAnnouncement(`試し動画を作れませんでした（${failure.code}）。相談を続けられます`);
      } finally {
        setRequestBusy(false);
      }
    })();
  };

  const authorizeFull = () => {
    void (async () => {
      if (authorizeBusy || displayed === null) return;
      const sampleId = displayed.sample_id;
      setAuthorizeBusy(true);
      setAuthorizeError(null);
      try {
        const { view } = await postConsultationJudgment(
          episodeId,
          {
            consultation_id: consultationId,
            proposal_id: null,
            decision: "full_authorized",
            scope,
            note: null,
            sample_id: sampleId,
            operation_id: newOperationId(),
          },
          fetchImpl,
        );
        onView(view);
        setAuthorizedSampleId(sampleId);
        setAnnouncement(`この方向で全編へ進めます（試し動画 ${sampleId} の確認に基づく）`);
      } catch (cause) {
        const failure = apiFailure(cause);
        setAuthorizeError(failure);
        setAnnouncement(`全編への承認を記録できませんでした（${failure.code}）`);
      } finally {
        setAuthorizeBusy(false);
      }
    })();
  };

  return (
    <section data-testid="consultation-sample-section">
      <h3>30秒以内の試し動画</h3>
      <p className="field-hint">採用した探した場面から、短い試し動画を作れます。尺はサーバーが確定します。</p>
      <div className="actions">
        <button
          type="button"
          className="btn-primary"
          onClick={requestSample}
          disabled={requestBusy}
          data-testid="sample-request"
        >
          {requestBusy ? "作成中…" : "30秒の試し動画を作る"}
        </button>
      </div>
      {requestState !== null ? (
        <p className="field-hint" data-testid="sample-request-state">
          {requestStateLine(requestState)}
        </p>
      ) : null}
      {requestError !== null ? (
        <div data-testid="sample-request-error">
          <ErrorNotice code={requestError.code} detail={requestError.detail} />
          <p className="field-hint">相談を続けられます</p>
        </div>
      ) : null}
      {(samples ?? []).map((sample) => (
        <div className="card" key={sample.sample_id} data-testid="consultation-sample">
          <p className="field-hint" data-testid="consultation-sample-kind">
            30秒以内の試し動画
          </p>
          <details data-testid="consultation-sample-record">
            <summary>詳しい記録</summary>
            <p className="mono" data-testid="consultation-sample-id">
              {sample.sample_id}
            </p>
          </details>
          <p className="field-hint" data-testid="consultation-sample-status">
            {sampleStatusLine(sample)}
          </p>
          {isSamplePublished(sample) ? (
            <video
              controls
              preload="metadata"
              src={samplePreviewUrl(episodeId, sample.sample_id)}
              data-testid="sample-preview"
            />
          ) : null}
        </div>
      ))}
      {displayed !== null ? (
        <div>
          <div className="actions">
            <button
              type="button"
              className="btn-primary"
              onClick={authorizeFull}
              disabled={authorizeBusy}
              data-testid="full-authorize"
            >
              {authorizeBusy ? "記録中…" : "この方向で全編へ進める"}
            </button>
          </div>
          {authorizedSampleId !== null ? (
            <p className="field-hint" data-testid="full-authorize-done">
              この方向で全編へ進めます（試し動画 {authorizedSampleId}{" "}
              の確認に基づく）
            </p>
          ) : null}
          {authorizeError !== null ? (
            <div data-testid="full-authorize-error">
              <ErrorNotice code={authorizeError.code} detail={authorizeError.detail} />
            </div>
          ) : null}
        </div>
      ) : null}
      <div aria-live="polite">
        {announcement !== null ? (
          <p className="field-hint" data-testid="consultation-sample-announcement">
            {announcement}
          </p>
        ) : null}
      </div>
    </section>
  );
}
