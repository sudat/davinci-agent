/**
 * Typed thin client for the episode cockpit backend (task 44 + 46).
 *
 * Contract notes (read from video-pipeline/services/episode_cockpit/*):
 * - POST /episodes takes a STRICT body: only `source_folder` and
 *   `brief_text` (extra keys are rejected with 422 validation-error), so
 *   this client must never add intake-only UI fields to the request.
 * - Every backend failure is `{error: {code, detail}}`; `detail` may be a
 *   string OR a structured array (validation-error). Both are preserved.
 * - Default base is the SAME-ORIGIN proxy path (/cockpit-api → next.config
 *   rewrites → COCKPIT_API): the browser must not call the backend
 *   cross-origin (localhost:3100 → 127.0.0.1:8765 is CORS-blocked).
 *   Set COCKPIT_API (server-side rewrite target) or
 *   NEXT_PUBLIC_COCKPIT_API (direct browser base) to override.
 * - Task-46 optional fields (eta_minutes / work_units / before_after /
 *   flags.at_seconds) render ONLY when the payload carries them — the UI
 *   never invents precision (PRD 13.2).
 */

import {
  apiBase,
  CockpitApiError,
  request,
  type FetchLike,
} from "@/lib/http";

export { apiBase, CockpitApiError, DEFAULT_API_BASE } from "@/lib/http";

export type EpisodeCreateInput = {
  source_folder: string;
  brief_text: string;
};

export type EpisodeCreateResult = {
  episode_id: string;
  job_id: string;
  status: string;
  brief_status: string;
};

export type StageRun = {
  stage_name: string;
  status: string;
  retry_count: number;
  last_error_code: string | null;
};

/** Completed/remaining work units — present only when the backend counts them. */
export type WorkUnits = {
  completed: number;
  remaining: number;
};

/** Before-vs-after comparison — embedded in the detail payload when the
 *  backend has one; absent otherwise (render-when-present, task 46). */
export type BeforeAfterItem = {
  label: string;
  before: string;
  after: string;
};

export type BeforeAfterSummary = {
  summary: string;
  items?: BeforeAfterItem[];
};

export type EpisodeStatus = {
  episode_id: string;
  job_id: string;
  status: string;
  current_stage: string;
  created_at_seq: number;
  updated_at_seq: number;
  stage_runs: StageRun[];
  /** Measured-history ETA in minutes. ABSENT until the backend measures
   *  historical stage timing — the UI never invents it (PRD 13.2). */
  eta_minutes?: number;
  work_units?: WorkUnits;
  before_after?: BeforeAfterSummary;
};

/** One flagged review item. `at_seconds` is optional: the task-44 flags
 *  payload carries sequence/kind/reason only; timestamps arrive when the
 *  backend surfaces them — until then flags render without seek. */
export type ReviewFlag = {
  sequence: number;
  kind: string;
  reason: string | null;
  at_seconds?: number;
};

export type FlagsPayload = {
  flags: ReviewFlag[];
  not_yet_generated: boolean;
};

export async function createEpisode(
  input: EpisodeCreateInput,
  fetchImpl: FetchLike = fetch,
): Promise<EpisodeCreateResult> {
  return request<EpisodeCreateResult>(
    "/episodes",
    { method: "POST", body: JSON.stringify(input) },
    fetchImpl,
  );
}

export async function getEpisodeStatus(
  episodeId: string,
  fetchImpl: FetchLike = fetch,
): Promise<EpisodeStatus> {
  return request<EpisodeStatus>(
    `/episodes/${encodeURIComponent(episodeId)}`,
    { method: "GET" },
    fetchImpl,
  );
}

export async function getEpisodeFlags(
  episodeId: string,
  fetchImpl: FetchLike = fetch,
): Promise<FlagsPayload> {
  return request<FlagsPayload>(
    `/episodes/${encodeURIComponent(episodeId)}/flags`,
    { method: "GET" },
    fetchImpl,
  );
}

export function previewUrl(episodeId: string): string {
  return `${apiBase()}/episodes/${encodeURIComponent(episodeId)}/preview`;
}

/** Probe GET /episodes/{id}/preview with a 2-byte Range request (the
 *  FastAPI route answers 405 to HEAD): 2xx → file exists; anything else →
 *  not generated (episode existence is already known from status). */
export async function probeEpisodePreview(
  episodeId: string,
  fetchImpl: FetchLike = fetch,
): Promise<boolean> {
  let response: Response;
  try {
    response = await fetchImpl(previewUrl(episodeId), {
      method: "GET",
      headers: { range: "bytes=0-1" },
    });
  } catch (cause) {
    throw new CockpitApiError(
      "network-error",
      0,
      `バックエンドに接続できません: ${String(cause)}`,
    );
  }
  return response.ok;
}

// Reference learning surface (task 50)

export const PREFERENCE_DOMAINS = [
  "story_structure",
  "pacing",
  "color",
  "subtitle",
  "b_roll",
  "framing_graphics",
  "audio",
] as const;

export type PreferenceDomain = (typeof PREFERENCE_DOMAINS)[number];

export type Polarity = "like" | "dislike" | "neutral" | "unspecified";

export type DomainPolarities = Partial<Record<PreferenceDomain, Polarity>>;

export type ParsePreviewDraft = {
  named_domains: PreferenceDomain[];
  domains: DomainPolarities;
  needs_review: boolean;
  rationale: string;
  confidence: number;
  ts_seconds: number | null;
};

export type ParsePreviewInput = {
  text: string;
  ts_seconds?: number;
};

