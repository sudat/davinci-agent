/**
 * Japanese rendering of the backend's deterministic confirmation reasons
 * (the API contract keeps English codes stable; the operator surface maps
 * them). Unknown reasons render verbatim — never silently replaced.
 */

const CONFIRMATION_REASON_JA: Record<string, string> = {
  "no known command kind matched the message; restate the correction":
    "どの修正にも当てはまりませんでした。言い換えて伝えてください",
  "no target timestamp: set the player position or name an explicit time in the message":
    "対象の時刻がわかりませんでした。動画の位置を合わせてから送るか、時刻（例: 2:05）を書き加えてください",
  "llm-interpretation: no explicit target timestamp for a positional command; confirm the restated correction":
    "対象時刻が明示されていません。この解釈で合っているか確認してください",
};

export function confirmationReasonJa(reason: string | null): string | null {
  if (reason === null) return null;
  return CONFIRMATION_REASON_JA[reason] ?? reason;
}
