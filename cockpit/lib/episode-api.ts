/** Episode lifecycle endpoints: creation, status, flags, and preview. */

import {
  apiBase,
  CockpitApiError,
  request,
  requestWithStatus,
  type FetchLike,
} from "@/lib/http";

export type EpisodeCreateInput = {
  source_folder: string;
  brief_text: string;
  /** 工程3: explicit channel choice only — omitted unless the operator
   *  picked a channel (never a silent default). */
  channel?: string;
  /** 工程3: pinned style version — omitted unless explicitly chosen
   *  together with channel. */
  style_version?: number;
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
  /** 工程3: the channel style pinned at episode start. Absent/null on old
   *  data — renderers show nothing, never 不明. */
  applied_style?: { channel: string; version: number } | null;
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
  outputId?: OutputId,
): Promise<FlagsPayload> {
  return request<FlagsPayload>(
    `/episodes/${encodeURIComponent(episodeId)}/flags${outputQuery(outputId)}`,
    { method: "GET" },
    fetchImpl,
  );
}

/** 工程5: per-output format independence (backend: services/outputs/geometry.py).
 *
 * Contract (read from services/episode_cockpit/api.py + episode_files.py):
 * - GET /episodes/{id}/outputs → `{outputs: [{output_id, orientation,
 *   width, height}, ...]}` (default [landscape]; absent file = landscape).
 * - POST /episodes/{id}/outputs {output_id} → `{outputs, registered,
 *   idempotent}` (idempotent=true when already registered).
 * - Preview/flags/approvals/rebuild/apply/revert accept the output
 *   dimension (`?output=vertical` on GET/file routes, `output_id` in the
 *   rebuild/apply POST bodies; None/"" = landscape default).
 * - The episode STATUS payload has NO output dimension (verified in
 *   status_view.py::build_status_payload) — stage/version readouts stay
 *   shared; only preview binding / flags / approvals / rebuild chains are
 *   per-output. Unknown binding fields render as 不明, never guessed.
 * - Old backends (no /outputs route) → 404 → null → landscape-only UI
 *   with no output noise.
 */

export type OutputId = "landscape" | "vertical";

export function isOutputId(value: unknown): value is OutputId {
  return value === "landscape" || value === "vertical";
}

/** One registered output (OutputGeometryV1 dump). Only output_id drives
 *  the UI; orientation/size ride along when the backend sends them and
 *  stay undefined otherwise (never fabricated). */
export type OutputGeometry = {
  output_id: string;
  orientation?: string;
  width?: number;
  height?: number;
};

export type OutputsPayload = {
  outputs: OutputGeometry[];
};

export type OutputRegisterResult = {
  outputs: OutputGeometry[];
  registered: string;
  /** True when the output was already registered (backend `idempotent`).
   *  Absent on old payloads → treated as 新規追加 (false). */
  idempotent?: boolean;
};

function parseGeometryEntry(entry: unknown): OutputGeometry | null {
  if (typeof entry !== "object" || entry === null) return null;
  const record = entry as { output_id?: unknown; orientation?: unknown; width?: unknown; height?: unknown };
  if (typeof record.output_id !== "string" || record.output_id === "") return null;
  const geometry: OutputGeometry = { output_id: record.output_id };
  if (typeof record.orientation === "string") geometry.orientation = record.orientation;
  if (typeof record.width === "number") geometry.width = record.width;
  if (typeof record.height === "number") geometry.height = record.height;
  return geometry;
}

function parseOutputsPayload(body: unknown): OutputsPayload | null {
  if (typeof body !== "object" || body === null) return null;
  const outputs = (body as { outputs?: unknown }).outputs;
  if (!Array.isArray(outputs)) return null;
  const geometries: OutputGeometry[] = [];
  for (const entry of outputs) {
    const geometry = parseGeometryEntry(entry);
    if (geometry !== null) geometries.push(geometry);
  }
  if (geometries.length === 0) return null;
  return { outputs: geometries };
}

/** Query suffix for the output dimension: only vertical is ever sent —
 *  landscape/undefined omit the param (backend default), so landscape
 *  URLs stay byte-identical to today. Unknown strings never serialize. */
export function outputQuery(outputId?: string): string {
  return outputId === "vertical" ? "?output=vertical" : "";
}

/** 横版/縦版 display label. Unknown ids render raw (never hidden). */
export function outputLabel(outputId: string): string {
  if (outputId === "landscape") return "横版";
  if (outputId === "vertical") return "縦版";
  return outputId;
}

