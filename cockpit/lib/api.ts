/**
 * Typed thin client for the episode cockpit backend (task 44 + 46).
 *
 * Contract notes (read from video-pipeline/services/episode_cockpit/*):
 * - POST /episodes takes `source_folder` + `brief_text` plus the 工程3
 *   optional `channel` / `style_version` (sent ONLY when the operator
 *   explicitly chose them — never intake-only UI defaults); other
 *   intake-only UI fields must never be added to the request.
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
 *
 * Barrel: endpoint groups live in sibling concept modules; this file only
 * re-exports so `@/lib/api` stays the single import surface.
 */

export { apiBase, apiFailure, CockpitApiError, DEFAULT_API_BASE, requestWithStatus } from "@/lib/http";

export * from "@/lib/episode-api";
export * from "@/lib/reference-api";
export * from "@/lib/review-api";
export * from "@/lib/approval-api";
export * from "@/lib/kit-preview-api";
export * from "@/lib/finishing-api";
export * from "@/lib/consultation-api";
export * from "@/lib/channel-styles-api";
