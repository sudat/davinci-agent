/** Channel style endpoints (UX-redesign 工程3): explicit save/restore of a
 *  channel's style versions. Fixed contract (backend builds in parallel):
 *  - GET /channels → {channels: [{channel_id}]}
 *  - GET /channels/{id}/style → {channel_id, versions: [{version, name,
 *    saved_at}], current: number | null} (+ ?version=N → full entry)
 *  - POST /channels/{id}/style/save {name, ...summary text fields, source?}
 *    → 201 {version} | 200 {version, idempotent: true}
 *  - POST /channels/{id}/style/restore {target_version} → 201 {version}
 *
 *  Every parser is null-safe: absent fields are tolerated (empty versions,
 *  null current, "" names) and a missing numeric version on a write
 *  response is a typed unexpected-response — never guessed. All style
 *  state derives from these server polls; this module never touches
 *  sessionStorage. */

import {
  CockpitApiError,
  request,
  requestWithStatus,
  type FetchLike,
} from "@/lib/http";

export type ChannelEntry = {
  channel_id: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

/** Parse GET /channels defensively: entries without a channel_id are
 *  skipped, a missing/non-array channels field means "no channels". */
export function parseChannelList(body: unknown): ChannelEntry[] {
  if (!isRecord(body)) return [];
  const channels: unknown = body.channels;
  if (!Array.isArray(channels)) return [];
  const out: ChannelEntry[] = [];
  for (const item of channels) {
    const id = isRecord(item) ? nonEmptyString(item.channel_id) : null;
    if (id !== null) out.push({ channel_id: id });
  }
  return out;
}

export async function getChannels(
  fetchImpl: FetchLike = fetch,
): Promise<ChannelEntry[]> {
  const body = await request<unknown>(
    "/channels",
    { method: "GET" },
    fetchImpl,
  );
  return parseChannelList(body);
}

export type ChannelStyleVersion = {
  version: number;
  name: string;
  saved_at: string;
};

export type ChannelStyle = {
  channel_id: string;
  versions: ChannelStyleVersion[];
  current: number | null;
};

function parseStyleVersion(item: unknown): ChannelStyleVersion | null {
  if (!isRecord(item)) return null;
  if (typeof item.version !== "number") return null;
  return {
    version: item.version,
    name: typeof item.name === "string" ? item.name : "",
    saved_at: typeof item.saved_at === "string" ? item.saved_at : "",
  };
}

/** Parse GET style defensively: versions defaults to [], current defaults
 *  to null, channel_id defaults to the requested id (the wire may carry
 *  it; a mismatch is not resolved here — callers display what was asked). */
export function parseChannelStyle(body: unknown, channelId: string): ChannelStyle {
  if (!isRecord(body)) {
    return { channel_id: channelId, versions: [], current: null };
  }
  const versions: ChannelStyleVersion[] = [];
  if (Array.isArray(body.versions)) {
    for (const item of body.versions) {
      const parsed = parseStyleVersion(item);
      if (parsed !== null) versions.push(parsed);
    }
  }
  return {
    channel_id: nonEmptyString(body.channel_id) ?? channelId,
    versions,
    current: typeof body.current === "number" ? body.current : null,
  };
}

export async function getChannelStyle(
  channelId: string,
  options?: { version?: number; fetchImpl?: FetchLike },
): Promise<ChannelStyle> {
  const fetchImpl: FetchLike = options?.fetchImpl ?? fetch;
  const query =
    options?.version !== undefined
      ? `?version=${encodeURIComponent(String(options.version))}`
      : "";
  const body = await request<unknown>(
    `/channels/${encodeURIComponent(channelId)}/style${query}`,
    { method: "GET" },
    fetchImpl,
  );
  return parseChannelStyle(body, channelId);
}

/** Summary text fields carried from an adopted consultation policy into an
 *  explicit style save. All optional except name: the server owns which
 *  fields a version needs; the client sends what the policy carried. */
export type ChannelStyleSaveInput = {
  name: string;
  audience_message?: string;
  structure?: string;
  duration_estimate?: string;
  candidate_scenes?: string[];
  subtitle_policy?: string;
  audio_policy?: string;
  tempo_policy?: string;
  reference_mapping?: string;
  unused_reasons?: string;
  unconfirmed?: string[];
  note?: string;
  source?: {
    episode_id: string;
    judgment_id: string;
    proposal_id: string | null;
  };
};

export type ChannelStyleSaveResult = {
  version: number;
  /** True on the 200 already-latest path (or an explicit server flag);
   *  false on a fresh 201. */
  idempotent: boolean;
};

function parseVersion(body: unknown): number | null {
  return isRecord(body) && typeof body.version === "number"
    ? body.version
    : null;
}

/** Explicit save only — no caller may auto-save. 201 = fresh version,
 *  200 (+ idempotent flag) = already the latest. */
export async function saveChannelStyle(
  channelId: string,
  input: ChannelStyleSaveInput,
  fetchImpl: FetchLike = fetch,
): Promise<ChannelStyleSaveResult> {
  const { status, body } = await requestWithStatus<unknown>(
    `/channels/${encodeURIComponent(channelId)}/style/save`,
    { method: "POST", body: JSON.stringify(input) },
    fetchImpl,
  );
  const version = parseVersion(body);
  if (version === null) {
    throw new CockpitApiError(
      "unexpected-response",
      status,
      "スタイル保存の応答に版がありません",
    );
  }
  const flag: unknown = isRecord(body) ? body.idempotent : undefined;
  return {
    version,
    idempotent: typeof flag === "boolean" ? flag : status === 200,
  };
}

/** Operator-only restore: re-saves target content as a NEW forward version
 *  (the server never rewrites history). Returns the new version. */
export async function restoreChannelStyle(
  channelId: string,
  targetVersion: number,
  fetchImpl: FetchLike = fetch,
): Promise<{ version: number }> {
  const { status, body } = await requestWithStatus<unknown>(
    `/channels/${encodeURIComponent(channelId)}/style/restore`,
    { method: "POST", body: JSON.stringify({ target_version: targetVersion }) },
    fetchImpl,
  );
  const version = parseVersion(body);
  if (version === null) {
    throw new CockpitApiError(
      "unexpected-response",
      status,
      "スタイル復元の応答に版がありません",
    );
  }
  return { version };
}
