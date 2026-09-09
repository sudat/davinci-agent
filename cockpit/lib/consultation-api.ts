/** Pre-edit directional consultation endpoints (UX 2.5 slice-1): a plain
 *  Japanese consultation BEFORE the plan is committed — message in,
 *  proposals out, per-proposal judgment recorded. The client is typed
 *  strictly against the pinned contract (backend slice:
 *  `services/episode_cockpit/consultation_store.py` + `api_consultation`):
 *  GET lists entries, each POST returns the UPDATED single consultation
 *  entry. Budget lives INSIDE each entry and is CUMULATIVE per episode —
 *  never reset; cost is managed by call count because it cannot be
 *  measured directly (cost_display carries the honest service wording). */

import { CockpitApiError, request, requestWithStatus, type FetchLike } from "@/lib/http";

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
  /** W9 conditional-presentation classification (absent on old backends). */
  presentation_condition?: "normal" | "location_change_only" | "local_exception" | null;
};

export type ConsultationProposal = {
  proposal_id: string;
  title: string;
  summary: string;
  details: ConsultationProposalDetails;
  /** 工程4 optional storyboard panels (absent on old data → render
   *  nothing extra; never claim to be the trial edit). */
  panels?: ConsultationGenerationPanel[] | null;
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
 *  the message POST ("updated consultation"). The judgment POST returns
 *  the whole view instead (see `ConsultationPayload` + slice-2 below). */
export type Consultation = {
  consultation_id: string;
  created_at: string;
  message: string;
  proposals: ConsultationProposal[];
  judgments: ConsultationJudgment[];
  budget: ConsultationBudget;
  /** Slice-2 riders (absent on old backends): this consultation's
   *  policy-outcome verdicts and the episode-level policy/rebuild state
   *  the backend duplicates onto every view. The panel merges the latest
   *  view that carries them. */
  policy_outcomes?: ConsultationPolicyOutcomeEntry[] | null;
  policy?: ConsultationPolicy | null;
  rebuild?: ConsultationRebuild | null;
};

/** Slice-2: the adopted policy carried on the consultation view
 *  (backend `view.policy.adopted`). Text fields are plain Japanese strings
 *  rendered verbatim; `candidate_scenes` and `unconfirmed` are string
 *  arrays (backend `StringSequence`) — the UI never derives numbers from
 *  them. */
export type ConsultationAdoptedDecision = "adopt" | "revise";

export type ConsultationAdoptedPolicy = {
  consultation_id: string;
  judgment_id: string;
  proposal_id: string;
  decision: ConsultationAdoptedDecision;
  scope: ConsultationScope;
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
  note: string;
  /** W9 conditional-presentation classification (absent on old backends). */
  presentation_condition?: "normal" | "location_change_only" | "local_exception" | null;
};

export type ConsultationPolicy = {
  adopted: ConsultationAdoptedPolicy | null;
};

/** Slice-2: the selection-rebuild state carried on the consultation view
 *  (backend `view.rebuild`). Absent (or "none") means no adopted policy
 *  is being reflected — the UI renders nothing, not 不明. */
export type ConsultationRebuildStatus =
  | "none"
  | "requested"
  | "running"
  | "succeeded"
  | "failed";

export type ConsultationRebuild = {
  status: ConsultationRebuildStatus;
  target_version: string | null;
  detail: string | null;
};

/** Slice-2: the outcome of reflecting an adopted policy, rendered as a
 *  consultation journal entry (実装結果 line). On the wire it travels on
 *  EACH consultation view as its top-level `policy_outcomes` array (one
 *  entry per verdict, `kind: "policy_outcome"`), NOT inside the view rows.
 *  Every field may be absent on old data — renderers must read them
 *  defensively (see `outcomeLineOf` in ConsultationEntryList) and never
 *  fabricate values. */
export type ConsultationPolicyOutcomeStatus = "honored" | "connected" | "failed";

/** Slice-2 P1-4 connection evidence (backend `DirectorConnection`).
 *  `null`/absent on old data — renderers fall back honestly, never assume. */
export type ConsultationDirectorConnection = "confirmed" | "not_started" | "unknown";

export type ConsultationPolicyOutcomeEntry = {
  kind: "policy_outcome";
  outcome_id: string;
  consultation_id: string;
  judgment_id: string;
  proposal_id: string | null;
  plan_version: string | null;
  status: ConsultationPolicyOutcomeStatus;
  reasons: string[];
  note: string | null;
  recorded_at: string;
  /** Slice-2 P1-4 evidence fields (backend `ConsultationPolicyOutcomeV1`):
   *  absent/null on old data — renderers read them null-safely. */
  director_connection?: ConsultationDirectorConnection | null;
  realized_checks?: string[] | null;
  unaddressed?: string[] | null;
  unconfirmed?: string[] | null;
  /** Slice-2 W10: set by the backend when an idempotent same-policy return
   *  names a version older than the current head — the row is record-only,
   *  never 現在適用済み. Absent/null on old data — renderers treat absence
   *  as unknown and render no record-only line (never fabricate it). */
  superseded_by_head?: boolean | null;
  /** Slice-2 W10 alias contract: false = this outcome is not the currently
   *  effective version. Absent/null on old data — same tolerance as above. */
  effective?: boolean | null;
  failure_code?: string | null;
  reservation_sequence?: number | null;
  run_id?: string | null;
  commit_event_id?: string | null;
  /** W9 deterministic field→setting rows applied to the derived plan. */
  applied_settings?: { policy_field: string; setting: string; detail: string }[] | null;
};

/** One row of the consultation journal: either a message entry or a
 *  policy-outcome entry. Use `isPolicyOutcomeEntry` to narrow — never
 *  assume a row has `message`/`proposals`/`budget`. */
export type ConsultationEntry = Consultation | ConsultationPolicyOutcomeEntry;

export function isPolicyOutcomeEntry(
  value: unknown,
): value is ConsultationPolicyOutcomeEntry {
  if (typeof value !== "object" || value === null) return false;
  return (value as { kind?: unknown }).kind === "policy_outcome";
}

export type ConsultationPayload = {
  consultations: ConsultationEntry[];
  /** Slice-2 extension, optional for backward compatibility. */
  policy?: ConsultationPolicy | null;
  /** Slice-2 extension, optional for backward compatibility. */
  rebuild?: ConsultationRebuild | null;
  generation_state?: ConsultationGenerationState | null;
};

/** 工程4: optional generation permission + panel scope for
 *  POST /consultation/message. Absent → text-only as today. */
export type ConsultationGenerationPermission = {
  granted: true;
  panels_max: number;
  images_max: number;
};

export type ConsultationGenerationPanelRequest = {
  frame_refs?: string[] | null;
  panel_id?: string | null;
};

export type ConsultationGenerationRequest = {
  permission: ConsultationGenerationPermission;
  panel_request?: ConsultationGenerationPanelRequest | null;
};

export type ConsultationGenerationPanelRole = "a" | "b" | "real_frame";

export type ConsultationGenerationPanelStatus =
  | "requested"
  | "generated"
  | "failed";

export type ConsultationGenerationPanelCaption = {
  subject: string;
  scene_note: string;
  changes: string;
  unconfirmed: string[];
};

export type ConsultationGenerationPanel = {
  panel_id: string;
  role: ConsultationGenerationPanelRole;
  source_frame_ref: string | null;
  image_ref: string | null;
  base_created_at?: string | null;
  caption: ConsultationGenerationPanelCaption;
  status: ConsultationGenerationPanelStatus | null;
};

/** Serve route for one generated panel's bytes (the backend resolves the
 *  episode-relative image_ref; the client never builds file paths). */
export function consultationPanelImageUrl(episodeId: string, panelId: string): string {
  return `/episodes/${encodeURIComponent(episodeId)}/consultation/panels/${encodeURIComponent(panelId)}`;
}

/** POST .../consultation/panels — retry ONE named panel (U33/U37: no
 *  auto-retry, no new-panel mixing; a moved base is a typed 409). */
export async function postConsultationPanelRetry(
  episodeId: string,
  input: {
    consultationId: string;
    proposalId: string;
    baseCreatedAt: string;
    panelId: string;
  },
  fetchImpl: typeof fetch = fetch,
): Promise<Consultation> {
  const response = await fetchImpl(
    `/episodes/${encodeURIComponent(episodeId)}/consultation/panels`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        consultation_id: input.consultationId,
        proposal_id: input.proposalId,
        base_created_at: input.baseCreatedAt,
        permission: { granted: true, panels_max: 1, images_max: 1 },
        retry_panel_ids: [input.panelId],
      }),
    },
  );
  if (!response.ok) {
    throw new CockpitApiError(
      "unexpected-response",
      response.status,
      `panels retry returned HTTP ${String(response.status)}`,
    );
  }
  return (await response.json()) as Consultation;
}

