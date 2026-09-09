import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import {
  applyReviewCommand,
  executeApproval,
  getApprovalSessions,
  getEpisodeFlags,
  getEpisodeOutputs,
  outputLabel,
  postRebuild,
  previewUrl,
  previewVideoSrc,
  probeEpisodePreview,
  registerOutput,
  revertReviewPlan,
} from "@/lib/api";
import EpisodeView from "@/components/EpisodeView";
import PreviewPlayer from "@/components/PreviewPlayer";
import ReviewChatPanel from "@/components/ReviewChatPanel";
import { createRef } from "react";

/**
 * 工程5 FRONTEND (per-output display) — contract shapes read from
 * video-pipeline/services/episode_cockpit/api.py + services/outputs/geometry.py:
 * GET /outputs → {outputs: [{output_id, orientation, width, height}]},
 * POST /outputs → {outputs, registered, idempotent}, ?output=vertical on
 * preview/flags/approvals/sessions/revert, output_id in rebuild/apply bodies.
 * The episode STATUS payload has NO output dimension (status_view.py) —
 * stage/version stay shared; only preview/flags/approvals/rebuild are scoped.
 */

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const LANDSCAPE_GEOMETRY = {
  output_id: "landscape",
  orientation: "landscape",
  width: 1920,
  height: 1080,
};

const VERTICAL_GEOMETRY = {
  output_id: "vertical",
  orientation: "portrait",
  width: 1080,
  height: 1920,
};

describe("outputs API — 取得と登録の形状", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("GET /outputs の backend 形状をそのまま写す（横版+縦版+寸法）", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ outputs: [LANDSCAPE_GEOMETRY, VERTICAL_GEOMETRY] }),
      );
    const payload = await getEpisodeOutputs(
      "ep-1",
      fetchImpl as unknown as typeof fetch,
    );
    expect(payload?.outputs).toHaveLength(2);
    expect(payload?.outputs[1]).toMatchObject({ output_id: "vertical", width: 1080 });
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/cockpit-api/episodes/ep-1/outputs");
    expect(init.method).toBe("GET");
  });

  it("旧バックエンド（404）→ null（横版のみUI、推測しない）", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response(null, { status: 404 }));
    const payload = await getEpisodeOutputs(
      "ep-1",
      fetchImpl as unknown as typeof fetch,
    );
    expect(payload).toBeNull();
  });

  it("outputs 欄なし → null（欠けた欄を補完しない）", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({ episode_id: "ep-1" }));
    const payload = await getEpisodeOutputs(
      "ep-1",
      fetchImpl as unknown as typeof fetch,
    );
    expect(payload).toBeNull();
  });

  it("POST /outputs は {output_id} を送り、idempotent を写す（欠けたら false）", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse({
        outputs: [LANDSCAPE_GEOMETRY, VERTICAL_GEOMETRY],
        registered: "vertical",
        idempotent: true,
      }),
    );
    const result = await registerOutput(
      "ep-1",
      "vertical",
      fetchImpl as unknown as typeof fetch,
    );
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/cockpit-api/episodes/ep-1/outputs");
    expect(JSON.parse(String(init.body))).toEqual({ output_id: "vertical" });
    expect(result).toMatchObject({ registered: "vertical", idempotent: true });
    expect(result.outputs).toHaveLength(2);
  });

  it("登録応答に idempotent 欄なし → false（新規追加扱い）", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse({
        outputs: [LANDSCAPE_GEOMETRY, VERTICAL_GEOMETRY],
        registered: "vertical",
      }),
    );
    const result = await registerOutput(
      "ep-1",
      "vertical",
      fetchImpl as unknown as typeof fetch,
    );
    expect(result.idempotent).toBe(false);
  });
});

