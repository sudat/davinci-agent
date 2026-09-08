"use client";

import type { ConsultationProposal } from "@/lib/api";

function DetailText({ label, value }: { label: string; value: string }) {
  if (value === "") return null;
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function DetailList({ label, items }: { label: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        <ul className="list-plain">
          {items.map((item, index) => (
            <li key={index}>{item}</li>
          ))}
        </ul>
      </dd>
    </div>
  );
}

/** One proposal: the short summary is the primary display, the fixed
 *  ten-field body stays collapsed until the operator opens <details>. */
export default function ConsultationProposalCard({
  proposal,
}: {
  proposal: ConsultationProposal;
}) {
  const details = proposal.details;
  return (
    <div data-testid="consultation-proposal" data-proposal-id={proposal.proposal_id}>
      <h3 className="section-title" data-testid="consultation-proposal-title">
        {proposal.title}
      </h3>
      <p data-testid="consultation-proposal-summary">{proposal.summary}</p>
      <details data-testid="consultation-proposal-details">
        <summary>詳しい内容を見る</summary>
        <dl className="status-list">
          <div>
            <dt>視聴者に伝えたいこと</dt>
            <dd>{details.audience_message}</dd>
          </div>
          <div>
            <dt>構成</dt>
            <dd>{details.structure}</dd>
          </div>
          <div>
            <dt>尺のめやす</dt>
            <dd data-testid="consultation-proposal-duration">{details.duration_estimate}</dd>
          </div>
          <div>
            <dt>字幕の方針</dt>
            <dd>{details.subtitle_policy}</dd>
          </div>
          <div>
            <dt>音声の方針</dt>
            <dd>{details.audio_policy}</dd>
          </div>
          <div>
            <dt>テンポの方針</dt>
            <dd>{details.tempo_policy}</dd>
          </div>
          <DetailList label="候補シーン" items={details.candidate_scenes} />
          <DetailText label="参考にしたいつもの作りとの対応" value={details.reference_mapping} />
          <DetailText label="使わなかった候補と理由" value={details.unused_reasons} />
          <DetailList label="確認できていないこと" items={details.unconfirmed} />
        </dl>
      </details>
    </div>
  );
}
