import { describe, expect, it, vi } from "vitest";
import {
  CockpitApiError,
  getConsultation,
  postConsultationJudgment,
  postConsultationMessage,
  type Consultation,
} from "@/lib/api";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const budget = {
  llm_calls_used: 1,
  llm_calls_limit: 10,
  intervals_used: 0,
  intervals_limit: 6,
  wall_seconds_used: 0,
  wall_seconds_limit: 600,
  cost_display: "計測なし（回数管理）",
};

const entry: Consultation = {
  consultation_id: "c-1",
  created_at: "2026-09-08T10:00:00Z",
  message: "冒頭を引きから",
  proposals: [],
  judgments: [],
  budget,
};

describe("getConsultation", () => {
  it("GET /episodes/{id}/consultation を叩き、payloadをそのまま返す", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(200, { consultations: [entry] }));

    const result = await getConsultation("ep-x", fetchImpl as unknown as typeof fetch);

    expect(result.consultations).toEqual([entry]);
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/cockpit-api/episodes/ep-x/consultation");
    expect(init.method).toBe("GET");
  });

  it("episodeIdはencodeURIComponentされる", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(200, { consultations: [] }));

    await getConsultation("ep/a b", fetchImpl as unknown as typeof fetch);

    const [url] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toContain(encodeURIComponent("ep/a b"));
  });

  it("構造化エラーは CockpitApiError に写る", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse(429, {
        error: { code: "consultation-budget-exhausted", detail: "limit" },
      }),
    );

    const cause = await getConsultation("ep-x", fetchImpl as unknown as typeof fetch).catch(
      (error: unknown) => error,
    );

    expect(cause).toBeInstanceOf(CockpitApiError);
    expect((cause as CockpitApiError).code).toBe("consultation-budget-exhausted");
    expect((cause as CockpitApiError).status).toBe(429);
  });

  it("ネットワーク断は network-error", async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError("fetch failed"));

    const cause = await getConsultation("ep-x", fetchImpl as unknown as typeof fetch).catch(
      (error: unknown) => error,
    );

    expect(cause).toBeInstanceOf(CockpitApiError);
    expect((cause as CockpitApiError).code).toBe("network-error");
  });
});

describe("postConsultationMessage", () => {
  it("POST /consultation/message に {message} を送り、更新後の1エントリを受け取る", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(200, entry));

    const result = await postConsultationMessage(
      "ep-x",
      { message: "冒頭を引きから" },
      fetchImpl as unknown as typeof fetch,
    );

    expect(result).toEqual(entry);
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/cockpit-api/episodes/ep-x/consultation/message");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ message: "冒頭を引きから" });
  });

  it("llm-unavailableの型付きエラーがそのまま出る", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse(503, {
        error: { code: "consultation-llm-unavailable", detail: "runtime down" },
      }),
    );

    const cause = await postConsultationMessage(
      "ep-x",
      { message: "m" },
      fetchImpl as unknown as typeof fetch,
    ).catch((error: unknown) => error);

    expect((cause as CockpitApiError).code).toBe("consultation-llm-unavailable");
  });
});

describe("postConsultationJudgment", () => {
  it("POST /consultation/judgment に判断本体を送る（空メモはnull）", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(200, entry));
    const input = {
      consultation_id: "c-1",
      proposal_id: "p-1",
      decision: "adopt" as const,
      scope: { composition: true, appearance: false, audio: true },
      note: null,
    };

    await postConsultationJudgment("ep-x", input, fetchImpl as unknown as typeof fetch);

    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/cockpit-api/episodes/ep-x/consultation/judgment");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual(input);
  });

  it("202は再編集あり：view全体（policy/rebuild付き）をstatus付きで返す", async () => {
    const view = {
      consultations: [entry],
      policy: {
        adopted: {
          consultation_id: "c-1",
          judgment_id: "j-1",
          proposal_id: "p-1",
          decision: "adopt",
          scope: { composition: true, appearance: false, audio: true },
          audience_message: "",
          structure: "",
          duration_estimate: "",
          candidate_scenes: "",
          subtitle_policy: "",
          audio_policy: "",
          tempo_policy: "",
          reference_mapping: "",
          unused_reasons: "",
          unconfirmed: "",
          note: "",
        },
      },
      rebuild: { status: "requested", target_version: null, detail: null },
    };
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(202, view));

    const result = await postConsultationJudgment(
      "ep-x",
      {
        consultation_id: "c-1",
        proposal_id: "p-1",
        decision: "adopt",
        scope: { composition: true, appearance: false, audio: true },
        note: null,
      },
      fetchImpl as unknown as typeof fetch,
    );

    expect(result.status).toBe(202);
    expect(result.view.consultations).toEqual([entry]);
    expect(result.view.rebuild).toEqual({
      status: "requested",
      target_version: null,
      detail: null,
    });
    expect(result.view.policy?.adopted?.judgment_id).toBe("j-1");
  });

  it("200は再編集なし：新フィールドのないviewをstatus付きで返す", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(200, { consultations: [entry] }));

    const result = await postConsultationJudgment(
      "ep-x",
      {
        consultation_id: "c-1",
        proposal_id: "p-1",
        decision: "reject",
        scope: { composition: true, appearance: true, audio: true },
        note: null,
      },
      fetchImpl as unknown as typeof fetch,
    );

    expect(result.status).toBe(200);
    expect(result.view.consultations).toEqual([entry]);
    expect(result.view.policy).toBeUndefined();
    expect(result.view.rebuild).toBeUndefined();
  });

  it("旧形式（単一エントリ本文）はviewに包んで返す", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(200, entry));

    const result = await postConsultationJudgment(
      "ep-x",
      {
        consultation_id: "c-1",
        proposal_id: "p-1",
        decision: "reject",
        scope: { composition: true, appearance: true, audio: true },
        note: null,
      },
      fetchImpl as unknown as typeof fetch,
    );

    expect(result.status).toBe(200);
    expect(result.view.consultations).toEqual([entry]);
  });
});
