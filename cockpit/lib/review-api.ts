/** Review loop endpoints: chat, command application, partial rebuild. */

import { request, type FetchLike } from "@/lib/http";
import type { OutputId } from "@/lib/episode-api";
import { outputQuery } from "@/lib/episode-api";

/** UX-redesign 工程1: honest DISPLAY state for feelings-route messages
 *  that went through cause investigation. Backend derivation
 *  (api.py `_investigation_state`): `hypothesis-proposed` when the
 *  interpreter proposed a hypothesis; `materials-checked` when nearby
 *  transcript/scene text was gathered; `unconfirmed` when nothing was
 *  available. No state may claim an identified cause — the completion
 *  form 「原因を調査しました」 is forbidden on this surface. */
export type InvestigationState =
  | "hypothesis-proposed"
  | "materials-checked"
  | "unconfirmed";

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
  /** UX-redesign 工程1 (feelings route): display-only cause hypothesis.
   *  Optional for backward compatibility with pre-investigation payloads;
   *  never enters the command_id hash, never relaxes confirmation. */
  hypothesis?: string | null;
  /** True when the message went through cause investigation. */
  investigated?: boolean;
  /** Honest display state when the backend attaches it per draft. */
  investigation_state?: InvestigationState;
};

/** 工程2 (reaction flow, backend `ReviewReactionKind` in models.py): the
 *  closed set of messages that REACT to the latest unconsumed proposal set
 *  instead of proposing a new command themselves. */
export type ReviewReactionKind =
  | "choice-a"
  | "choice-b"
  | "both-different"
  | "continuation";

/** 工程2 rework: how a previewed proposal set is adopted (backend
 *  `ProposalKind` in models.py). `command-bundle` = one plan fix previewed
 *  as several commands, ALL applied TOGETHER (a single-draft echo is a typed
 *  422 `bundle-requires-full-apply`); `alternatives` = mutually exclusive,
 *  adopt exactly ONE (a full-set echo is `alternatives-require-choice`). */
export type ProposalKind = "command-bundle" | "alternatives";

/** 工程2 rework: one checked video STILL (backend `FrameMaterial`) — a
 *  single frame with its 対象/時点/元版 traceability. A still is NEVER an
 *  audio or whole-video verification and no display may claim it is. */
export type FrameMaterial = {
  path: string;
  at_seconds: number;
  source: string;
};

/** 工程2 display honesty (backend `checked_materials`): what the cause
 *  investigation COULD check — nearby transcript/scene text plus, when the
 *  frame gate extracted stills, their traceability records. Video and
 *  audio are never checked by this machinery and never claimed. `frames`
 *  is present ONLY when stills were extracted (absent = not extracted,
 *  never "checked none"). Rework round 2 P1-2 — 抽出 / AIに渡した /
 *  確認結果が返った are DIFFERENT facts: `frames_verified` is true ONLY
 *  when an image-capable transport call was attempted with the stills AND
 *  the model answered; extraction alone is NEVER verification. */
export type CheckedMaterials = {
  transcript: boolean;
  shot: boolean;
  frames?: FrameMaterial[];
  /** True only when an image-capable transport call was ATTEMPTED with
   *  these frames (invocation fact; pre-spawn failures included) — actual
   *  transport-level delivery is UNCONFIRMED (with `frames`). */
  frames_delivery_attempted?: boolean;
  /** True only when `frames_delivery_attempted` AND the model answered —
   *  a STILL is verified, not merely extracted. Absent/false = extraction
   *  only. */
  frames_verified?: boolean;
};

