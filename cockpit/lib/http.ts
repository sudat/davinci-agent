/**
 * Transport core for the cockpit API client: base-URL resolution, the
 * typed error envelope, and the shared JSON `request()` helper.
 * Endpoint functions and payload types live in `lib/api.ts`.
 */

export const DEFAULT_API_BASE = "/cockpit-api";

/**
 * Sanitize an env value: treat missing, empty, and the literal strings
 * "undefined"/"null" as unset. (Some test transforms inline
 * `process.env.<KEY>` as the STRING "undefined" in src modules, so a bare
 * trim/empty check is not enough.)
 */
function sanitizeEnv(value: string | undefined): string {
  if (value === undefined) return "";
  const trimmed = value.trim();
  return trimmed === "" || trimmed === "undefined" || trimmed === "null"
    ? ""
    : trimmed;
}

export function apiBase(): string {
  return (
    sanitizeEnv(process.env.NEXT_PUBLIC_COCKPIT_API) ||
    sanitizeEnv(process.env.COCKPIT_API) ||
    DEFAULT_API_BASE
  );
}

export class CockpitApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly detail: string;

  constructor(code: string, status: number, detail: string) {
    super(`${code} (HTTP ${status}): ${detail}`);
    this.name = "CockpitApiError";
    this.code = code;
    this.status = status;
    this.detail = detail;
  }
}

export type FetchLike = typeof fetch;

/**
 * Convert a caught error into the UI error-state shape `{code, detail}`:
 * a CockpitApiError keeps its machine code/detail; anything else becomes
 * `unexpected-client-error` with `String(cause)`.
 */
export function apiFailure(cause: unknown): { code: string; detail: string } {
  if (cause instanceof CockpitApiError) {
    return { code: cause.code, detail: cause.detail };
  }
  return { code: "unexpected-client-error", detail: String(cause) };
}

function stringifyDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (detail === undefined || detail === null) return "";
  try {
    return JSON.stringify(detail);
  } catch {
    return String(detail);
  }
}

function errorFromEnvelope(body: unknown, status: number): CockpitApiError | null {
  if (typeof body !== "object" || body === null) return null;
  const envelope = body as { error?: unknown };
  if (typeof envelope.error !== "object" || envelope.error === null) return null;
  const inner = envelope.error as { code?: unknown; detail?: unknown };
  const code = typeof inner.code === "string" && inner.code !== "" ? inner.code : "unknown-error";
  return new CockpitApiError(code, status, stringifyDetail(inner.detail));
}

export async function requestWithStatus<T>(
  path: string,
  init: RequestInit,
  fetchImpl: FetchLike,
): Promise<{ status: number; body: T }> {
  let response: Response;
  try {
    response = await fetchImpl(`${apiBase()}${path}`, {
      ...init,
      headers: { "content-type": "application/json", ...init.headers },
    });
  } catch (cause) {
    throw new CockpitApiError(
      "network-error",
      0,
      `バックエンドに接続できません: ${String(cause)}`,
    );
  }

  const text = await response.text();
  let body: unknown;
  let parsed = false;
  if (text !== "") {
    try {
      body = JSON.parse(text);
      parsed = true;
    } catch {
      parsed = false;
    }
  }

  if (!response.ok) {
    const fromEnvelope = errorFromEnvelope(body, response.status);
    if (fromEnvelope !== null) throw fromEnvelope;
    throw new CockpitApiError(
      `http-${response.status}`,
      response.status,
      text.slice(0, 200),
    );
  }

  if (!parsed) {
    throw new CockpitApiError(
      "unexpected-response",
      response.status,
      `JSONではありません: ${text.slice(0, 200)}`,
    );
  }
  return { status: response.status, body: body as T };
}

export async function request<T>(
  path: string,
  init: RequestInit,
  fetchImpl: FetchLike,
): Promise<T> {
  return (await requestWithStatus<T>(path, init, fetchImpl)).body;
}
