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
  /** 工程2P run-scoping: the run that recorded this row. Absent on rows
   *  from backends/runs that never named a run — never guessed. */
  run_id?: string;
  /** Wall-clock stage milestones (ISO-or-absent; absent = unmeasured —
   *  the UI renders them honestly as 取得できません, never fabricated).
   *  first_output_arrived_at is the FIRST succeeded output arrival and is
   *  the only one of the three that counts as 成果進捗. */
  first_started_at?: string;
  last_transition_at?: string;
  first_output_arrived_at?: string;
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

/** One entry of the rebuild chain (予約→起動→成果), readable without
 *  inference: `spawned=false, run_id=null` = a reservation not yet
 *  launched (survives reload); a spawn entry names its run and the plan
 *  version it renders. Mirrors backend RebuildRequestEntry. */
export type RebuildChainEntry = {
  schema_version: string;
  sequence: number;
  stage_hint: string | null;
  marker: string | null;
  spawned: boolean;
  run_id: string | null;
  target_version: string | null;
  reserves_sequence: number | null;
};

/** The latest chain entry when it is still an unspawned reservation
 *  (起動待ち). Present only in that state; null/absent otherwise. */
export type PendingRebuild = {
  sequence: number;
  stage_hint: string | null;
  marker: string | null;
  run_id: string | null;
  target_version: string | null;
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
  /** --- 工程2P run/clock visibility (backend status_view.py) --- */
  /** The run id of the latest SPAWNED rebuild entry (null = none running
   *  for the latest request). Phase derivation scopes to this run ONLY. */
  current_run?: string | null;
  /** The plan version the current run renders (absent when unknown). */
  current_target_version?: string | null;
  pending_rebuild?: PendingRebuild | null;
  rebuild_requests?: RebuildChainEntry[];
  /** Run-scoped last 動作報告 (terminal/failure events included).
   *  null = no report reached us — displayed as 応答不明, never as normal. */
  last_worker_report_at?: string | null;
  last_worker_report_event?: string | null;
  /** True iff an UNCONSUMED proposal set answers the CURRENT plan head. */
  unreviewed_proposal_set?: boolean;
  /** Present only when the CURRENT run has retried (>0); legacy payloads
   *  and retry-free runs omit it. Max is not provided — never displayed. */
  current_run_retry_count?: number;
  /** Wall-clock intake time — the reference for 経過時間. Absent = unmeasured. */
  intake_created_at?: string;
  /** THIS-run first preview output arrival (試し編集完了 labeling only —
   *  never overall completion). Absent = not arrived (or unmeasured). */
  preview_first_arrived_at?: string;
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
