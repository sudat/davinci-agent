/** Approval-session endpoints: pending-session listing and execution. */

import { request, type FetchLike } from "@/lib/http";
import { outputQuery, type OutputId } from "@/lib/episode-api";

export type PendingApprovalItem = {
  record_id: string;
  purpose: string;
  target_hash: string;
};

export type ApprovalSession = {
  session_key: string;
  kind: "normal" | "exception";
  purposes: string[];
  items: PendingApprovalItem[];
  explanation: string | null;
};

export type DecidedApproval = {
  record_id: string;
  purpose: string;
  decision: string;
};

export type ApprovalSessionsPayload = {
  available: boolean;
  sessions: ApprovalSession[];
  blocking_session_count: number;
  decided: DecidedApproval[];
};

export async function getApprovalSessions(
  episodeId: string,
  fetchImpl: FetchLike = fetch,
  outputId?: OutputId,
): Promise<ApprovalSessionsPayload> {
  return request<ApprovalSessionsPayload>(
    `/episodes/${encodeURIComponent(episodeId)}/approval-sessions${outputQuery(outputId)}`,
    { method: "GET" },
    fetchImpl,
  );
}

export type ApprovalExecuteInput = {
  decision: "approve" | "reject";
  actor_id: string;
};

export type ApprovalExecuteResult = {
  record_id: string;
  decision: string;
  superseded_record_id: string | null;
  runner_class: string;
  fixture_only: boolean;
};

export async function executeApproval(
  episodeId: string,
  approvalId: string,
  input: ApprovalExecuteInput,
  fetchImpl: FetchLike = fetch,
  outputId?: OutputId,
): Promise<ApprovalExecuteResult> {
  return request<ApprovalExecuteResult>(
    `/episodes/${encodeURIComponent(episodeId)}/approvals/${encodeURIComponent(
      approvalId,
    )}${outputQuery(outputId)}`,
    { method: "POST", body: JSON.stringify(input) },
    fetchImpl,
  );
}
