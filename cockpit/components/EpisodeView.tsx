"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  apiFailure,
  getEpisodeFlags,
  getEpisodeStatus,
  probeEpisodePreview,
  CockpitApiError,
  type EpisodeStatus,
  type FlagsPayload,
} from "@/lib/api";
import ErrorNotice from "@/components/ErrorNotice";
import EpisodeProgress from "@/components/EpisodeProgress";
import PreviewPlayer, { type PreviewAvailability } from "@/components/PreviewPlayer";
import FlagList from "@/components/FlagList";
import BeforeAfterSummary from "@/components/BeforeAfterSummary";
import FinishingDomainPanel from "@/components/FinishingDomainPanel";
import ReviewChatPanel from "@/components/ReviewChatPanel";
import { useNow } from "@/components/useNow";
import { jobStatusSuffix } from "@/lib/stageGroups";

const POLL_INTERVAL_MS = 2000;
const STALE_AFTER_MS = 15000;

type EpisodeViewProps = {
  episodeId: string;
};

/**
 * Episode status view: polling progress (ETA only when measured), preview
 * player, flagged review items with timestamp jump, and the before/after
 * summary when the payload carries one (task 46).
 *
 * 工程2P poll hardening: every successful status fetch records `fetchedAt`
 * (the 状態取得時刻); failures keep polling but never refresh it. 15s
 * without a successful fetch shows the dedicated 途切れ banner (distinct
 * from the error notice, cleared on recovery) with 再照会. The aux
 * fetches (flags / preview probe) are fire-and-track: they must never
 * delay the next poll schedule (codex条件6). visibilitychange/focus
 * trigger an immediate requery.
 */
export default function EpisodeView({ episodeId }: EpisodeViewProps) {
  const [status, setStatus] = useState<EpisodeStatus | null>(null);
  const [flags, setFlags] = useState<FlagsPayload | null>(null);
  const [preview, setPreview] = useState<PreviewAvailability>("checking");
  const [error, setError] = useState<{ code: string; detail: string } | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [fetchedAt, setFetchedAt] = useState<number | null>(null);
  const now = useNow(true);
  const openedAtRef = useRef<number | null>(null);
  if (openedAtRef.current === null) openedAtRef.current = Date.now();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const requeryRef = useRef<(() => void) | null>(null);

  const seekTo = useCallback((seconds: number) => {
    const video = videoRef.current;
    if (video !== null) video.currentTime = seconds;
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const refreshAux = () => {
      void Promise.allSettled([
        getEpisodeFlags(episodeId),
        probeEpisodePreview(episodeId),
      ]).then(([flagsResult, previewResult]) => {
        if (cancelled) return;
        if (flagsResult.status === "fulfilled") setFlags(flagsResult.value);
        if (previewResult.status === "fulfilled") {
          setPreview(previewResult.value ? "available" : "not_generated");
        }
      });
    };

    const poll = async () => {
      try {
        const next = await getEpisodeStatus(episodeId);
        if (cancelled) return;
        setStatus(next);
        setError(null);
        setFetchedAt(Date.now());
        timer = setTimeout(() => void poll(), POLL_INTERVAL_MS);
        refreshAux();
      } catch (cause) {
        if (cancelled) return;
        setError(apiFailure(cause));
        if (cause instanceof CockpitApiError && cause.status === 404) {
          setNotFound(true);
          return; // stop polling — the episode does not exist
        }
        timer = setTimeout(() => void poll(), POLL_INTERVAL_MS);
      }
    };

    const requery = () => {
      if (cancelled) return;
      if (timer !== undefined) clearTimeout(timer);
      void poll();
    };
    requeryRef.current = requery;

    void poll();
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
  }, [episodeId]);

  const stale =
    !notFound && now - (fetchedAt ?? openedAtRef.current ?? now) > STALE_AFTER_MS;

  return (
    <div>
      <Link href="/new-episode" className="top-link">
        ← 新しいエピソード
      </Link>
      {error !== null ? <ErrorNotice code={error.code} detail={error.detail} /> : null}
      {stale ? (
        <div className="card" data-testid="stale-banner">
          <p>状態の更新が途切れています（再接続中）</p>
          <p className="field-hint">安全な中止は未実装です。</p>
          <button
            type="button"
            className="btn-small"
            data-testid="stale-requery"
            onClick={() => requeryRef.current?.()}
          >
            再照会
          </button>
        </div>
      ) : null}
      <section className="card">
        <h2 className="card-title">進捗</h2>
        <dl className="status-list">
          <div>
            <dt>エピソード</dt>
            <dd className="mono">{episodeId}</dd>
          </div>
          <div>
            <dt>ステータス</dt>
            <dd data-testid="episode-status">
              {status !== null
                ? `${status.status}${
                    jobStatusSuffix(status.status) !== null
                      ? `（${jobStatusSuffix(status.status)}）`
                      : ""
                  }`
                : "…"}
            </dd>
          </div>
          <div>
            <dt>現在のステージ</dt>
            <dd data-testid="current-stage">
              {status !== null ? status.current_stage : "…"}
            </dd>
          </div>
        </dl>
        {notFound ? (
          <p className="empty-note">このエピソードは見つかりません。</p>
        ) : status !== null ? (
          <EpisodeProgress status={status} onRequery={() => requeryRef.current?.()} />
        ) : (
          <p className="empty-note">読み込み中…</p>
        )}
      </section>
      <FinishingDomainPanel episodeId={episodeId} />
      <section className="card">
        <h2 className="card-title">プレビュー</h2>
        <PreviewPlayer episodeId={episodeId} state={preview} videoRef={videoRef} />
      </section>
      <section className="card">
        <h2 className="card-title">レビューflag</h2>
        {flags !== null ? (
          <FlagList
            flags={flags.flags}
            notYetGenerated={flags.not_yet_generated}
            canSeek={preview === "available"}
            onSeek={seekTo}
          />
        ) : (
          <p className="empty-note">レビューflagの有無を確認しています…</p>
        )}
      </section>
      <ReviewChatPanel
        episodeId={episodeId}
        getAtSeconds={() => videoRef.current?.currentTime ?? null}
        status={status}
        previewOk={preview === "available"}
      />
      <BeforeAfterSummary summary={status?.before_after ?? null} />
    </div>
  );
}