describe("outputs API — 出力次元の付き方（横版は無次元）", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("previewUrl / previewVideoSrc: 横版は従来URL、縦版だけ ?output=vertical", () => {
    expect(previewUrl("ep-abc")).toBe("/cockpit-api/episodes/ep-abc/preview");
    expect(previewUrl("ep-abc", "landscape")).toBe(
      "/cockpit-api/episodes/ep-abc/preview",
    );
    expect(previewUrl("ep-abc", "vertical")).toBe(
      "/cockpit-api/episodes/ep-abc/preview?output=vertical",
    );
    expect(previewVideoSrc("ep-abc", "landscape", null)).toBe(
      "/cockpit-api/episodes/ep-abc/preview",
    );
    expect(previewVideoSrc("ep-abc", "landscape", "hash-1")).toBe(
      "/cockpit-api/episodes/ep-abc/preview?content_hash=hash-1",
    );
    expect(previewVideoSrc("ep-abc", "vertical", null)).toBe(
      "/cockpit-api/episodes/ep-abc/preview?output=vertical",
    );
    expect(previewVideoSrc("ep-abc", "vertical", "hash-1")).toBe(
      "/cockpit-api/episodes/ep-abc/preview?output=vertical&content_hash=hash-1",
    );
  });

  it("outputLabel: 横版/縦版、未知は素通し（隠さない）", () => {
    expect(outputLabel("landscape")).toBe("横版");
    expect(outputLabel("vertical")).toBe("縦版");
    expect(outputLabel("square")).toBe("square");
  });

  it("probe: 縦版だけ ?output=vertical で叩く", async () => {
    const fetchImpl = vi.fn().mockImplementation(() =>
      Promise.resolve(new Response(null, { status: 404 })),
    );    await probeEpisodePreview("ep-1", fetchImpl as unknown as typeof fetch);
    await probeEpisodePreview(
      "ep-1",
      fetchImpl as unknown as typeof fetch,
      "landscape",
    );
    await probeEpisodePreview(
      "ep-1",
      fetchImpl as unknown as typeof fetch,
      "vertical",
    );
    const urls = fetchImpl.mock.calls.map(([url]) => String(url));
    expect(urls[0]).toBe("/cockpit-api/episodes/ep-1/preview");
    expect(urls[1]).toBe("/cockpit-api/episodes/ep-1/preview");
    expect(urls[2]).toBe("/cockpit-api/episodes/ep-1/preview?output=vertical");
  });

  it("flags: 縦版だけ ?output=vertical", async () => {
    const fetchImpl = vi.fn().mockImplementation(() =>
      Promise.resolve(jsonResponse({ flags: [], not_yet_generated: true })),
    );    await getEpisodeFlags("ep-1", fetchImpl as unknown as typeof fetch);
    await getEpisodeFlags("ep-1", fetchImpl as unknown as typeof fetch, "vertical");
    const urls = fetchImpl.mock.calls.map(([url]) => String(url));
    expect(urls[0]).toBe("/cockpit-api/episodes/ep-1/flags");
    expect(urls[1]).toBe("/cockpit-api/episodes/ep-1/flags?output=vertical");
  });

  it("rebuild/apply: output_id は縦版のときだけ送る（横版 body は無次元）", async () => {
    const fetchImpl = vi.fn().mockImplementation(() =>
      Promise.resolve(jsonResponse({ stage_hint: null, scheduled: true })),
    );    await postRebuild(
      "ep-1",
      { applied_command: "rcmd-1" },
      fetchImpl as unknown as typeof fetch,
    );
    await postRebuild(
      "ep-1",
      { applied_command: "rcmd-1", output_id: "vertical" },
      fetchImpl as unknown as typeof fetch,
    );
    const bodies = fetchImpl.mock.calls.map(([, init]) =>
      JSON.parse(String((init as RequestInit).body)),
    );
    expect(bodies[0]).toEqual({ applied_command: "rcmd-1" });
    expect(bodies[1]).toEqual({ applied_command: "rcmd-1", output_id: "vertical" });
  });

  it("apply: output_id は縦版のときだけ送る", async () => {
    const fetchImpl = vi.fn().mockImplementation(() =>
      Promise.resolve(
        jsonResponse({
          applied: {
            schema_version: "v",
            command_id: "rcmd-1",
            command_kind: "keep_longer",
            affected_domain: "edit_plan",
            event_id: null,
            base_plan_version: "v1",
            result_plan_version: "v2",
            deferred: false,
            reason: null,
            target_seconds: null,
            seconds_delta: null,
          },
          rebuild: {
            schema_version: "v",
            command_id: "rcmd-1",
            command_kind: "keep_longer",
            affected_domain: "edit_plan",
            stages: [],
            excluded_stages: [],
          },
        }),
      ),
    );
    await applyReviewCommand(
      "ep-1",
      { text: "のばして" },
      fetchImpl as unknown as typeof fetch,
    );
    await applyReviewCommand(
      "ep-1",
      { text: "のばして", output_id: "vertical" },
      fetchImpl as unknown as typeof fetch,
    );
    const bodies = fetchImpl.mock.calls.map(([, init]) =>
      JSON.parse(String((init as RequestInit).body)),
    );
    expect(bodies[0]).toEqual({ text: "のばして" });
    expect(bodies[1]).toEqual({ text: "のばして", output_id: "vertical" });
  });

  it("revert / approval-sessions / execute: 縦版だけ ?output=vertical", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/approval-sessions")) {
        return jsonResponse({ available: false, sessions: [], blocking_session_count: 0, decided: [] });
      }
      if (url.includes("/review-chat/revert")) {
        return jsonResponse({
          restored_from_version: "v1",
          new_version: "v2",
          rebuild: {
            stage_hint: "preview",
            scheduled: true,
            stages: ["preview"],
            runner_log: "r",
            applied_command: "c",
          },
        });
      }
      return jsonResponse({
        record_id: "a",
        decision: "approve",
        superseded_record_id: null,
        runner_class: "r",
        fixture_only: true,
      });
    });
    const asFetch = fetchImpl as unknown as typeof fetch;
    await revertReviewPlan("ep-1", asFetch);
    await revertReviewPlan("ep-1", asFetch, "vertical");
    await getApprovalSessions("ep-1", asFetch);
    await getApprovalSessions("ep-1", asFetch, "vertical");
    await executeApproval("ep-1", "a", { decision: "approve", actor_id: "op" }, asFetch);
    await executeApproval(
      "ep-1",
      "a",
      { decision: "approve", actor_id: "op" },
      asFetch,
      "vertical",
    );
    const urls = fetchImpl.mock.calls.map(([url]) => String(url));
    expect(urls[0]).toBe("/cockpit-api/episodes/ep-1/review-chat/revert");
    expect(urls[1]).toBe("/cockpit-api/episodes/ep-1/review-chat/revert?output=vertical");
    expect(urls[2]).toBe("/cockpit-api/episodes/ep-1/approval-sessions");
    expect(urls[3]).toBe("/cockpit-api/episodes/ep-1/approval-sessions?output=vertical");
    expect(urls[4]).toBe("/cockpit-api/episodes/ep-1/approvals/a");
    expect(urls[5]).toBe("/cockpit-api/episodes/ep-1/approvals/a?output=vertical");
  });
});