export type ConsultationGenerationState = {
  available: boolean;
  model_verified: boolean | null;
  model_selected: string | null;
  refused_reason: string | null;
};

export type ConsultationMessageInput = {
  message: string;
  generation?: ConsultationGenerationRequest | null;
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
  input: ConsultationMessageInput,
  fetchImpl: FetchLike = fetch,
): Promise<Consultation> {
  const body: Record<string, unknown> = { message: input.message };
  if (input.generation !== undefined && input.generation !== null) {
    body["generation"] = input.generation;
  }
  return request<Consultation>(
    `/episodes/${encodeURIComponent(episodeId)}/consultation/message`,
    { method: "POST", body: JSON.stringify(body) },
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

/** Slice-2 result: the HTTP status decides the UX branch — 202 means an
 *  adopt/revise with non-empty scope scheduled a selection rebuild,
 *  200 means no rebuild (reject/both_wrong/delegate/empty scope). */
export type ConsultationJudgmentResult = {
  status: number;
  view: ConsultationPayload;
};

function isPayloadLike(value: unknown): value is ConsultationPayload {
  if (typeof value !== "object" || value === null) return false;
  return Array.isArray((value as { consultations?: unknown }).consultations);
}

function isSingleEntryLike(value: unknown): value is Consultation {
  if (typeof value !== "object" || value === null) return false;
  const entry = value as { consultation_id?: unknown; message?: unknown };
  return (
    typeof entry.consultation_id === "string" && typeof entry.message === "string"
  );
}

export async function postConsultationJudgment(
  episodeId: string,
  input: ConsultationJudgmentInput,
  fetchImpl: FetchLike = fetch,
): Promise<ConsultationJudgmentResult> {
  const { status, body } = await requestWithStatus<unknown>(
    `/episodes/${encodeURIComponent(episodeId)}/consultation/judgment`,
    { method: "POST", body: JSON.stringify(input) },
    fetchImpl,
  );
  if (isPayloadLike(body)) return { status, view: body };
  if (isSingleEntryLike(body)) return { status, view: { consultations: [body] } };
  throw new CockpitApiError(
    "unexpected-response",
    status,
    "判断の応答の形式が期待と違います",
  );
}
