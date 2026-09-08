import { describe, expect, it, vi } from "vitest";
import {
  apiBase,
  CockpitApiError,
  createEpisode,
  getEpisodeStatus,
  probeEpisodePreview,
} from "@/lib/api";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("apiBase", () => {
  it("既定は同一オリジンプロキシ /cockpit-api", () => {
    const before = { ...process.env };
    delete process.env.COCKPIT_API;
    delete process.env.NEXT_PUBLIC_COCKPIT_API;
    expect(apiBase()).toBe("/cockpit-api");
    process.env.COCKPIT_API = before.COCKPIT_API;
    process.env.NEXT_PUBLIC_COCKPIT_API = before.NEXT_PUBLIC_COCKPIT_API;
  });

  it("リテラル文字列 \"undefined\" は未設定扱い（transform対策の回帰テスト）", () => {
    const before = { ...process.env };
    process.env.NEXT_PUBLIC_COCKPIT_API = "undefined";
    process.env.COCKPIT_API = "undefined";
    expect(apiBase()).toBe("/cockpit-api");
    process.env.COCKPIT_API = before.COCKPIT_API;
    process.env.NEXT_PUBLIC_COCKPIT_API = before.NEXT_PUBLIC_COCKPIT_API;
  });
});

describe("createEpisode", () => {
  it("成功時は episode_id 等をパースする", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse(200, {
        episode_id: "ep-abc123",
        job_id: "ep-abc123",
        status: "CREATED",
        brief_status: "draft",
      }),
    );
    const result = await createEpisode(
      { source_folder: "/tmp/a", brief_text: "説明" },
      fetchImpl as unknown as typeof fetch,
    );
    expect(result.episode_id).toBe("ep-abc123");
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/cockpit-api/episodes");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      source_folder: "/tmp/a",
      brief_text: "説明",
    });
  });

  it("構造化エラー {error:{code,detail}} を CockpitApiError に写す（malformed input / 409）", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse(409, {
        error: {
          code: "episode-exists",
          detail: "an episode for source folder /tmp/a already exists",
        },
      }),
    );
    const cause = await createEpisode(
      { source_folder: "/tmp/a", brief_text: "x" },
      fetchImpl as unknown as typeof fetch,
    ).catch((error: unknown) => error);
    expect(cause).toBeInstanceOf(CockpitApiError);
    const apiError = cause as CockpitApiError;
    expect(apiError.code).toBe("episode-exists");
    expect(apiError.status).toBe(409);
    expect(apiError.detail).toContain("already exists");
  });

  it("detailが配列（validation-error）でも文字列化して保持する", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse(422, {
        error: {
          code: "validation-error",
          detail: [{ loc: ["body", "brief_text"], msg: "field required" }],
        },
      }),
    );
    const cause = await createEpisode(
      { source_folder: "/tmp/a", brief_text: "" },
      fetchImpl as unknown as typeof fetch,
    ).catch((error: unknown) => error);
    const apiError = cause as CockpitApiError;
    expect(apiError.code).toBe("validation-error");
    expect(apiError.detail).toContain("brief_text");
  });

  it("非JSONボディは unexpected-response", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(new Response("<html>bad gateway</html>", { status: 502 }));
    const cause = await getEpisodeStatus(
      "ep-x",
      fetchImpl as unknown as typeof fetch,
    ).catch((error: unknown) => error);
    const apiError = cause as CockpitApiError;
    expect(apiError.code).toBe("http-502");
  });

  it("ネットワーク断は network-error（status 0）", async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError("fetch failed"));
    const cause = await getEpisodeStatus(
      "ep-x",
      fetchImpl as unknown as typeof fetch,
    ).catch((error: unknown) => error);
    const apiError = cause as CockpitApiError;
    expect(apiError).toBeInstanceOf(CockpitApiError);
    expect(apiError.code).toBe("network-error");
    expect(apiError.status).toBe(0);
  });
});

