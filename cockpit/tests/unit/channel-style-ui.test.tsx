import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const pushMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

import IntakeForm from "@/components/IntakeForm";
import EpisodeView from "@/components/EpisodeView";
import ConsultationPanel from "@/components/ConsultationPanel";
import type {
  Consultation,
  ConsultationAdoptedPolicy,
  ConsultationPayload,
  ConsultationProposal,
  EpisodeStatus,
} from "@/lib/api";

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

type Call = { url: string; init: RequestInit };

function recordingFetch(
  responseFor: (url: string, init: RequestInit) => Response,
): { fetchImpl: typeof fetch; calls: Call[] } {
  const calls: Call[] = [];
  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const requestInit = init ?? {};
    calls.push({ url, init: requestInit });
    return responseFor(url, requestInit);
  }) as typeof fetch;
  return { fetchImpl, calls };
}

function fill(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

function stageStatus(stage: string, applied?: { channel: string; version: number }): EpisodeStatus {
  return {
    episode_id: "ep-c01",
    job_id: "ep-c01",
    status: "ANALYZED",
    current_stage: stage,
    created_at_seq: 1,
    updated_at_seq: 1,
    stage_runs: [],
    ...(applied !== undefined ? { applied_style: applied } : {}),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  pushMock.mockClear();
});

describe("IntakeForm — チャンネル選択とスタイルのピン留め（工程3）", () => {
  const styleV2 = {
    channel_id: "ch-a",
    versions: [
      { version: 1, name: "初版", saved_at: "2026-09-01T00:00:00Z" },
      { version: 2, name: "台所回", saved_at: "2026-09-08T00:00:00Z" },
    ],
    current: 2,
  };

  function intakeFetch(styleBody: unknown = styleV2) {
    return recordingFetch((url) => {
      if (url.endsWith("/style/restore")) return jsonResponse({ version: 3 }, 201);
      if (url.includes("/style")) return jsonResponse(styleBody);
      if (url.endsWith("/channels")) {
        return jsonResponse({ channels: [{ channel_id: "ch-a" }, { channel_id: "ch-b" }] });
      }
      return jsonResponse({
        episode_id: "ep-new",
        job_id: "ep-new",
        status: "CREATED",
        brief_status: "draft",
      });
    });
  }

  it("GET /channelsの一覧を出し、選んだチャンネルの現在版を表示する", async () => {
    const { fetchImpl } = intakeFetch();
    render(<IntakeForm fetchImpl={fetchImpl} />);

    const select = await screen.findByTestId("channel-select");
    expect(select.textContent).toContain("ch-a");
    expect(select.textContent).toContain("ch-b");
    expect(await screen.findByTestId("channel-style-current")).toBeVisible();
    expect(screen.getByTestId("channel-style-current").textContent).toBe(
      "使われるスタイル: 台所回（版2）",
    );
    expect(screen.queryByTestId("channels-fallback")).toBeNull();
  });

  it("明示選択でのみchannel+style_versionを送る（触らなければ送らない）", async () => {
    const { fetchImpl, calls } = intakeFetch();
    render(<IntakeForm fetchImpl={fetchImpl} />);
    await screen.findByTestId("channel-style-current");

    fill("撮影素材のフォルダを選ぶ", "/tmp/a");
    fill("どんな動画にしたいですか？", "テストbrief");
    fireEvent.change(screen.getByTestId("channel-select"), { target: { value: "ch-b" } });
    await waitFor(() =>
      expect(screen.getByTestId("channel-style-current")).toBeVisible(),
    );
    fireEvent.click(screen.getByTestId("create-button"));

    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/episodes/ep-new"));
    const createCalls = calls.filter((call) => call.url.endsWith("/episodes"));
    expect(createCalls).toHaveLength(1);
    expect(JSON.parse(String(createCalls[0]!.init.body))).toEqual({
      source_folder: "/tmp/a",
      brief_text: "テストbrief",
      channel: "ch-b",
      style_version: 2,
    });
  });

  it("チャンネルに触れなければchannelもstyle_versionも送らない", async () => {
    const { fetchImpl, calls } = intakeFetch();
    render(<IntakeForm fetchImpl={fetchImpl} />);
    await screen.findByTestId("channel-style-current");

    fill("撮影素材のフォルダを選ぶ", "/tmp/a");
    fill("どんな動画にしたいですか？", "テストbrief");
    fireEvent.click(screen.getByTestId("create-button"));

    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/episodes/ep-new"));
    const createCalls = calls.filter((call) => call.url.endsWith("/episodes"));
    expect(JSON.parse(String(createCalls[0]!.init.body))).toEqual({
      source_folder: "/tmp/a",
      brief_text: "テストbrief",
    });
  });

  it("一覧の取得失敗は単一デフォルトに正直に退避し、素のbodyで作成する", async () => {
    const { fetchImpl, calls } = recordingFetch((url) => {
      if (url.endsWith("/channels")) {
        return jsonResponse({ error: { code: "channels-unavailable", detail: "down" } }, 503);
      }
      if (url.includes("/style")) {
        return jsonResponse({ channel_id: "default", versions: [], current: null });
      }
      return jsonResponse({
        episode_id: "ep-new",
        job_id: "ep-new",
        status: "CREATED",
        brief_status: "draft",
      });
    });
    render(<IntakeForm fetchImpl={fetchImpl} />);

    fireEvent.click(screen.getByText("詳細設定"));
    expect(await screen.findByTestId("channels-fallback")).toBeVisible();
    expect(screen.getByTestId("channels-fallback").textContent).toContain(
      "チャンネル一覧を取得できませんでした",
    );
    expect(screen.getByTestId("channel-select").textContent).toContain("デフォルト");
    fireEvent.click(screen.getByText("詳しい記録"));
    expect(await screen.findByTestId("channel-style-empty")).toBeVisible();
    expect(screen.getByTestId("channel-style-empty").textContent).toContain(
      "保存されたスタイルはまだありません",
    );

    fill("撮影素材のフォルダを選ぶ", "/tmp/a");
    fill("どんな動画にしたいですか？", "テストbrief");
    fireEvent.click(screen.getByTestId("create-button"));
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/episodes/ep-new"));
    const createCalls = calls.filter((call) => call.url.endsWith("/episodes"));
    expect(JSON.parse(String(createCalls[0]!.init.body))).toEqual({
      source_folder: "/tmp/a",
      brief_text: "テストbrief",
    });
  });

  it("版が2つ以上あれば戻す操作ができ、POST restore後に表示が更新される", async () => {
    let current = 2;
    const styleOf = () => ({
      channel_id: "ch-a",
      versions: [
        { version: 1, name: "初版", saved_at: "2026-09-01T00:00:00Z" },
        { version: 2, name: "台所回", saved_at: "2026-09-08T00:00:00Z" },
        ...(current === 3
          ? [{ version: 3, name: "初版", saved_at: "2026-09-09T00:00:00Z" }]
          : []),
      ],
      current,
    });
    const { fetchImpl, calls } = recordingFetch((url) => {
      if (url.endsWith("/style/restore")) {
        current = 3;
        return jsonResponse({ version: 3 }, 201);
      }
      if (url.includes("/style")) return jsonResponse(styleOf());
      if (url.endsWith("/channels")) {
        return jsonResponse({ channels: [{ channel_id: "ch-a" }] });
      }
      return jsonResponse({ episode_id: "ep-new", job_id: "ep-new", status: "CREATED", brief_status: "draft" });
    });
    render(<IntakeForm fetchImpl={fetchImpl} />);
    await screen.findByTestId("channel-style-current");

    expect(screen.getByTestId("style-version-1").textContent).toContain("版1 初版");
    fireEvent.click(screen.getByTestId("style-restore-1"));

    await waitFor(() => {
      expect(screen.getByTestId("style-restored").textContent).toBe(
        "版1の内容で新版3として保存しました",
      );
    });
    expect(screen.getByTestId("channel-style-current").textContent).toBe(
      "使われるスタイル: 初版（版3）",
    );
    const restoreCalls = calls.filter((call) => call.url.endsWith("/style/restore"));
    expect(restoreCalls).toHaveLength(1);
    expect(JSON.parse(String(restoreCalls[0]!.init.body))).toEqual({ target_version: 1 });
  });

  it("いつものスタイルを使うは保存済みがあるときだけ出て、付けるとstyle_versionを送る", async () => {
    const { fetchImpl, calls } = intakeFetch();
    render(<IntakeForm fetchImpl={fetchImpl} />);
    await screen.findByTestId("channel-style-current");

    const optIn = screen.getByTestId("style-opt-in") as HTMLInputElement;
    expect(optIn.checked).toBe(false);
    fireEvent.click(optIn);
    expect(optIn.checked).toBe(true);

    fill("撮影素材のフォルダを選ぶ", "/tmp/a");
    fill("どんな動画にしたいですか？", "テストbrief");
    fireEvent.click(screen.getByTestId("create-button"));

    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/episodes/ep-new"));
    const createCalls = calls.filter((call) => call.url.endsWith("/episodes"));
    expect(JSON.parse(String(createCalls[0]!.init.body))).toEqual({
      source_folder: "/tmp/a",
      brief_text: "テストbrief",
      channel: "ch-a",
      style_version: 2,
    });
  });

  it("保存済みスタイルがなければいつものスタイルを使うは出ない", async () => {
    const { fetchImpl } = recordingFetch((url) => {
      if (url.includes("/style")) {
        return jsonResponse({ channel_id: "default", versions: [], current: null });
      }
      if (url.endsWith("/channels")) {
        return jsonResponse({ channels: [{ channel_id: "default" }] });
      }
      return jsonResponse({
        episode_id: "ep-new",
        job_id: "ep-new",
        status: "CREATED",
        brief_status: "draft",
      });
    });
    render(<IntakeForm fetchImpl={fetchImpl} />);
    await screen.findByTestId("channel-style-empty");
    expect(screen.queryByTestId("style-opt-in")).toBeNull();
  });
});

describe("EpisodeView — 使ったスタイル行（工程3）", () => {
  function stubStatus(statusBody: unknown) {
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const url = String(input);
      if (url.endsWith("/flags")) {
        return Promise.resolve(jsonResponse({ flags: [], not_yet_generated: true }));
      }
      if (url.endsWith("/preview")) return Promise.resolve(new Response(null, { status: 404 }));
      if (url.includes("/consultation")) {
        return Promise.resolve(jsonResponse({ consultations: [] }));
      }
      return Promise.resolve(jsonResponse(statusBody));
    });
    vi.stubGlobal("fetch", fetchMock);
  }

  const baseStatus = {
    episode_id: "ep-applied",
    job_id: "ep-applied",
    status: "PREVIEW_READY",
    current_stage: "review",
    created_at_seq: 1,
    updated_at_seq: 9,
    stage_runs: [],
  };

  it("applied_styleがあるとき一行出す", async () => {
    stubStatus({ ...baseStatus, applied_style: { channel: "ch-a", version: 2 } });
    render(<EpisodeView episodeId="ep-applied" />);
    expect(await screen.findByTestId("applied-style")).toBeVisible();
    expect(screen.getByTestId("applied-style").textContent).toBe(
      "使ったスタイル: チャンネルch-a・版2",
    );
  });

  it("applied_styleがない旧データでは何も出さない（不明ノイズなし）", async () => {
    stubStatus(baseStatus);
    render(<EpisodeView episodeId="ep-applied" />);
    await screen.findByTestId("episode-status");
    expect(screen.queryByTestId("applied-style")).toBeNull();
  });
});