export type ReviewChatResult = {
  received: boolean;
  sequence: number;
  draft: ReviewCommandDraft;
  /** V44-1: present ONLY when one message proposed SEVERAL commands
   *  (multi-target LLM interpretation); `draft` is the first of these. */
  drafts?: ReviewCommandDraft[];
  /** UX-redesign 工程1: present ONLY for feelings-class messages that went
   *  through cause investigation (display-only, mirrors draft.hypothesis). */
  investigated?: boolean;
  hypothesis?: string | null;
  /** Honest display state (see InvestigationState): present only for
   *  investigated messages. The apply request may reference the saved
   *  proposal set through `sequence` below. */
  investigation_state?: InvestigationState;
  /** UX-redesign 工程2: present only for reaction-class messages. For
   *  choices the response carries NO `draft`/`drafts` (acknowledgment only
   *  — adoption is the per-draft apply of the still-previewed set). */
  reaction?: ReviewReactionKind;
  /** 工程2: the prior proposal set's sequence this message answered. */
  responds_to_set?: number;
  /** 工程2: what the investigation could check, shown separately from any
   *  hypothesis (never a video/audio claim). */
  checked_materials?: CheckedMaterials;
  /** 工程2 rework: how the previewed set is adopted (see ProposalKind).
   *  Absent (old payloads) = "command-bundle". */
  proposal_kind?: ProposalKind;
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
 *  each command_id server-side, so only previewed drafts ever apply.
 *  `sequence` (工程1 security model) names the SERVER-SAVED proposal set
 *  recorded at review-chat time — the reference is authoritative, never
 *  the browser's echoed content. */
export type ReviewApplyInput = {
  text: string;
  at_seconds?: number | null;
  drafts?: ReviewCommandDraft[];
  /** The chat response's `sequence`: which saved proposal set to adopt.
   *  Optional (old-client shape resolves by full-draft equality). */
  sequence?: number;
  /** 工程5: the output chain this adoption renders (backend
   *  ReviewChatApplyRequest.output_id; omitted = landscape default, so
   *  landscape request bodies stay byte-identical). */
  output_id?: OutputId;
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
  input: {
    applied_command?: string;
    applied_commands?: string[];
    stage_hint?: string;
    /** 工程5: the output chain the rebuild renders (backend
     *  RebuildRequestWithCommand.output_id; omitted = landscape). */
    output_id?: OutputId;
  },
  fetchImpl: FetchLike = fetch,
): Promise<RebuildResult> {
  return request<RebuildResult>(
    `/episodes/${encodeURIComponent(episodeId)}/rebuild`,
    { method: "POST", body: JSON.stringify(input) },
    fetchImpl,
  );
}

export type ReviewRevertRebuild = {
  stage_hint: string;
  scheduled: boolean;
  stages: string[];
  runner_log: string;
  applied_command: string;
  /** P1b spawn-failure honesty: present (e.g. "runner-start-failed") when
   *  `scheduled` is false — the RESTORE itself still succeeded (the
   *  response's new_version is committed), only the rebuild launch failed.
   *  A resend relaunches the same step (backend resend is idempotent). */
  reason?: string;
  detail?: string;
};

export type ReviewRevertResult = {
  restored_from_version: string;
  new_version: string;
  rebuild: ReviewRevertRebuild;
};

/** The revert's rebuild readout in the SAME RebuildResult shape the apply
 *  flow uses, so the panel's phase derivation + status polling work
 *  unchanged across both flows (UX-redesign 工程1 undo). */
export function revertRebuildReadout(result: ReviewRevertResult): RebuildResult {
  return {
    stage_hint: result.rebuild.stage_hint,
    scheduled: result.rebuild.scheduled,
    applied_command: result.rebuild.applied_command,
    stages: result.rebuild.stages,
    runner_log: result.rebuild.runner_log,
    reason: result.rebuild.reason,
  };
}

/** UX-redesign 工程1: restore the previous plan version as a NEW version.
 *  Empty body. 409 `nothing-to-revert` at the bootstrap floor (surfaced as
 *  CockpitApiError via the shared transport, like every backend failure).
 *  工程5: `outputId` scopes the revert to that output chain (?output=). */
export async function revertReviewPlan(
  episodeId: string,
  fetchImpl: FetchLike = fetch,
  outputId?: OutputId,
): Promise<ReviewRevertResult> {
  return request<ReviewRevertResult>(
    `/episodes/${encodeURIComponent(episodeId)}/review-chat/revert${outputQuery(outputId)}`,
    { method: "POST" },
    fetchImpl,
  );
}
