/** Pre-edit directional consultation endpoints (UX 2.5 slice-1): a plain
 *  Japanese consultation BEFORE the plan is committed — message in,
 *  proposals out, per-proposal judgment recorded. The client is typed
 *  strictly against the pinned contract (backend slice:
 *  `services/episode_cockpit/consultation_store.py` + `api_consultation`):
 *  GET lists entries, each POST returns the UPDATED single consultation
 *  entry. Budget lives INSIDE each entry and is CUMULATIVE per episode —
 *  never reset; cost is managed by call count because it cannot be
 *  measured directly (cost_display carries the honest service wording). */

import { request, type FetchLike } from "@/lib/http";

/** The closed decision set of a judgment (backend `ConsultationDecision`).
 *  `both_wrong` mirrors the review chat 「両方違う」 vocabulary as a
 *  whole-set judgment; `delegate` is いつもの方向性におまかせ. */
export type ConsultationDecision =
  | "adopt"
  | "revise"
  | "reject"
  | "both_wrong"
  | "delegate";

export type ConsultationScope = {
  composition: boolean;
  appearance: boolean;
  audio: boolean;
};

/** The fixed proposal body. Display-text fields arrive as plain Japanese
 *  text; the UI renders them verbatim and never derives numbers from them
 *  (尺のめやす・処理秒は表示値そのまま). */
export type ConsultationProposalDetails = {
  audience_message: string;
  structure: string;
  duration_estimate: string;
  candidate_scenes: string[];
  subtitle_policy: string;
  audio_policy: string;
  tempo_policy: string;
  reference_mapping: string;
  unused_reasons: string;
  unconfirmed: string[];
};

export type ConsultationProposal = {
  proposal_id: string;
  title: string;
  summary: string;
  details: ConsultationProposalDetails;
};

export type ConsultationJudgment = {
  judgment_id: string;
  /** `null` judges the consultation as a whole (both_wrong). */
  proposal_id: string | null;
  decision: ConsultationDecision;
  scope: ConsultationScope;
  note: string | null;
  created_at: string;
};

/** Budget readout: CUMULATIVE per episode, NEVER reset by a retry or
 *  regeneration. Costs are not directly measurable — call counts are the
 *  honest proxy (`cost_display` keeps the service-side wording). */
export type ConsultationBudget = {
  llm_calls_used: number;
  llm_calls_limit: number;
  intervals_used: number;
  intervals_limit: number;
  wall_seconds_used: number;
  wall_seconds_limit: number;
  cost_display: string;
};

/** One consultation entry (= one message + its proposal set + judgments +
 *  the episode-cumulative budget snapshot). This is ALSO the response of
 *  both POSTs ("updated consultation"). */
export type Consultation = {
  consultation_id: string;
  created_at: string;
  message: string;
  proposals: ConsultationProposal[];
  judgments: ConsultationJudgment[];
  budget: ConsultationBudget;
};

export type ConsultationPayload = {
  consultations: Consultation[];
};

export async function getConsultation(
  episodeId: string,
  fetchImpl: FetchLike = fetch,
): Promise<ConsultationPayload> {
  return request<ConsultationPayload>(
    `/episodes/${encodeURIComponent(episodeId)}/consultation`,
    { method: "GET" },
    fetchImpl,
  );
}

export async function postConsultationMessage(
  episodeId: string,
  input: { message: string },
  fetchImpl: FetchLike = fetch,
): Promise<Consultation> {
  return request<Consultation>(
    `/episodes/${encodeURIComponent(episodeId)}/consultation/message`,
    { method: "POST", body: JSON.stringify(input) },
    fetchImpl,
  );
}

export type ConsultationJudgmentInput = {
  consultation_id: string;
  /** `null` judges the whole consultation (both_wrong); otherwise the
   *  per-proposal judgment the form was rendered for. */
  proposal_id: string | null;
  decision: ConsultationDecision;
  scope: ConsultationScope;
  /** Optional free text — empty input is sent as null. */
  note: string | null;
};

export async function postConsultationJudgment(
  episodeId: string,
  input: ConsultationJudgmentInput,
  fetchImpl: FetchLike = fetch,
): Promise<Consultation> {
  return request<Consultation>(
    `/episodes/${encodeURIComponent(episodeId)}/consultation/judgment`,
    { method: "POST", body: JSON.stringify(input) },
    fetchImpl,
  );
}
