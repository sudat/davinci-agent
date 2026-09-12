/**
 * Episode flow states ②–⑤ (codex 2026-09-12 reorganization): the episode
 * page shows ONE state at a time — one heading, one short explanation, one
 * primary action. Derived client-side only, from the already-polled episode
 * status plus the consultation view. No new API, no new routes.
 *
 * - compose: nothing sent yet — the wish form is the primary action.
 * - waiting-proposals: a message was sent, no proposal response yet.
 * - choose: at least one proposal response exists, none adopted yet.
 * - waiting-trial: a direction was adopted, no published trial video yet.
 * - review: a published trial video (or full authorization) is on screen.
 */

export type FlowState =
  | "compose"
  | "waiting-proposals"
  | "choose"
  | "waiting-trial"
  | "review";

export type FlowStateInput = {
  loaded: boolean;
  hasConsultation: boolean;
  hasProposal: boolean;
  adopted: boolean;
  hasPublishedSample: boolean;
  fullAuthorized: boolean;
};

export function deriveFlowState(input: FlowStateInput): FlowState {
  if (!input.loaded) return "compose";
  if (input.fullAuthorized || input.hasPublishedSample) return "review";
  if (input.adopted) return "waiting-trial";
  if (input.hasProposal) return "choose";
  if (input.hasConsultation) return "waiting-proposals";
  return "compose";
}
