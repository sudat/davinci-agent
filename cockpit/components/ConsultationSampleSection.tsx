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
  if (isSamplePublished(sample)) return "試し動画を保存しました";
  if (isSampleWorking(sample.status)) return "作成中です";
  return `状態を確認しています（${sample.status}）`;
}

/** Newest published sample wins. Key is published_at (fallback
 *  created_at); ties keep the LATER list position so an oldest-first
 *  server list still resolves to its tail. No 'representative' flag is
 *  invented — the list order / timestamps decide. */
function sampleOrderKey(sample: ConsultationSample): string {
  const publishedAt: unknown = sample.published_at;
  if (typeof publishedAt === "string" && publishedAt !== "") return publishedAt;
  return typeof sample.created_at === "string" ? sample.created_at : "";
}

export function newestPublishedSample(
  samples: readonly ConsultationSample[],
): ConsultationSample | null {
  const published = samples.filter(isSamplePublished);
  if (published.length === 0) return null;
  let best = published[0]!;
  let bestKey = sampleOrderKey(best);
  for (const candidate of published.slice(1)) {
    const key = sampleOrderKey(candidate);
    if (key >= bestKey) {
      best = candidate;
      bestKey = key;
    }
  }
  return best;
}

type SampleWindowLike = { label?: unknown };

function windowsOf(sample: ConsultationSample): SampleWindowLike[] {
  // Live payloads nest the scene windows under identity.windows; older
  // payloads carry top-level windows. Either source drives the breakdown.
  const record = sample as { windows?: unknown; identity?: unknown };
  const sources: unknown[] = [record.windows];
  if (typeof record.identity === "object" && record.identity !== null) {
    sources.push((record.identity as { windows?: unknown }).windows);
  }
  for (const source of sources) {
    if (!Array.isArray(source)) continue;
    const windows = source.filter(
      (window): window is SampleWindowLike =>
        typeof window === "object" && window !== null,
    );
    if (windows.length > 0) return windows;
  }
  return [];
}

function formatSeconds(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds)) return "";
  const rounded = Math.round(totalSeconds * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : String(rounded);
}

export type SampleFeedback = {
  text: string;
  busy: boolean;
  onChange: (value: string) => void;
  onSubmit: () => void;
};

