import { describe, expect, it, vi } from "vitest";
import { CockpitApiError, getFinishingStatus } from "@/lib/api";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("getFinishingStatus", () => {
  it("available: true では7ドメインとrun_idまでパースする", async () => {
    const payload = {
      available: true,
      episode_id: "ep-x",
      run_id: "run-1",
      domains: [
        { domain: "editorial_construction", status: "applied", justification: null, blocked: false },
        { domain: "subtitle", status: "applied", justification: null, blocked: false },
        { domain: "audio_finishing", status: "applied", justification: null, blocked: false },
        { domain: "color_finishing", status: "applied", justification: null, blocked: false },
        {
          domain: "framing_motion",
          status: "intentionally_not_needed",
          justification: "no framing requested",
          blocked: false,
        },
        {
          domain: "graphics_presentation",
          status: "intentionally_not_needed",
          justification: "no graphics requested",
          blocked: false,
        },
        {
          domain: "delivery_qc",
          status: "blocked",
          justification: "QC did not pass",
          blocked: true,
        },
      ],
    };
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(200, payload));

    const result = await getFinishingStatus("ep-x", fetchImpl as unknown as typeof fetch);

    expect(result.available).toBe(true);
    expect(result.domains).toHaveLength(7);
    expect(result.run_id).toBe("run-1");
    const [url] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/cockpit-api/episodes/ep-x/finishing-status");
  });

  it("available: false（未実施）では domains=[]", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(200, { available: false, domains: [] }));

    const result = await getFinishingStatus("ep-y", fetchImpl as unknown as typeof fetch);

    expect(result.available).toBe(false);
    expect(result.domains).toHaveLength(0);
  });

  it("episodeIdはencodeURIComponentされる", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(200, { available: false, domains: [] }));

    await getFinishingStatus("ep/a b", fetchImpl as unknown as typeof fetch);

    const [url] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toContain(encodeURIComponent("ep/a b"));
  });

  it("構造化エラーは CockpitApiError に写る", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse(422, {
        error: { code: "finishing-run-invalid", detail: "not a valid report" },
      }),
    );

    const cause = await getFinishingStatus(
      "ep-x",
      fetchImpl as unknown as typeof fetch,
    ).catch((error: unknown) => error);

    expect(cause).toBeInstanceOf(CockpitApiError);
    expect((cause as CockpitApiError).code).toBe("finishing-run-invalid");
    expect((cause as CockpitApiError).status).toBe(422);
  });

  it("ネットワーク断は network-error", async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError("fetch failed"));

    const cause = await getFinishingStatus(
      "ep-x",
      fetchImpl as unknown as typeof fetch,
    ).catch((error: unknown) => error);

    expect(cause).toBeInstanceOf(CockpitApiError);
    expect((cause as CockpitApiError).code).toBe("network-error");
  });
});
