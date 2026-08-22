"use client";

import type { BeforeAfterSummary as BeforeAfterPayload } from "@/lib/api";

type BeforeAfterSummaryProps = {
  summary: BeforeAfterPayload | null;
};

/** Before-vs-after review summary — rendered only when the comparison
 *  payload is present in the episode detail; hidden otherwise. */
export default function BeforeAfterSummary({ summary }: BeforeAfterSummaryProps) {
  if (summary === null) return null;
  return (
    <section className="card" data-testid="before-after">
      <h2 className="card-title">修正前後の要約</h2>
      <p className="before-after-summary">{summary.summary}</p>
      {summary.items !== undefined && summary.items.length > 0 ? (
        <table className="stage-runs">
          <thead>
            <tr>
              <th>項目</th>
              <th>修正前</th>
              <th>修正後</th>
            </tr>
          </thead>
          <tbody>
            {summary.items.map((item) => (
              <tr key={item.label}>
                <td>{item.label}</td>
                <td>{item.before}</td>
                <td>{item.after}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </section>
  );
}