/** GET /episodes/{id}/outputs. null = unknown (old backend 404, or a
 *  payload without a usable outputs list) → landscape-only UI. */
export async function getEpisodeOutputs(
  episodeId: string,
  fetchImpl: FetchLike = fetch,
): Promise<OutputsPayload | null> {
  let body: unknown;
  try {
    ({ body } = await requestWithStatus<unknown>(
      `/episodes/${encodeURIComponent(episodeId)}/outputs`,
      { method: "GET" },
      fetchImpl,
    ));
  } catch (cause) {
    if (cause instanceof CockpitApiError && cause.status === 404) return null;
    throw cause;
  }
  return parseOutputsPayload(body);
}

/** POST /episodes/{id}/outputs — explicit registration only (the UI never
 *  auto-registers). `.idempotent` echoes the backend flag (absent → false). */
export async function registerOutput(
  episodeId: string,
  outputId: OutputId,
  fetchImpl: FetchLike = fetch,
): Promise<OutputRegisterResult> {
  const body = await request<unknown>(
    `/episodes/${encodeURIComponent(episodeId)}/outputs`,
    { method: "POST", body: JSON.stringify({ output_id: outputId }) },
    fetchImpl,
  );
  const parsed = typeof body === "object" && body !== null ? body : {};
  const record = parsed as { outputs?: unknown; registered?: unknown; idempotent?: unknown };
  return {
    outputs: parseOutputsPayload(parsed)?.outputs ?? [],
    registered: typeof record.registered === "string" ? record.registered : outputId,
    idempotent: record.idempotent === true,
  };
}

export function previewUrl(episodeId: string, outputId?: OutputId): string {
  return `${apiBase()}/episodes/${encodeURIComponent(episodeId)}/preview${outputQuery(outputId)}`;
}

/** The <video> src for one output: content_hash (when probe-verified)
 *  combines with the output param (`?output=vertical&content_hash=…`).
 *  Landscape without a hash stays the legacy fixed URL, byte for byte. */
export function previewVideoSrc(
  episodeId: string,
  outputId: OutputId | undefined,
  contentHash: string | null,
): string {
  const base = previewUrl(episodeId, outputId);
  if (contentHash === null) return base;
  const separator = base.includes("?") ? "&" : "?";
  return `${base}${separator}content_hash=${encodeURIComponent(contentHash)}`;
}

/** One preview probe result. The run/version binding comes ONLY from the
 *  X-Cockpit-Preview-* response headers — a missing header stays null and
 *  downstream renders it as 不明, never guessed. Old backends serve the
 *  video without the headers → available=true with all-null fields. */
export type EpisodePreviewProbe = {
  available: boolean;
  run_id: string | null;
  target_version: string | null;
  content_hash: string | null;
  output_arrived_at: string | null;
};

/** Header value, or null when the header is absent/empty. An empty header
 *  carries no identity — reporting "" as a value would fabricate a binding. */
function headerOrNull(response: Response, name: string): string | null {
  const value = response.headers.get(name);
  return value === null || value === "" ? null : value;
}

/** Probe GET /episodes/{id}/preview with a 2-byte Range request (the
 *  FastAPI route answers 405 to HEAD). 200/206 → the file exists; the
 *  X-Cockpit-Preview-* headers carry the run/version binding (missing
 *  header → null). 404 → not generated (episode existence is already
 *  known from status). ANY other status — and network failure — THROWS:
 *  a probe outage must reach the UI as 確認できません (probe_failed),
 *  never silently as not_generated. */
export async function probeEpisodePreview(
  episodeId: string,
  fetchImpl: FetchLike = fetch,
  outputId?: OutputId,
): Promise<EpisodePreviewProbe> {
  let response: Response;
  try {
    response = await fetchImpl(previewUrl(episodeId, outputId), {
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
  if (response.status === 404) {
    return {
      available: false,
      run_id: null,
      target_version: null,
      content_hash: null,
      output_arrived_at: null,
    };
  }
  if (response.status !== 200 && response.status !== 206) {
    throw new CockpitApiError(
      `http-${response.status}`,
      response.status,
      "試し編集の有無を確認できませんでした",
    );
  }
  return {
    available: true,
    run_id: headerOrNull(response, "X-Cockpit-Preview-Run-Id"),
    target_version: headerOrNull(response, "X-Cockpit-Preview-Target-Version"),
    content_hash: headerOrNull(response, "X-Cockpit-Preview-Content-SHA256"),
    output_arrived_at: headerOrNull(response, "X-Cockpit-Preview-Output-Arrived-At"),
  };
}