export async function parseReferencePreview(
  input: ParsePreviewInput,
  fetchImpl: FetchLike = fetch,
): Promise<ParsePreviewDraft> {
  return request<ParsePreviewDraft>(
    "/references/parse-preview",
    { method: "POST", body: JSON.stringify(input) },
    fetchImpl,
  );
}

export type ReferenceRegisterInput = {
  path: string;
  source_id?: string;
};

export type ReferenceRegisterResult = {
  source_id: string;
  sha256: string;
  library_version: number;
};

export async function registerReference(
  input: ReferenceRegisterInput,
  fetchImpl: FetchLike = fetch,
): Promise<ReferenceRegisterResult> {
  return request<ReferenceRegisterResult>(
    "/references",
    { method: "POST", body: JSON.stringify(input) },
    fetchImpl,
  );
}

// Reference library listing (task 51: GET /references over the persisted file)

export type LibraryReference = {
  source_id: string;
  kind: string;
  location: string;
  sha256: string;
  created_at: string | null;
};

export type ReferencesListResult = {
  available: boolean;
  references: LibraryReference[];
};

export async function listReferences(
  fetchImpl: FetchLike = fetch,
): Promise<ReferencesListResult> {
  return request<ReferencesListResult>("/references", { method: "GET" }, fetchImpl);
}

// Review chat -> structured command -> partial rebuild (task 47 + 51 wiring)

export type ReviewCommandDraft = {
  schema_version: string;
  command_id: string;
  command_kind: string | null;
  text: string;
  target_seconds: number | null;
  seconds_delta: number | null;
  scope: "episode" | "channel";
  needs_confirmation: boolean;
  confirmation_reason: string | null;
};

export type ReviewChatResult = {
  received: boolean;
  sequence: number;
  draft: ReviewCommandDraft;
};

export type ReviewChatInput = {
  text: string;
  at_seconds?: number | null;
};

export async function postReviewChat(
  episodeId: string,
  input: ReviewChatInput,
  fetchImpl: FetchLike = fetch,
): Promise<ReviewChatResult> {
  return request<ReviewChatResult>(
    `/episodes/${encodeURIComponent(episodeId)}/review-chat`,
    { method: "POST", body: JSON.stringify(input) },
    fetchImpl,
  );
}

export type AppliedCommand = {
  schema_version: string;
  command_id: string;
  command_kind: string;
  affected_domain: string;
  event_id: string | null;
  base_plan_version: string | null;
  result_plan_version: string | null;
  deferred: boolean;
  reason: string | null;
  target_seconds: number | null;
  seconds_delta: number | null;
};

export type RebuildPlan = {
  schema_version: string;
  command_id: string;
  command_kind: string;
  affected_domain: string;
  stages: string[];
  excluded_stages: string[];
};

export type ReviewApplyResult = {
  applied: AppliedCommand;
  rebuild: RebuildPlan;
};

export async function applyReviewCommand(
  episodeId: string,
  input: ReviewChatInput,
  fetchImpl: FetchLike = fetch,
): Promise<ReviewApplyResult> {
  return request<ReviewApplyResult>(
    `/episodes/${encodeURIComponent(episodeId)}/review-chat/apply`,
    { method: "POST", body: JSON.stringify(input) },
    fetchImpl,
  );
}

export type RebuildResult = {
  stage_hint: string | null;
  scheduled: boolean;
  note: string;
  applied_command?: string;
  rebuild_stages?: string[];
};

export async function postRebuild(
  episodeId: string,
  input: { applied_command?: string; stage_hint?: string },
  fetchImpl: FetchLike = fetch,
): Promise<RebuildResult> {
  return request<RebuildResult>(
    `/episodes/${encodeURIComponent(episodeId)}/rebuild`,
    { method: "POST", body: JSON.stringify(input) },
    fetchImpl,
  );
}

// Approval sessions (task 49 bundling + task 51 wiring)

export type PendingApprovalItem = {
  record_id: string;
  purpose: string;
  target_hash: string;
};

export type ApprovalSession = {
  session_key: string;
  kind: "normal" | "exception";
  purposes: string[];
  items: PendingApprovalItem[];
  explanation: string | null;
};

export type DecidedApproval = {
  record_id: string;
  purpose: string;
  decision: string;
};

export type ApprovalSessionsPayload = {
  available: boolean;
  sessions: ApprovalSession[];
  blocking_session_count: number;
  decided: DecidedApproval[];
};

export async function getApprovalSessions(
  episodeId: string,
  fetchImpl: FetchLike = fetch,
): Promise<ApprovalSessionsPayload> {
  return request<ApprovalSessionsPayload>(
    `/episodes/${encodeURIComponent(episodeId)}/approval-sessions`,
    { method: "GET" },
    fetchImpl,
  );
}

export type ApprovalExecuteInput = {
  decision: "approve" | "reject";
  actor_id: string;
};

export type ApprovalExecuteResult = {
  record_id: string;
  decision: string;
  superseded_record_id: string | null;
  runner_class: string;
  fixture_only: boolean;
};

export async function executeApproval(
  episodeId: string,
  approvalId: string,
  input: ApprovalExecuteInput,
  fetchImpl: FetchLike = fetch,
): Promise<ApprovalExecuteResult> {
  return request<ApprovalExecuteResult>(
    `/episodes/${encodeURIComponent(episodeId)}/approvals/${encodeURIComponent(
      approvalId,
    )}`,
    { method: "POST", body: JSON.stringify(input) },
    fetchImpl,
  );
}
