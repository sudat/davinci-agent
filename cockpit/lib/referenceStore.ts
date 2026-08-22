/**
 * Session-scoped display store for the reference surface (task 50).
 *
 * PERSISTENCE CONTRACT: the authoritative store is the backend reference
 * library (POST /references writes reference-library.json via task-44
 * reference_ops). The backend exposes no list GET yet, so this module only
 * backs the session list UI — it is a display cache over POST responses,
 * NEVER a persistence layer (gap recorded in learnings for task 51 wiring).
 */

import type {
  DomainPolarities,
  PreferenceDomain,
} from "@/lib/api";

export type RegisteredReference = {
  source_id: string;
  path: string;
  sha256: string;
  library_version: number;
  registered_at: string;
};

export type SavedAnnotation = {
  episode_id: string;
  source_id: string;
  path: string;
  ts_seconds: number | null;
  comment: string;
  named_domains: PreferenceDomain[];
  domains: DomainPolarities;
  corrected: boolean;
  saved_at: string;
};

export type SavedPairwise = {
  domain: PreferenceDomain;
  choice: "a" | "b";
  reason: string;
  reference_a_id: string;
  reference_b_id: string;
  saved_at: string;
};

const REFERENCES_KEY = "cockpit.references.v1";
const ANNOTATIONS_KEY = "cockpit.annotations.v1";
const PAIRWISE_KEY = "cockpit.pairwise.v1";

function readList<T>(key: string): T[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.sessionStorage.getItem(key);
    if (raw === null) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as T[]) : [];
  } catch {
    return [];
  }
}

function writeList<T>(key: string, items: readonly T[]): void {
  if (typeof window === "undefined") return;
  window.sessionStorage.setItem(key, JSON.stringify(items));
}

function append<T>(key: string, item: T): T[] {
  const next = [...readList<T>(key), item];
  writeList(key, next);
  return next;
}

export const loadReferences = (): RegisteredReference[] =>
  readList<RegisteredReference>(REFERENCES_KEY);

export const addReference = (item: RegisteredReference): RegisteredReference[] =>
  append(REFERENCES_KEY, item);

export const loadAnnotations = (): SavedAnnotation[] =>
  readList<SavedAnnotation>(ANNOTATIONS_KEY);

export const addAnnotation = (item: SavedAnnotation): SavedAnnotation[] =>
  append(ANNOTATIONS_KEY, item);

export const loadPairwise = (): SavedPairwise[] =>
  readList<SavedPairwise>(PAIRWISE_KEY);

export const addPairwiseRecord = (item: SavedPairwise): SavedPairwise[] =>
  append(PAIRWISE_KEY, item);

/** Distinct reference source_ids, latest first — A/B comparison inputs. */
export function distinctSourceIds(): string[] {
  const seen = new Set<string>();
  const ordered: string[] = [];
  for (const reference of [...loadReferences(), ...loadAnnotations()].reverse()) {
    if (!seen.has(reference.source_id)) {
      seen.add(reference.source_id);
      ordered.push(reference.source_id);
    }
  }
  return ordered;
}