describe("PreviewPlayer — 出力ごとの寸法", () => {
  it("縦版は 360x640 の実寸を名乗る（src も ?output=vertical）", () => {
    render(
      <PreviewPlayer
        episodeId="ep-abc"
        state="available"
        contentHash={null}
        videoRef={createRef()}
        output="vertical"
      />,
    );
    const video = screen.getByTestId("preview-player") as HTMLVideoElement;
    expect(video.getAttribute("src")).toBe(
      "/cockpit-api/episodes/ep-abc/preview?output=vertical",
    );
    expect(video.getAttribute("width")).toBe("360");
    expect(video.getAttribute("height")).toBe("640");
    expect(video.getAttribute("data-output")).toBe("vertical");
  });

  it("縦版+hash は ?output=vertical&content_hash=…", () => {
    render(
      <PreviewPlayer
        episodeId="ep-abc"
        state="available"
        contentHash="hash-9"
        videoRef={createRef()}
        output="vertical"
      />,
    );
    const video = screen.getByTestId("preview-player") as HTMLVideoElement;
    expect(video.getAttribute("src")).toBe(
      "/cockpit-api/episodes/ep-abc/preview?output=vertical&content_hash=hash-9",
    );
  });

  it("横版（既定）は従来どおり（寸法 attrs なし・固定URL）", () => {
    render(
      <PreviewPlayer
        episodeId="ep-abc"
        state="available"
        contentHash={null}
        videoRef={createRef()}
      />,
    );
    const video = screen.getByTestId("preview-player") as HTMLVideoElement;
    expect(video.getAttribute("src")).toBe("/cockpit-api/episodes/ep-abc/preview");
    expect(video.hasAttribute("width")).toBe(false);
    expect(video.hasAttribute("height")).toBe(false);
  });
});