describe("getEpisodeStatus", () => {
  it("成功時はステージ情報までパースする", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse(200, {
        episode_id: "ep-1",
        job_id: "ep-1",
        status: "CREATED",
        current_stage: "intake",
        created_at_seq: 1,
        updated_at_seq: 1,
        stage_runs: [
          {
            stage_name: "ingest",
            status: "PENDING",
            retry_count: 0,
            last_error_code: null,
          },
        ],
      }),
    );
    const status = await getEpisodeStatus(
      "ep-1",
      fetchImpl as unknown as typeof fetch,
    );
    expect(status.current_stage).toBe("intake");
    expect(status.stage_runs).toHaveLength(1);
    const [url] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/cockpit-api/episodes/ep-1");
  });

  it("404はエラーコードを保持する（stale state / 不正ID）", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse(404, {
        error: { code: "job-not-found", detail: "no job row for ep-missing" },
      }),
    );
    const cause = await getEpisodeStatus(
      "ep-missing",
      fetchImpl as unknown as typeof fetch,
    ).catch((error: unknown) => error);
    const apiError = cause as CockpitApiError;
    expect(apiError.code).toBe("job-not-found");
    expect(apiError.status).toBe(404);
  });
});

describe("probeEpisodePreview（試し編集probeの正規化）", () => {
  const bindingHeaders = {
    "content-type": "video/mp4",
    "x-cockpit-preview-run-id": "run-20260909-a",
    "x-cockpit-preview-target-version": "v7",
    "x-cockpit-preview-content-sha256": "c".repeat(64),
    "x-cockpit-preview-output-arrived-at": "2026-09-09T01:02:03+00:00",
  };

  it("206+全ヘッダ → available true と4欄を正規化する（2-byte Range probe）", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 206, headers: bindingHeaders }));
    const probe = await probeEpisodePreview("ep-1", fetchImpl as unknown as typeof fetch);
    expect(probe).toEqual({
      available: true,
      run_id: "run-20260909-a",
      target_version: "v7",
      content_hash: "c".repeat(64),
      output_arrived_at: "2026-09-09T01:02:03+00:00",
    });
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/cockpit-api/episodes/ep-1/preview");
    expect(init.headers).toMatchObject({ range: "bytes=0-1" });
  });

  it("200でも正規化できる（Rangeを無視するバックエンド）", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 200, headers: bindingHeaders }));
    const probe = await probeEpisodePreview("ep-1", fetchImpl as unknown as typeof fetch);
    expect(probe.available).toBe(true);
    expect(probe.run_id).toBe("run-20260909-a");
    expect(probe.target_version).toBe("v7");
  });

  it("ヘッダが一部欠け → 欠けた欄だけ null（推測しない）", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      new Response(null, {
        status: 206,
        headers: {
          "x-cockpit-preview-run-id": "run-1",
          "x-cockpit-preview-content-sha256": "abc",
        },
      }),
    );
    const probe = await probeEpisodePreview("ep-1", fetchImpl as unknown as typeof fetch);
    expect(probe).toEqual({
      available: true,
      run_id: "run-1",
      target_version: null,
      content_hash: "abc",
      output_arrived_at: null,
    });
  });

  it("旧バックエンド（ヘッダなしの200）→ available true・全欄 null（下流は不明として扱う）", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 200 }));
    const probe = await probeEpisodePreview("ep-1", fetchImpl as unknown as typeof fetch);
    expect(probe).toEqual({
      available: true,
      run_id: null,
      target_version: null,
      content_hash: null,
      output_arrived_at: null,
    });
  });

  it("404 → available false・全欄 null（未生成）", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response(null, { status: 404 }));
    const probe = await probeEpisodePreview("ep-1", fetchImpl as unknown as typeof fetch);
    expect(probe).toEqual({
      available: false,
      run_id: null,
      target_version: null,
      content_hash: null,
      output_arrived_at: null,
    });
  });

  it("404以外のHTTPエラーは throw する（not_generated に偽装しない）", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response(null, { status: 500 }));
    const cause = await probeEpisodePreview(
      "ep-1",
      fetchImpl as unknown as typeof fetch,
    ).catch((error: unknown) => error);
    expect(cause).toBeInstanceOf(CockpitApiError);
    const apiError = cause as CockpitApiError;
    expect(apiError.code).toBe("http-500");
    expect(apiError.status).toBe(500);
  });

  it("ネットワーク断は network-error で throw する", async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError("fetch failed"));
    const cause = await probeEpisodePreview(
      "ep-1",
      fetchImpl as unknown as typeof fetch,
    ).catch((error: unknown) => error);
    expect(cause).toBeInstanceOf(CockpitApiError);
    expect((cause as CockpitApiError).code).toBe("network-error");
  });
});
