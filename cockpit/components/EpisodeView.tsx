"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
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

const POLL_INTERVAL_MS = 2000;

type EpisodeViewProps = {
  episodeId: string;
};

/**
 * Episode status view: polling progress (ETA only when measured),
 * preview player, flagged review items with timestamp jump, and the
 * before/after summary when the payload carries one (task 46).
 */
export default function EpisodeView({ episodeId }: EpisodeViewProps) {
  const [status, setStatus] = useState<EpisodeStatus | null>(null);
  const [flags, setFlags] = useState<FlagsPayload | null>(null);
  const [preview, setPreview] = useState<PreviewAvailability>("checking");
  const [error, setError] = useState<{ code: string; detail: string } | null>(null);
  const [notFound, setNotFound] = useState(false);
  const videoRef = useRef<HTMLVideoElement | null>(null);

  const seekTo = useCallback((seconds: number) => {
    const video = videoRef.current;
    if (video !== null) video.currentTime = seconds;
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      try {
        const next = await getEpisodeStatus(episodeId);
        if (cancelled) return;
        setStatus(next);
        setError(null);
        const [flagsResult, previewResult] = await Promise.allSettled([
          getEpisodeFlags(episodeId),
          probeEpisodePreview(episodeId),
        ]);
        if (cancelled) return;
        if (flagsResult.status === "fulfilled") setFlags(flagsResult.value);
        if (previewResult.status === "fulfilled") {
          setPreview(previewResult.value ? "available" : "not_generated");
        }
      } catch (cause) {
        if (cancelled) return;
        if (cause instanceof CockpitApiError) {
          setError({ code: cause.code, detail: cause.detail });
          if (cause.status === 404) {
            setNotFound(true);
            return; // stop polling — the episode does not exist
          }
        } else {
          setError({ code: "unexpected-client-error", detail: String(cause) });
        }
      }
      if (!cancelled) {
        timer = setTimeout(() => void poll(), POLL_INTERVAL_MS);
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [episodeId]);

  return (
    <div>
      <Link href="/new-episode" className="top-link">
        ← 新しいエピソード
      </Link>
      {error !== null ? <ErrorNotice code={error.code} detail={error.detail} /> : null}
      <section className="card">
        <h2 className="card-title">進捗</h2>
        <dl className="status-list">
          <div>
            <dt>エピソード</dt>
            <dd className="mono">{episodeId}</dd>
          </div>
          <div>
            <dt>ステータス</dt>
            <dd data-testid="episode-status">{status !== null ? status.status : "…"}</dd>
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
          <EpisodeProgress status={status} />
        ) : (
          <p className="empty-note">読み込み中…</p>
        )}
      </section>
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
      <BeforeAfterSummary summary={status?.before_after ?? null} />
    </div>
  );
}
