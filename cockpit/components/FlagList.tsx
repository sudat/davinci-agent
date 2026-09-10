"use client";

import type { ReviewFlag } from "@/lib/api";

type FlagListProps = {
  flags: ReviewFlag[];
  notYetGenerated: boolean;
  canSeek: boolean;
  onSeek: (seconds: number) => void;
};

function formatSeconds(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

function FlagItem({
  flag,
  canSeek,
  onSeek,
}: {
  flag: ReviewFlag;
  canSeek: boolean;
  onSeek: (seconds: number) => void;
}) {
  const ts = flag.at_seconds;
  return (
    <li data-testid="flag-item">
      {flag.reason !== null ? <p className="flag-reason">{flag.reason}</p> : null}
      <details data-testid="flag-record">
        <summary>詳しい記録</summary>
        <div className="flag-meta">
          #{flag.sequence} · {flag.kind}
        </div>
      </details>
      {typeof ts === "number" ? (
        <div className="flag-jump-row">
          <button
            type="button"
            className="btn-small flag-jump"
            data-testid="flag-jump"
            disabled={!canSeek}
            onClick={() => onSeek(ts)}
          >
            ▶ {formatSeconds(ts)} に移動
          </button>
          {canSeek ? null : (
            <p data-testid="flag-jump-note" className="field-hint">
              プレビュー未生成のため、この位置へジャンプできません。
            </p>
          )}
        </div>
      ) : null}
    </li>
  );
}

/**
 * Flagged review items with timestamp jump. Flags without a timestamp
 * (the current task-44 payload shape) render information-only.
 */
export default function FlagList({
  flags,
  notYetGenerated,
  canSeek,
  onSeek,
}: FlagListProps) {
  if (notYetGenerated) {
    return (
      <p data-testid="flags-empty" className="empty-note">
        レビューflagはまだ生成されていません。
      </p>
    );
  }
  if (flags.length === 0) {
    return (
      <p data-testid="flags-empty" className="empty-note">
        表示するレビューflagはありません。
      </p>
    );
  }
  return (
    <ul className="flag-list">
      {flags.map((flag) => (
        <FlagItem key={flag.sequence} flag={flag} canSeek={canSeek} onSeek={onSeek} />
      ))}
    </ul>
  );
}