export default function ConsultationSampleSection({
  episodeId,
  consultationId,
  judgmentId,
  scope,
  fetchImpl,
  onView,
  adoptedSummary = null,
  feedback = null,
  onSamplesChange,
}: {
  episodeId: string;
  consultationId: string;
  judgmentId: string;
  scope: ConsultationScope;
  fetchImpl?: typeof fetch;
  onView: (view: ConsultationPayload) => void;
  /** 採用した方針の2〜3行（親が採用ポリシーから抜粋）。あるときだけ
   *  試し動画の右（上）に「採用した方針」として出す。 */
  adoptedSummary?: readonly string[] | null;
  /** 見てどうでしたか？の感想欄（親の相談送信へつなぐ）。あるときだけ
   *  修正してもう一度見る（副）と並べて出す。 */
  feedback?: SampleFeedback | null;
  onSamplesChange?: (info: { hasPublished: boolean }) => void;
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
  const onSamplesChangeRef = useRef(onSamplesChange);
  onSamplesChangeRef.current = onSamplesChange;

  const load = useCallback(async () => {
    try {
      const payload = await getConsultationSamples(episodeId, fetchImpl);
      setSamples(payload.samples);
      onSamplesChangeRef.current?.({
        hasPublished: newestPublishedSample(payload.samples) !== null,
      });
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

  const displayed = newestPublishedSample(samples ?? []);
  const working = (samples ?? []).filter((sample) => isSampleWorking(sample.status));
  const older = (samples ?? []).filter(
    (sample) =>
      isSamplePublished(sample) &&
      (displayed === null || sample.sample_id !== displayed.sample_id),
  );
  const displayedWindows = displayed !== null ? windowsOf(displayed) : [];
  const displayedSeconds =
    displayed !== null && typeof displayed.total_seconds === "number"
      ? displayed.total_seconds
      : null;

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
    <section data-testid="consultation-sample-section" className="sample-stage">
      <h3>採用した方向の見本</h3>
      {adoptedSummary !== null && adoptedSummary.length > 0 ? (
        <div data-testid="adopted-policy-summary">
          <h4>採用した方針</h4>
          {adoptedSummary.slice(0, 3).map((line, index) => (
            <p key={index}>{line}</p>
          ))}
        </div>
      ) : null}
      {displayed === null ? (
        <>
          <p className="field-hint">採用した方向で短い試し動画を作れます。尺はサーバーが確定します。</p>
          <div className="actions">
            <button
              type="button"
              className="btn-primary"
              onClick={requestSample}
              disabled={requestBusy}
              data-testid="sample-request"
            >
              {requestBusy ? "作成中…" : "試し動画を作る"}
            </button>
          </div>
        </>
      ) : (
        <div className="sample-stage-grid">
          <div className="sample-stage-player">
            {displayedWindows.length > 0 && displayedSeconds !== null ? (
              <p className="field-hint" data-testid="sample-breakdown">
                {displayedWindows.length}か所・{formatSeconds(displayedSeconds)}秒
              </p>
            ) : displayedSeconds !== null ? (
              <p className="field-hint" data-testid="sample-breakdown">
                {formatSeconds(displayedSeconds)}秒の試し動画
              </p>
            ) : null}
            {displayedWindows.length > 0 ? (
              <ul className="list-plain" data-testid="sample-scenes">
                {displayedWindows.map((window, index) => (
                  <li key={index}>
                    {typeof window.label === "string" && window.label !== ""
                      ? window.label
                      : `か所${index + 1}`}
                  </li>
                ))}
              </ul>
            ) : null}
            <video
              controls
              preload="metadata"
              src={samplePreviewUrl(episodeId, displayed.sample_id)}
              data-testid="sample-preview"
            />
            <p className="field-hint" data-testid="consultation-sample-kind">
              試し動画（撮影素材から作成）
            </p>
          </div>
          <div className="sample-stage-feedback">
            {feedback !== null ? (
              <>
                <label className="field" htmlFor="sample-feedback-input">
                  見てどうでしたか？
                  <textarea
                    id="sample-feedback-input"
                    rows={3}
                    value={feedback.text}
                    onChange={(event) => feedback.onChange(event.target.value)}
                    placeholder="方向はいい。テロップをもう少し暗くして"
                    data-testid="sample-feedback-input"
                  />
                </label>
                <div className="actions">
                  <button
                    type="button"
                    className="btn-small"
                    onClick={feedback.onSubmit}
                    disabled={feedback.busy || feedback.text.trim() === ""}
                    data-testid="sample-feedback-submit"
                  >
                    {feedback.busy ? "送信中…" : "修正してもう一度見る"}
                  </button>
                </div>
              </>
            ) : null}
            <div className="actions">
              <button
                type="button"
                className="btn-small"
                onClick={requestSample}
                disabled={requestBusy}
                data-testid="sample-request"
              >
                {requestBusy ? "作成中…" : "試し動画を作り直す"}
              </button>
              <button
                type="button"
                className="btn-primary"
                onClick={authorizeFull}
                disabled={authorizeBusy}
                data-testid="full-authorize"
              >
                {authorizeBusy ? "記録中…" : "この方向で全編へ進む"}
              </button>
            </div>
            {authorizedSampleId !== null ? (
              <p className="field-hint" data-testid="full-authorize-done">
                この方向で全編へ進めます（確認した試し動画に基づく）
              </p>
            ) : null}
            {authorizeError !== null ? (
              <div data-testid="full-authorize-error">
                <ErrorNotice code={authorizeError.code} detail={authorizeError.detail} />
              </div>
            ) : null}
            <details data-testid="sample-record">
              <summary>試し動画の記録</summary>
              <p className="mono" data-testid="consultation-sample-id">
                {displayed.sample_id}
              </p>
              <p className="field-hint" data-testid="consultation-sample-status">
                {sampleStatusLine(displayed)}
              </p>
              {older.length > 0 ? (
                <details data-testid="previous-samples">
                  <summary>以前の試し動画の記録</summary>
                  {older.map((sample) => (
                    <p className="mono" key={sample.sample_id}>
                      {sample.sample_id} — {sampleStatusLine(sample)}
                    </p>
                  ))}
                </details>
              ) : null}
            </details>
          </div>
        </div>
      )}
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
      {working.map((sample) => (
        <div className="card" key={sample.sample_id} data-testid="consultation-sample">
          <p className="field-hint" data-testid="consultation-sample-kind">
            試し動画
          </p>
          <p className="mono" data-testid="consultation-sample-id">
            {sample.sample_id}
          </p>
          <p className="field-hint" data-testid="consultation-sample-status">
            {sampleStatusLine(sample)}
          </p>
        </div>
      ))}

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
