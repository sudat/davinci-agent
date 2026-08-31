/** Reference-learning endpoints: preference parsing, registration, listing. */

import { request, type FetchLike } from "@/lib/http";

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
