/** Finishing domains status endpoint: read-only display of the 7 quality domains. */

import { request, type FetchLike } from "@/lib/http";

export type FinishingDomainStatus = {
  domain: string;
  status: string;
  justification: string | null;
  blocked: boolean;
};

export type FinishingStatusPayload = {
  available: boolean;
  episode_id?: string;
  run_id?: string;
  domains: FinishingDomainStatus[];
};

export async function getFinishingStatus(
  episodeId: string,
  fetchImpl: FetchLike = fetch,
): Promise<FinishingStatusPayload> {
  return request<FinishingStatusPayload>(
    `/episodes/${encodeURIComponent(episodeId)}/finishing-status`,
    { method: "GET" },
    fetchImpl,
  );
}
