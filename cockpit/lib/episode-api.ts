/** Episode lifecycle endpoints: creation, status, flags, and preview. */

import {
  apiBase,
  CockpitApiError,
  request,
  type FetchLike,
} from "@/lib/http";

export type EpisodeCreateInput = {
  source_folder: string;
  brief_text: string;
};

export type EpisodeCreateResult = {
  episode_id: string;
  job_id: string;
  status: string;
  brief_status: string;
  /** Task-7 additive field: "started" once the detached pipeline runner is
   *  spawned. Render-when-present; absent on older backend responses. */
  pipeline?: string;
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
