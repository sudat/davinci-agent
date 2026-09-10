"use client";

import type {
  ConsultationGenerationPanel,
  ConsultationProposal,
} from "@/lib/api";
import { consultationPanelImageUrl } from "@/lib/consultation-api";

export type ConsultationPanelChoice = "keep" | "drop" | "retry";

export function panelNoteLine(panelId: string, choice: ConsultationPanelChoice): string {
  return choice === "keep"
    ? `パネル${panelId}はこのまま採用の希望`
    : `パネル${panelId}は不要の希望`;
}

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

function textOf(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function listOf(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && item !== "");
}

function panelsOf(proposal: ConsultationProposal): ConsultationGenerationPanel[] {
  const panels: unknown = proposal.panels;
  if (!Array.isArray(panels)) return [];
  return panels.filter(
    (panel): panel is ConsultationGenerationPanel =>
      typeof panel === "object" &&
      panel !== null &&
      typeof (panel as { panel_id?: unknown }).panel_id === "string",
  );
}

function StoryboardPanel({
  panel,
  choice,
  onAction,
  episodeId,
}: {
  panel: ConsultationGenerationPanel;
  choice: ConsultationPanelChoice | null;
  onAction: (panelId: string, action: ConsultationPanelChoice) => void;
  episodeId: string;
}) {
  const role: unknown = panel.role;
  const isRealFrame = role === "real_frame";
  const status: unknown = panel.status;
  const imageRef = textOf(panel.image_ref);
  const caption: unknown = panel.caption;
  const captionRecord =
    typeof caption === "object" && caption !== null
      ? (caption as Record<string, unknown>)
      : {};
  const unconfirmed = listOf(captionRecord["unconfirmed"]);
  const testid = `panel-${panel.panel_id}`;
  return (
    <figure data-testid={testid} data-panel-id={panel.panel_id}>
      <figcaption data-testid={`${testid}-kind`}>
        {isRealFrame ? "撮影素材" : "生成案"}
      </figcaption>
      {status === "generated" && imageRef !== "" ? (
        <img
          src={consultationPanelImageUrl(episodeId, panel.panel_id)}
          alt=""
          data-testid={`${testid}-image`}
        />
      ) : status === "failed" ? (
        <p data-testid={`${testid}-placeholder`}>
          作れませんでした（再依頼はこの1枚だけ）
        </p>
      ) : (
        <p data-testid={`${testid}-placeholder`}>準備中</p>
      )}
      <dl className="status-list">
        <DetailText label="写っているもの" value={textOf(captionRecord["subject"])} />
        <DetailText label="場面の説明" value={textOf(captionRecord["scene_note"])} />
        <DetailText label="変えたところ" value={textOf(captionRecord["changes"])} />
        <DetailList label="確認できていないこと" items={unconfirmed} />
      </dl>
      {choice !== null ? (
        <p className="field-hint" data-testid={`${testid}-choice`}>
          {choice === "keep" ? "このまま採用の希望を記録します" : "不要の希望を記録します"}
        </p>
      ) : null}
      <div className="actions">
        <button
          type="button"
          className="btn-small"
          onClick={() => onAction(panel.panel_id, "keep")}
          data-testid={`${testid}-keep`}
        >
          このまま（採用）
        </button>
        <button
          type="button"
          className="btn-small"
          onClick={() => onAction(panel.panel_id, "retry")}
          data-testid={`${testid}-retry`}
        >
          直す（この1枚だけ再依頼）
        </button>
        <button
          type="button"
          className="btn-small"
          onClick={() => onAction(panel.panel_id, "drop")}
          data-testid={`${testid}-drop`}
        >
          不要
        </button>
      </div>
    </figure>
  );
}

/** One proposal: the short summary is the primary display, the fixed
 *  ten-field body stays collapsed until the operator opens <details>.
 *  When the backend attaches storyboard panels they render as a row
 *  beneath the summary — each panel carries its 区分 badge and verbatim
 *  caption, and never claims to be the trial edit. */
export default function ConsultationProposalCard({
  proposal,
  panelChoices,
  onPanelAction,
  episodeId,
  onQuickAdopt,
  quickAdoptBusy = false,
}: {
  proposal: ConsultationProposal;
  panelChoices?: Record<string, ConsultationPanelChoice | null>;
  onPanelAction?: (panelId: string, action: ConsultationPanelChoice) => void;
  episodeId: string;
  /** 方向画面のカード主操作。この方向で試す（採用判断をそのまま送る）。 */
  onQuickAdopt?: () => void;
  quickAdoptBusy?: boolean;
}) {
  const details = proposal.details;
  const panels = panelsOf(proposal);
  return (
    <div data-testid="consultation-proposal" data-proposal-id={proposal.proposal_id}>
      <h3 className="section-title" data-testid="consultation-proposal-title">
        AIの提案: {proposal.title}
      </h3>
      <p data-testid="consultation-proposal-summary">{proposal.summary}</p>
      {onQuickAdopt !== undefined ? (
        <div className="actions">
          <button
            type="button"
            className="btn-small"
            onClick={onQuickAdopt}
            disabled={quickAdoptBusy}
            data-testid="proposal-quick-adopt"
          >
            {quickAdoptBusy ? "送信中…" : "この方向で試す"}
          </button>
        </div>
      ) : null}
      {panels.length > 0 ? (
        <div data-testid="consultation-storyboard">
          <p className="field-hint">
            生成案は見本であり、試し編集ではありません
          </p>
          <div className="storyboard-row">
            {panels.map((panel) => (
              <StoryboardPanel
                key={panel.panel_id}
                panel={panel}
                choice={panelChoices?.[panel.panel_id] ?? null}
                onAction={(panelId, action) => onPanelAction?.(panelId, action)}
                episodeId={episodeId}
              />
            ))}
          </div>
        </div>
      ) : null}
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
