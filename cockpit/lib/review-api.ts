/** Review loop endpoints: chat, command application, partial rebuild. */

import { request, type FetchLike } from "@/lib/http";

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
  /** V44-1: present ONLY when one message proposed SEVERAL commands
   *  (multi-target LLM interpretation); `draft` is the first of these. */
  drafts?: ReviewCommandDraft[];
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
  /** V44-1: present when a message applied SEVERAL commands;
   *  `applied` is the first of these. */
  applied_commands?: AppliedCommand[];
};

/** The echoed drafts ride along (V44-1): the backend integrity-checks
 *  each command_id server-side, so only previewed drafts ever apply. */
export type ReviewApplyInput = {
  text: string;
  at_seconds?: number | null;
  drafts?: ReviewCommandDraft[];
};

export async function applyReviewCommand(
  episodeId: string,
  input: ReviewApplyInput,
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
  note?: string;
  applied_command?: string;
  /** V44-1 batch rebuild: every applied command id in the batch
   *  (present only when more than one was applied). */
  applied_commands?: string[];
  rebuild_stages?: string[];
  /** Task-9 fields (present when the backend schedules the runner):
   *  stages = the lineage-derived stage set the rebuild plan covers;
   *  runner_log = the episode's runner.log path; reason = the honest
   *  refusal note for not-yet-executable command kinds. */
  stages?: string[];
  runner_log?: string;
  reason?: string;
};

export async function postRebuild(
  episodeId: string,
  input: { applied_command?: string; applied_commands?: string[]; stage_hint?: string },
  fetchImpl: FetchLike = fetch,
): Promise<RebuildResult> {
  return request<RebuildResult>(
    `/episodes/${encodeURIComponent(episodeId)}/rebuild`,
    { method: "POST", body: JSON.stringify(input) },
    fetchImpl,
  );
}