describe("ConsultationPanel — 今後のスタイルとして保存（工程3・明示操作）", () => {
  const budget = {
    llm_calls_used: 1,
    llm_calls_limit: 10,
    intervals_used: 1,
    intervals_limit: 6,
    wall_seconds_used: 60,
    wall_seconds_limit: 600,
    cost_display: "費用は直接計測できません（回数で管理）",
  };

  const proposal: ConsultationProposal = {
    proposal_id: "p-1",
    title: "冒頭から引きで見せる構成",
    summary: "引きの映像から始める構成です。",
    details: {
      audience_message: "手間が減ることを伝えます。",
      structure: "引き → 解決 → まとめ",
      duration_estimate: "45秒前後",
      candidate_scenes: ["冒頭の空撮"],
      subtitle_policy: "短く区切る",
      audio_policy: "いつもの選曲",
      tempo_policy: "前半は速め",
      reference_mapping: "ep12を踏襲",
      unused_reasons: "玄関は使わない",
      unconfirmed: ["After撮影の有無"],
    },
  };

  const adopted: ConsultationAdoptedPolicy = {
    consultation_id: "c-1",
    judgment_id: "j-1",
    proposal_id: "p-1",
    decision: "adopt",
    scope: { composition: true, appearance: false, audio: true },
    audience_message: "手間が減ることを伝えます。",
    structure: "引き → 解決 → まとめ",
    duration_estimate: "45秒前後",
    candidate_scenes: ["冒頭の空撮"],
    subtitle_policy: "短く区切る",
    audio_policy: "いつもの選曲",
    tempo_policy: "前半は速め",
    reference_mapping: "ep12を踏襲",
    unused_reasons: "玄関は使わない",
    unconfirmed: ["After撮影の有無"],
    note: "",
  };

  const entry: Consultation = {
    consultation_id: "c-1",
    created_at: "2026-09-08T10:00:00Z",
    message: "冒頭の見せ方を相談したい",
    proposals: [proposal],
    judgments: [],
    budget,
  };

  const adoptedPayload: ConsultationPayload = {
    consultations: [{ ...entry, policy: { adopted }, rebuild: null }],
  };

  it("採用済み方針に保存ボタンが出て、201で版Nを表示する", async () => {
    const { fetchImpl, calls } = recordingFetch((url) =>
      url.includes("/style/save")
        ? jsonResponse({ version: 3 }, 201)
        : jsonResponse(adoptedPayload),
    );
    render(
      <ConsultationPanel
        episodeId="ep-c01"
        status={stageStatus("selection", { channel: "ch-a", version: 2 })}
        fetchImpl={fetchImpl}
      />,
    );

    expect(await screen.findByTestId("style-save-button")).toBeVisible();
    expect(screen.getByTestId("style-save-channel").textContent).toContain("ch-a");
    expect(screen.queryByTestId("style-saved")).toBeNull();

    fireEvent.change(screen.getByTestId("style-save-name"), {
      target: { value: "いつもの台所回" },
    });
    fireEvent.click(screen.getByTestId("style-save-button"));

    await waitFor(() => {
      expect(screen.getByTestId("style-saved").textContent).toBe(
        "スタイルに保存しました（版3）",
      );
    });
    const saveCalls = calls.filter((call) => call.url.includes("/style/save"));
    expect(saveCalls).toHaveLength(1);
    expect(saveCalls[0]!.url).toContain("/channels/ch-a/style/save");
    const body = JSON.parse(String(saveCalls[0]!.init.body));
    expect(body.name).toBe("いつもの台所回");
    expect(body.structure).toBe("引き → 解決 → まとめ");
    expect(body.source).toEqual({ episode_id: "ep-c01", judgment_id: "j-1", proposal_id: "p-1" });
  });

  it("200冪等は保存済み表示になる", async () => {
    const { fetchImpl } = recordingFetch((url) =>
      url.includes("/style/save")
        ? jsonResponse({ version: 2, idempotent: true }, 200)
        : jsonResponse(adoptedPayload),
    );
    render(
      <ConsultationPanel
        episodeId="ep-c01"
        status={stageStatus("selection", { channel: "ch-a", version: 2 })}
        fetchImpl={fetchImpl}
      />,
    );

    await screen.findByTestId("style-save-button");
    fireEvent.change(screen.getByTestId("style-save-name"), {
      target: { value: "いつもの台所回" },
    });
    fireEvent.click(screen.getByTestId("style-save-button"));

    await waitFor(() => {
      expect(screen.getByTestId("style-saved").textContent).toBe(
        "すでに最新のスタイルとして保存済みです（版2）",
      );
    });
  });

  it("採用がなければ保存操作は出ない", async () => {
    const { fetchImpl, calls } = recordingFetch(() =>
      jsonResponse({ consultations: [entry] }),
    );
    render(
      <ConsultationPanel
        episodeId="ep-c01"
        status={stageStatus("selection", { channel: "ch-a", version: 2 })}
        fetchImpl={fetchImpl}
      />,
    );
    await screen.findAllByTestId("consultation-proposal");
    expect(screen.queryByTestId("style-save-area")).toBeNull();
    expect(calls.some((call) => call.url.includes("/style/save"))).toBe(false);
  });

  it("採用だけでは保存しない（ボタンを押すまで書き込みなし）", async () => {
    const judged: ConsultationPayload = {
      consultations: [
        {
          ...entry,
          judgments: [
            {
              judgment_id: "j-1",
              proposal_id: "p-1",
              decision: "adopt",
              scope: { composition: true, appearance: true, audio: true },
              note: null,
              created_at: "2026-09-08T10:05:00Z",
            },
          ],
          policy: { adopted },
          rebuild: null,
        },
      ],
    };
    const { fetchImpl, calls } = recordingFetch((url) => {
      if (url.endsWith("/consultation/judgment")) return jsonResponse(judged, 200);
      if (url.includes("/style/save")) return jsonResponse({ version: 3 }, 201);
      return jsonResponse({ consultations: [entry] });
    });
    render(
      <ConsultationPanel
        episodeId="ep-c01"
        status={stageStatus("selection", { channel: "ch-a", version: 2 })}
        fetchImpl={fetchImpl}
      />,
    );
    await screen.findAllByTestId("consultation-proposal");

    fireEvent.click(screen.getByTestId("consultation-judgment-adopt"));
    fireEvent.click(screen.getByTestId("consultation-judgment-submit"));

    await waitFor(() => {
      expect(screen.getByTestId("style-save-button")).toBeVisible();
    });
    expect(calls.some((call) => call.url.includes("/style/save"))).toBe(false);

    fireEvent.change(screen.getByTestId("style-save-name"), {
      target: { value: "いつもの台所回" },
    });
    fireEvent.click(screen.getByTestId("style-save-button"));
    await waitFor(() => {
      expect(screen.getByTestId("style-saved")).toBeVisible();
    });
    expect(calls.filter((call) => call.url.includes("/style/save"))).toHaveLength(1);
  });

  it("保存先チャンネル不明では正直な行だけ出す", async () => {
    const { fetchImpl } = recordingFetch(() => jsonResponse(adoptedPayload));
    render(
      <ConsultationPanel
        episodeId="ep-c01"
        status={stageStatus("selection")}
        fetchImpl={fetchImpl}
      />,
    );
    expect(await screen.findByTestId("style-save-no-channel")).toBeVisible();
    expect(screen.queryByTestId("style-save-button")).toBeNull();
  });
});
