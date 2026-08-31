/** Kit recipe A/B preview endpoints: listing, media URLs, selection. */

import { apiBase, request, type FetchLike } from "@/lib/http";

export type KitPreviewBounds = Record<
  string,
  { min: number; max: number; default: number; unit?: string | null }
>;

export type KitPreviewCandidate = {
  recipe_id: string;
  semantic_intent: string;
  resolved_params: Record<string, number>;
  parameter_bounds: KitPreviewBounds;
  file: string;
};

export type KitPreviewSnippet = {
  origin: "best_moment" | "shot" | "explicit";
  media_path: string;
  start_frame: number;
  end_frame: number;
  duration_seconds: number;
};

export type KitPreviewDomain = {
  domain: string;
  intents: string[];
  snippet: KitPreviewSnippet;
  candidates: KitPreviewCandidate[];
  /** Latest recorded recipe_id for the domain; null = none/keep-current. */
  selection: string | null;
};

export type KitPreviewsPayload = {
  available: boolean;
  domains: KitPreviewDomain[];
};

export type KitSelectionResult = {
  domain: string;
  recipe_id: string | null;
  semantic_intent: string | null;
  note: string | null;
  recorded_at: string;
  entries: number;
};

export async function getKitPreviews(
  episodeId: string,
  fetchImpl: FetchLike = fetch,
): Promise<KitPreviewsPayload> {
  return request<KitPreviewsPayload>(
    `/episodes/${encodeURIComponent(episodeId)}/kit-previews`,
    { method: "GET" },
    fetchImpl,
  );
}

/** Candidate mp4 URL; `file` is "domain/name.mp4" (each segment encoded). */
export function kitPreviewFileUrl(episodeId: string, file: string): string {
  const encoded = file
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
  return `${apiBase()}/episodes/${encodeURIComponent(episodeId)}/kit-previews/${encoded}`;
}

export async function selectKitRecipe(
  episodeId: string,
  domain: string,
  input: { recipe_id?: string | null; note?: string | null },
  fetchImpl: FetchLike = fetch,
): Promise<KitSelectionResult> {
  return request<KitSelectionResult>(
    `/episodes/${encodeURIComponent(episodeId)}/kit-previews/${encodeURIComponent(
      domain,
    )}/select`,
    { method: "POST", body: JSON.stringify(input) },
    fetchImpl,
  );
}