type EpisodeRoutes = {
  outputs: () => Response;
  registerVertical: () => Response;
  status: () => Response;
  flags: (url: string) => Response;
  preview: (url: string) => Response;
};

function stubEpisodeApi(routes: EpisodeRoutes): ReturnType<typeof vi.fn> {
  const calls: { url: string; method: string; body: string | null }[] = [];
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const record = {
      url,
      method,
      body:
        typeof init?.body === "string"
          ? init.body
          : init?.body == null
            ? null
            : String(init.body),
    };
    calls.push(record);
    (fetchMock as unknown as { callsLog: typeof calls }).callsLog = calls;
    if (url.includes("/outputs") && method === "POST") {
      return Promise.resolve(routes.registerVertical());
    }
    if (url.includes("/outputs")) return Promise.resolve(routes.outputs());
    if (url.endsWith("/flags") || url.includes("/flags?")) {
      return Promise.resolve(routes.flags(url));
    }
    if (url.includes("/preview")) return Promise.resolve(routes.preview(url));
    if (url.includes("/consultation")) {
      return Promise.resolve(jsonResponse({ consultations: [] }));
    }
    return Promise.resolve(routes.status());
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const baseStatus = () =>
  jsonResponse({
    episode_id: "ep-out01",
    job_id: "ep-out01",
    status: "PREVIEW_READY",
    current_stage: "preview",
    created_at_seq: 1,
    updated_at_seq: 9,
    stage_runs: [],
    current_run: "run-1",
    current_target_version: "v3",
  });

const headersFor = (run: string, version: string, hash: string) => ({
  "content-type": "video/mp4",
  "x-cockpit-preview-run-id": run,
  "x-cockpit-preview-target-version": version,
  "x-cockpit-preview-content-sha256": hash,
});

function requestedUrls(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls.map(([input]) => String(input));
}

describe("EpisodeView — 出力が2つあるときだけ切替が出る", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("横版+縦版 → 切替（output-select）と独立注記が出る", async () => {
    stubEpisodeApi({
      outputs: () => jsonResponse({ outputs: [LANDSCAPE_GEOMETRY, VERTICAL_GEOMETRY] }),
      registerVertical: () => jsonResponse({ outputs: [], registered: "vertical" }),
      status: baseStatus,
      flags: () => jsonResponse({ flags: [], not_yet_generated: true }),
      preview: () => new Response(null, { status: 404 }),
    });

    render(<EpisodeView episodeId="ep-out01" />);

    const selector = await screen.findByTestId("output-select");
    expect(selector.textContent).toContain("横版");
    expect(selector.textContent).toContain("縦版");
    expect(screen.getByTestId("output-option-landscape")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByTestId("output-independence-note").textContent).toContain(
      "横版と縦版の承認は別々です",
    );
  });

  it("縦版を選ぶ → preview src と probe が縦版に切り替わる", async () => {
    const fetchMock = stubEpisodeApi({
      outputs: () => jsonResponse({ outputs: [LANDSCAPE_GEOMETRY, VERTICAL_GEOMETRY] }),
      registerVertical: () => jsonResponse({ outputs: [], registered: "vertical" }),
      status: baseStatus,
      flags: () => jsonResponse({ flags: [], not_yet_generated: true }),
      preview: (url) =>
        url.includes("output=vertical")
          ? new Response(null, {
              status: 206,
              headers: headersFor("run-9", "v9", "v".repeat(64)),
            })
          : new Response(null, { status: 206, headers: headersFor("run-1", "v3", "h".repeat(64)) }),
    });

    render(<EpisodeView episodeId="ep-out01" />);
    await screen.findByTestId("output-select");

    fireEvent.click(screen.getByTestId("output-option-vertical"));

    await waitFor(() => {
      const video = screen.getByTestId("preview-player") as HTMLVideoElement;
      expect(video.getAttribute("src")).toContain("output=vertical");
    });
    expect(requestedUrls(fetchMock).some((url) => url.includes("output=vertical"))).toBe(
      true,
    );
    expect(screen.getByTestId("output-option-vertical")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("binding 行は選択中の出力に従う（横版=今回、縦版=未生成）", async () => {
    stubEpisodeApi({
      outputs: () => jsonResponse({ outputs: [LANDSCAPE_GEOMETRY, VERTICAL_GEOMETRY] }),
      registerVertical: () => jsonResponse({ outputs: [], registered: "vertical" }),
      status: baseStatus,
      flags: () => jsonResponse({ flags: [], not_yet_generated: true }),
      preview: (url) =>
        url.includes("output=vertical")
          ? new Response(null, { status: 404 })
          : new Response(null, { status: 206, headers: headersFor("run-1", "v3", "h".repeat(64)) }),
    });

    render(<EpisodeView episodeId="ep-out01" />);
    await screen.findByTestId("output-select");
    await waitFor(() => {
      expect(screen.getByTestId("preview-binding").textContent).toBe(
        "今回の実行の試し編集です（対象版 v3）",
      );
    });

    fireEvent.click(screen.getByTestId("output-option-vertical"));

    await waitFor(() => {
      expect(screen.getByTestId("preview-binding").textContent).toBe(
        "試し編集はまだ生成されていません",
      );
    });
  });

  it("縦版を追加する → POST して 追加しました（selector が生える）", async () => {
    const fetchMock = stubEpisodeApi({
      outputs: () => jsonResponse({ outputs: [LANDSCAPE_GEOMETRY] }),
      registerVertical: () =>
        jsonResponse({
          outputs: [LANDSCAPE_GEOMETRY, VERTICAL_GEOMETRY],
          registered: "vertical",
          idempotent: false,
        }),
      status: baseStatus,
      flags: () => jsonResponse({ flags: [], not_yet_generated: true }),
      preview: () => new Response(null, { status: 404 }),
    });

    render(<EpisodeView episodeId="ep-out01" />);
    const addButton = await screen.findByTestId("output-add");
    expect(screen.queryByTestId("output-select")).toBeNull();

    fireEvent.click(addButton);

    await waitFor(() => {
      expect(screen.getByTestId("output-add-result").textContent).toBe(
        "縦版を追加しました",
      );
    });
    const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
    const registerPost = posts.find(([input]) => String(input).includes("/outputs"));
    expect(registerPost).toBeTruthy();
    expect(JSON.parse(String(registerPost?.[1]?.body))).toEqual({
      output_id: "vertical",
    });
    expect(await screen.findByTestId("output-select")).toBeTruthy();
  });

  it("登録済みの縦版を追加する → 既に追加済み（idempotent を正直に表示）", async () => {
    stubEpisodeApi({
      outputs: () => jsonResponse({ outputs: [LANDSCAPE_GEOMETRY] }),
      registerVertical: () =>
        jsonResponse({
          outputs: [LANDSCAPE_GEOMETRY, VERTICAL_GEOMETRY],
          registered: "vertical",
          idempotent: true,
        }),
      status: baseStatus,
      flags: () => jsonResponse({ flags: [], not_yet_generated: true }),
      preview: () => new Response(null, { status: 404 }),
    });

    render(<EpisodeView episodeId="ep-out01" />);
    fireEvent.click(await screen.findByTestId("output-add"));

    await waitFor(() => {
      expect(screen.getByTestId("output-add-result").textContent).toBe(
        "縦版は既に追加済みです",
      );
    });
  });
});

describe("EpisodeView — 横版のみの回帰（出力ノイズなし）", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("横版のみ → 切替なし・?output= 付きfetchなし・POSTなし・preview URL不変", async () => {
    const fetchMock = stubEpisodeApi({
      outputs: () => jsonResponse({ outputs: [LANDSCAPE_GEOMETRY] }),
      registerVertical: () => jsonResponse({ outputs: [], registered: "vertical" }),
      status: baseStatus,
      flags: () => jsonResponse({ flags: [], not_yet_generated: true }),
      preview: () =>
        new Response(null, { status: 206, headers: headersFor("run-1", "v3", "h".repeat(64)) }),
    });

    render(<EpisodeView episodeId="ep-out01" />);

    const video = (await screen.findByTestId("preview-player")) as HTMLVideoElement;
    expect(video.getAttribute("src")).toBe(
      "/cockpit-api/episodes/ep-out01/preview?content_hash=" + encodeURIComponent("h".repeat(64)),
    );
    expect(screen.queryByTestId("output-select")).toBeNull();
    expect(screen.queryByTestId("output-independence-note")).toBeNull();
    expect(screen.queryByTestId("output-add-result")).toBeNull();
    // 明示の登録操作だけが POST する — 自動登録も出力次元付き取得もしない。
    const urls = requestedUrls(fetchMock);
    expect(urls.filter((url) => url.includes("output="))).toEqual([]);
    expect(
      fetchMock.mock.calls.filter(([, init]) => (init?.method ?? "GET") !== "GET"),
    ).toEqual([]);
  });

  it("outputs 不明（旧 backend 相当）→ 切替も追加も出さない", async () => {
    const fetchMock = stubEpisodeApi({
      outputs: () => new Response(null, { status: 404 }),
      registerVertical: () => jsonResponse({ outputs: [], registered: "vertical" }),
      status: baseStatus,
      flags: () => jsonResponse({ flags: [], not_yet_generated: true }),
      preview: () => new Response(null, { status: 404 }),
    });

    render(<EpisodeView episodeId="ep-out01" />);

    await waitFor(() => {
      expect(screen.getByTestId("preview-pending")).toBeTruthy();
    });
    expect(screen.queryByTestId("output-select")).toBeNull();
    expect(screen.queryByTestId("output-add")).toBeNull();
    const urls = requestedUrls(fetchMock);
    expect(urls.filter((url) => url.includes("output="))).toEqual([]);
  });
});

const CHAT_DRAFT = {
  schema_version: "cockpit-review-command-draft-v1",
  command_id: "rcmd-0123456789ab",
  command_kind: "keep_longer",
  text: "この後2秒残して",
  target_seconds: 1,
  seconds_delta: 2,
  scope: "episode",
  needs_confirmation: false,
  confirmation_reason: null,
};

const CHAT_APPLIED = {
  schema_version: "cockpit-applied-command-v1",
  command_id: "rcmd-0123456789ab",
  command_kind: "keep_longer",
  affected_domain: "edit_plan",
  event_id: null,
  base_plan_version: "v1",
  result_plan_version: "v2",
  deferred: false,
  reason: null,
  target_seconds: 1,
  seconds_delta: 2,
};

const CHAT_REBUILD_PLAN = {
  schema_version: "cockpit-rebuild-plan-v1",
  command_id: "rcmd-0123456789ab",
  command_kind: "keep_longer",
  affected_domain: "edit_plan",
  stages: ["plan", "preview"],
  excluded_stages: [],
};

const CHAT_STATUS = {
  episode_id: "ep-abc",
  job_id: "ep-abc",
  status: "PREVIEW_READY",
  current_stage: "preview",
  created_at_seq: 1,
  updated_at_seq: 1,
  stage_runs: [],
};

describe("ReviewChatPanel — 適用/再構築/復帰は選択中の出力を運ぶ", () => {
  it("縦版: apply body と rebuild body に output_id、revert は ?output=vertical", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/review-chat")) {
        return jsonResponse({ received: true, sequence: 1, draft: CHAT_DRAFT });
      }
      if (url.endsWith("/review-chat/apply")) {
        return jsonResponse({ applied: CHAT_APPLIED, rebuild: CHAT_REBUILD_PLAN });
      }
      if (url.endsWith("/rebuild") || url.includes("/rebuild?")) {
        return jsonResponse({ stage_hint: "plan,preview", scheduled: true });
      }
      if (url.includes("/review-chat/revert")) {
        return jsonResponse({
          restored_from_version: "v1",
          new_version: "v2",
          rebuild: {
            stage_hint: "preview",
            scheduled: true,
            stages: ["preview"],
            runner_log: "r",
            applied_command: "c",
          },
        });
      }
      throw new Error(`unexpected url: ${url}`);
    });

    render(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => 1}
        status={CHAT_STATUS}
        fetchImpl={fetchImpl as unknown as typeof fetch}
        outputId="vertical"
      />,
    );

    fireEvent.change(screen.getByLabelText("修正指示（自然言語）"), {
      target: { value: "この後2秒残して" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));
    await waitFor(() => {
      expect(screen.getByTestId("review-draft")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("review-apply-button"));
    await waitFor(() => {
      expect(screen.getByTestId("rebuild-indicator")).toBeTruthy();
    });

    const bodies = fetchImpl.mock.calls.map(([, init]) =>
      init?.body == null ? null : JSON.parse(String(init.body)),
    );
    const applyBody = bodies.find(
      (body) => body !== null && Array.isArray(body.drafts),
    );
    const rebuildBody = bodies.find(
      (body) => body !== null && body.applied_command === "rcmd-0123456789ab",
    );
    expect(applyBody).toMatchObject({ output_id: "vertical" });
    expect(rebuildBody).toMatchObject({ output_id: "vertical" });

    fireEvent.click(screen.getByTestId("review-revert-button"));
    await waitFor(() => {
      expect(
        fetchImpl.mock.calls.some(([url]) =>
          String(url).includes("/review-chat/revert?output=vertical"),
        ),
      ).toBe(true);
    });
  });

  it("横版（既定）: body に output_id なし・revert に query なし（従来どおり）", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/review-chat")) {
        return jsonResponse({ received: true, sequence: 1, draft: CHAT_DRAFT });
      }
      if (url.endsWith("/review-chat/apply")) {
        return jsonResponse({ applied: CHAT_APPLIED, rebuild: CHAT_REBUILD_PLAN });
      }
      return jsonResponse({ stage_hint: "plan,preview", scheduled: true });
    });

    render(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => 1}
        status={CHAT_STATUS}
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );

    fireEvent.change(screen.getByLabelText("修正指示（自然言語）"), {
      target: { value: "この後2秒残して" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));
    await waitFor(() => {
      expect(screen.getByTestId("review-draft")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("review-apply-button"));
    await waitFor(() => {
      expect(screen.getByTestId("rebuild-indicator")).toBeTruthy();
    });

    const bodies = fetchImpl.mock.calls.map(([, init]) =>
      init?.body == null ? null : JSON.parse(String(init.body)),
    );
    const applyBody = bodies.find(
      (body) => body !== null && Array.isArray(body.drafts),
    );
    const rebuildBody = bodies.find(
      (body) => body !== null && body.applied_command === "rcmd-0123456789ab",
    );
    expect(applyBody).toEqual({
      text: "この後2秒残して",
      at_seconds: 1,
      drafts: [CHAT_DRAFT],
      sequence: 1,
    });
    expect(rebuildBody).toEqual({ applied_command: "rcmd-0123456789ab" });
  });
});
