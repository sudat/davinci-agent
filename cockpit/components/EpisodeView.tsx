"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  getEpisodeStatus,
  CockpitApiError,
  type EpisodeStatus,
} from "@/lib/api";
import ErrorNotice from "@/components/ErrorNotice";

const POLL_INTERVAL_MS = 2000;

type EpisodeViewProps = {
  episodeId: string;
};

/**
 * Simple stage/progress readout over GET /episodes/{id} with polling.
 * No ETA — historical stage timing is not measured yet (PRD 13.2: never
 * invent precision). Later tasks (46+) extend this view with the preview
 * player, flags and review chat.
 */
export default function EpisodeView({ episodeId }: EpisodeViewProps) {
  const [status, setStatus] = useState<EpisodeStatus | null>(null);
  const [error, setError] = useState<{ code: string; detail: string } | null>(
    null,
  );
  const [notFound, setNotFound] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      try {
        const next = await getEpisodeStatus(episodeId);
        if (cancelled) return;
        setStatus(next);
        setError(null);
      } catch (cause) {
        if (cancelled) return;
        if (cause instanceof CockpitApiError) {
          setError({ code: cause.code, detail: cause.detail });
          if (cause.status === 404) {
            setNotFound(true);
            return; // stop polling — the episode does not exist
          }
        } else {
          setError({
            code: "unexpected-client-error",
            detail: String(cause),
          });
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
        {status !== null && status.stage_runs.length > 0 ? (
          <table className="stage-runs">
            <thead>
              <tr>
                <th>ステージ</th>
                <th>状態</th>
                <th>リトライ</th>
                <th>最終エラー</th>
              </tr>
            </thead>
            <tbody>
              {status.stage_runs.map((run) => (
                <tr key={run.stage_name}>
                  <td>{run.stage_name}</td>
                  <td>{run.status}</td>
                  <td>{run.retry_count}</td>
                  <td>{run.last_error_code ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="empty-note">
            {notFound
              ? "このエピソードは見つかりません。"
              : "ステージ実行はまだありません。2秒ごとに自動更新します。"}
          </p>
        )}
      </section>
    </div>
  );
}
