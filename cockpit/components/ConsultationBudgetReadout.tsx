"use client";

import type { ConsultationBudget, ConsultationPayload } from "@/lib/api";

/** The most recent budget snapshot. Journal order is not depended on —
 *  the newest `created_at` wins; null while no consultation exists. */
export function latestBudgetOf(payload: ConsultationPayload | null): ConsultationBudget | null {
  if (payload === null) return null;
  let latest: ConsultationBudget | null = null;
  let latestAt = -Infinity;
  for (const entry of payload.consultations) {
    const at = Date.parse(entry.created_at);
    if (
      !Number.isNaN(at) &&
      at >= latestAt &&
      typeof entry.budget === "object" &&
      entry.budget !== null
    ) {
      latestAt = at;
      latest = entry.budget;
    }
  }
  return latest;
}

/** Budget readout: cumulative per episode, managed by LLM call count
 *  because cost cannot be measured directly, NEVER reset. `degraded`
 *  adds the explicit notice while the service is in the text-only state. */
export default function ConsultationBudgetReadout({
  budget,
  degraded,
}: {
  budget: ConsultationBudget;
  degraded: boolean;
}) {
  return (
    <div data-testid="consultation-budget">
      <p className="field-hint">AIの利用状況（このエピソードの累計）</p>
      <dl className="status-list">
        <div>
          <dt>AIの呼び出し回数</dt>
          <dd data-testid="consultation-budget-llm-calls">
            {budget.llm_calls_used} / {budget.llm_calls_limit}回
          </dd>
        </div>
        <div>
          <dt>区間の使用数</dt>
          <dd data-testid="consultation-budget-intervals">
            {budget.intervals_used} / {budget.intervals_limit}
          </dd>
        </div>
        <div>
          <dt>処理時間の合計</dt>
          <dd data-testid="consultation-budget-wall-seconds">
            {budget.wall_seconds_used} / {budget.wall_seconds_limit}秒
          </dd>
        </div>
      </dl>
      <p className="field-hint" data-testid="consultation-budget-policy">
        費用は直接計測できないため、AIの呼び出し回数で管理しています。この数はリセットされません。
      </p>
      {budget.cost_display !== "" ? (
        <p className="field-hint" data-testid="consultation-budget-cost-display">
          {budget.cost_display}
        </p>
      ) : null}
      {degraded ? (
        <p className="field-hint" data-testid="consultation-budget-degrade">
          回数上限のため、これからの相談は提案なし・文章のみになります
        </p>
      ) : null}
    </div>
  );
}
