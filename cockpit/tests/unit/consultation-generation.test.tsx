import { describe, expect, it } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import ConsultationPanel from "@/components/ConsultationPanel";
import type {
  Consultation,
  ConsultationGenerationPanel,
  ConsultationPayload,
  ConsultationProposal,
  EpisodeStatus,
} from "@/lib/api";

/**
 * 工程4 任意生成絵コンテ: 既定OFFの回帰、許可フロー、区分つき絵コンテ行、
 * 1枚だけの再依頼、判断メモへの同梱、拒否・型付きエラーの正直表示、
 * 旧データ耐性。判断の送信契約は増やさない。
 */

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

function stageStatus(stage: string): EpisodeStatus {
  return {
    episode_id: "ep-g4",
    job_id: "ep-g4",
    status: "ANALYZED",
    current_stage: stage,
    created_at_seq: 1,
    updated_at_seq: 1,
    stage_runs: [],
  };
}

const budget = {
  llm_calls_used: 3,
  llm_calls_limit: 10,
  intervals_used: 2,
  intervals_limit: 6,
  wall_seconds_used: 120,
  wall_seconds_limit: 600,
  cost_display: "費用は直接計測できません（回数で管理）",
};

function panelFixture(overrides: Partial<ConsultationGenerationPanel>): ConsultationGenerationPanel {
  return {
    panel_id: "a-1",
    role: "a",
    source_frame_ref: "f-1",
    image_ref: "/files/p-a1.png",
    base_created_at: "2026-09-09T04:00:00Z",
    caption: {
      subject: "台所の引きの絵",
      scene_note: "朝の光が入る場面",
      changes: "通りすがりの人物を消した",
      unconfirmed: ["色味が実素材どおりかは未確認"],
    },
    status: "generated",
    ...overrides,
  };
}

const panelsFixture: ConsultationGenerationPanel[] = [
  panelFixture({}),
  panelFixture({
    panel_id: "b-2",
    role: "b",
    source_frame_ref: "f-2",
    image_ref: null,
    caption: {
      subject: "シンク前の寄り",
      scene_note: "手元の作業",
      changes: "背景をぼかした",
      unconfirmed: ["手元がぶれていないかは未確認"],
    },
    status: "requested",
  }),
  panelFixture({
    panel_id: "r-0",
    role: "real_frame",
    source_frame_ref: "f-0",
    image_ref: null,
    caption: { subject: "元の場面", scene_note: "", changes: "", unconfirmed: [] },
    status: null,
  }),
  panelFixture({
    panel_id: "a-9",
    role: "a",
    source_frame_ref: "f-9",
    image_ref: null,
    caption: { subject: "失敗した案", scene_note: "", changes: "", unconfirmed: [] },
    status: "failed",
  }),
];

const proposalBase: ConsultationProposal = {
  proposal_id: "p-1",
  title: "冒頭から引きで見せる構成",
  summary: "引きの映像から始める構成です。",
  details: {
    audience_message: "手間が減ることを伝えます。",
    structure: "引き → まとめ",
    duration_estimate: "45秒前後",
    candidate_scenes: ["冒頭の空撮"],
    subtitle_policy: "短く",
    audio_policy: "いつもの選曲",
    tempo_policy: "前半は速め",
    reference_mapping: "",
    unused_reasons: "",
    unconfirmed: [],
  },
};

const entryPlain: Consultation = {
  consultation_id: "c-1",
  created_at: "2026-09-08T10:00:00Z",
  message: "冒頭の見せ方を相談したい",
  proposals: [proposalBase],
  judgments: [],
  budget,
};

const entryWithPanels: Consultation = {
  ...entryPlain,
  proposals: [{ ...proposalBase, panels: panelsFixture }],
};

const payloadPlain: ConsultationPayload = { consultations: [entryPlain] };

function payloadWithState(
  state: ConsultationPayload["generation_state"],
  entry: Consultation = entryPlain,
): ConsultationPayload {
  return { consultations: [entry], generation_state: state };
}

function renderPanel(fetchImpl: typeof fetch) {
  return render(
    <ConsultationPanel
      episodeId="ep-g4"
      status={stageStatus("selection")}
      fetchImpl={fetchImpl}
    />,
  );
}

function messageCalls(calls: Call[]) {
  return calls.filter((call) => call.url.endsWith("/consultation/message"));
}

describe("工程4 既定OFFの回帰", () => {
  it("生成セクションは閉じたままで内容を出さず、閉じた送信はgenerationを付けない", async () => {
    const { fetchImpl, calls } = recordingFetch((url, init) =>
      url.endsWith("/consultation/message")
        ? jsonResponse({
            ...entryPlain,
            consultation_id: "c-2",
            message: JSON.parse(String(init.body)).message,
          })
        : jsonResponse(payloadPlain),
    );
    renderPanel(fetchImpl);
    await screen.findByTestId("consultation-proposal");

    expect(screen.getByTestId("generation-permission")).toBeVisible();
    expect(screen.getByTestId("generation-section-toggle").textContent).toContain(
      "画像の案を作る（任意）",
    );
    expect(screen.queryByTestId("generation-budget-remaining")).toBeNull();
    expect(screen.queryByTestId("generation-request")).toBeNull();
    expect(screen.queryByTestId("generation-decline")).toBeNull();

    fireEvent.change(screen.getByTestId("consultation-message-input"), {
      target: { value: "冒頭を引きから" },
    });
    fireEvent.click(screen.getByTestId("consultation-send"));

    await waitFor(() => {
      expect(messageCalls(calls)).toHaveLength(1);
    });
    const body = JSON.parse(String(messageCalls(calls)[0]!.init.body)) as Record<
      string,
      unknown
    >;
    expect(body).toEqual({ message: "冒頭を引きから" });
    expect(body).not.toHaveProperty("generation");
  });
});

describe("工程4 許可フロー", () => {
  it("開くと残りと枚数が先に見え、許可して依頼が generation を付けて送る", async () => {
    const { fetchImpl, calls } = recordingFetch((url, init) =>
      url.endsWith("/consultation/message")
        ? jsonResponse({
            ...entryPlain,
            consultation_id: "c-2",
            message: JSON.parse(String(init.body)).message,
          })
        : jsonResponse(payloadPlain),
    );
    renderPanel(fetchImpl);
    await screen.findByTestId("consultation-proposal");

    fireEvent.click(screen.getByTestId("generation-section-toggle"));

    const remaining = screen.getByTestId("generation-budget-remaining").textContent ?? "";
    expect(remaining).toContain("残り");
    expect(remaining).toContain("7");
    expect(screen.getByTestId("generation-panels-1")).toBeVisible();
    expect(screen.getByTestId("generation-panels-2")).toBeVisible();
    expect(screen.getByTestId("generation-panels-3")).toBeVisible();

    fireEvent.click(screen.getByTestId("generation-panels-2"));
    fireEvent.change(screen.getByTestId("consultation-message-input"), {
      target: { value: "絵コンテも見たい" },
    });
    fireEvent.click(screen.getByTestId("generation-request"));

    await waitFor(() => {
      expect(messageCalls(calls)).toHaveLength(1);
    });
    expect(JSON.parse(String(messageCalls(calls)[0]!.init.body))).toEqual({
      message: "絵コンテも見たい",
      generation: {
        permission: { granted: true, panels_max: 2, images_max: 2 },
      },
    });
  });

  it("生成しないは generation なしで送り、畳んで追従の促しを出さない", async () => {
    const { fetchImpl, calls } = recordingFetch((url, init) =>
      url.endsWith("/consultation/message")
        ? jsonResponse({
            ...entryPlain,
            consultation_id: "c-2",
            message: JSON.parse(String(init.body)).message,
          })
        : jsonResponse(payloadPlain),
    );
    renderPanel(fetchImpl);
    await screen.findByTestId("consultation-proposal");

    fireEvent.click(screen.getByTestId("generation-section-toggle"));
    expect(screen.getByTestId("generation-request")).toBeVisible();
    fireEvent.change(screen.getByTestId("consultation-message-input"), {
      target: { value: "見本だけで進める" },
    });
    fireEvent.click(screen.getByTestId("generation-decline"));

    await waitFor(() => {
      expect(messageCalls(calls)).toHaveLength(1);
    });
    const body = JSON.parse(String(messageCalls(calls)[0]!.init.body)) as Record<
      string,
      unknown
    >;
    expect(body).toEqual({ message: "見本だけで進める" });
    expect(body).not.toHaveProperty("generation");
    expect(screen.queryByTestId("generation-request")).toBeNull();
    expect(screen.queryByTestId("generation-budget-remaining")).toBeNull();
  });
});

describe("工程4 絵コンテ行", () => {
  it("区分・状態・見本の但し書き・文言どおりを表示する", async () => {
    const { fetchImpl } = recordingFetch(() =>
      jsonResponse(payloadWithState(undefined, entryWithPanels)),
    );
    renderPanel(fetchImpl);
    await screen.findByTestId("consultation-storyboard");

    expect(screen.getByTestId("consultation-storyboard").textContent).toContain(
      "生成案は見本であり、試し編集ではありません",
    );
    expect(screen.getByTestId("panel-a-1-kind").textContent).toBe("生成案");
    expect(screen.getByTestId("panel-b-2-kind").textContent).toBe("生成案");
    expect(screen.getByTestId("panel-r-0-kind").textContent).toBe("撮影素材");
    expect(screen.getByTestId("panel-a-1-image").getAttribute("src")).toBe(
      "/episodes/ep-g4/consultation/panels/a-1",
    );
    expect(screen.getByTestId("panel-b-2-placeholder").textContent).toBe("準備中");
    expect(screen.getByTestId("panel-r-0-placeholder").textContent).toBe("準備中");
    expect(screen.getByTestId("panel-a-9-placeholder").textContent).toContain(
      "作れませんでした（再依頼はこの1枚だけ）",
    );
    const card = screen.getByTestId("panel-a-1").textContent ?? "";
    expect(card).toContain("台所の引きの絵");
    expect(card).toContain("朝の光が入る場面");
    expect(card).toContain("通りすがりの人物を消した");
    expect(card).toContain("色味が実素材どおりかは未確認");
  });

  it("直すはその1枚だけを指名して送る", async () => {
    const { fetchImpl, calls } = recordingFetch((url, init) =>
      url.endsWith("/consultation/panels")
        ? jsonResponse({ ...entryWithPanels, consultations: undefined })
        : url.endsWith("/consultation/message")
          ? jsonResponse({
              ...entryPlain,
              consultation_id: "c-2",
              message: JSON.parse(String(init.body)).message,
            })
          : jsonResponse(payloadWithState(undefined, entryWithPanels)),
    );
    renderPanel(fetchImpl);
    await screen.findByTestId("consultation-storyboard");

    fireEvent.click(screen.getByTestId("panel-b-2-retry"));

    await waitFor(() => {
      expect(
        calls.filter((call) => call.url.endsWith("/consultation/panels")),
      ).toHaveLength(1);
    });
    const retryCall = calls.find((call) => call.url.endsWith("/consultation/panels"))!;
    const body = JSON.parse(String(retryCall.init.body)) as {
      consultation_id: string;
      proposal_id: string;
      base_created_at: string;
      permission: { granted: boolean; panels_max: number; images_max: number };
      retry_panel_ids: string[];
    };
    expect(body.retry_panel_ids).toEqual(["b-2"]);
    expect(body.permission).toEqual({
      granted: true,
      panels_max: 1,
      images_max: 1,
    });
    expect(body.consultation_id).toBe("c-1");
    expect(body.proposal_id).toBe("p-1");
    expect(typeof body.base_created_at).toBe("string");
    expect(messageCalls(calls)).toHaveLength(0);
  });

  it("このまま・不要は判断メモに載る（判断の契約は増やさない）", async () => {
    const { fetchImpl, calls } = recordingFetch((url) =>
      url.endsWith("/consultation/judgment")
        ? jsonResponse(entryPlain)
        : jsonResponse(payloadWithState(undefined, entryWithPanels)),
    );
    renderPanel(fetchImpl);
    await screen.findByTestId("consultation-storyboard");

    fireEvent.click(screen.getByTestId("panel-a-1-keep"));
    expect(screen.getByTestId("panel-a-1-choice").textContent).toContain(
      "このまま採用の希望を記録します",
    );
    fireEvent.click(screen.getByTestId("panel-a-9-drop"));
    fireEvent.click(screen.getByTestId("consultation-judgment-adopt"));
    fireEvent.click(screen.getByTestId("consultation-judgment-submit"));

    await waitFor(() => {
      expect(calls.filter((call) => call.url.endsWith("/consultation/judgment"))).toHaveLength(
        1,
      );
    });
    const judgmentCalls = calls.filter((call) =>
      call.url.endsWith("/consultation/judgment"),
    );
    const body = JSON.parse(String(judgmentCalls[0]!.init.body)) as Record<string, unknown>;
    expect(body["decision"]).toBe("adopt");
    expect(String(body["note"])).toContain("パネルa-1はこのまま採用の希望");
    expect(String(body["note"])).toContain("パネルa-9は不要の希望");
    expect(Object.keys(body).sort()).toEqual(
      ["consultation_id", "decision", "note", "proposal_id", "scope"].sort(),
    );
  });
});

describe("工程4 拒否と型付きエラー", () => {
  it("refused_reason は正直な1行になる", async () => {
    const { fetchImpl } = recordingFetch(() =>
      jsonResponse(
        payloadWithState({
          available: false,
          model_verified: false,
          model_selected: null,
          refused_reason: "モデルの確認が済んでいません",
        }),
      ),
    );
    renderPanel(fetchImpl);
    await screen.findByTestId("consultation-proposal");

    fireEvent.click(screen.getByTestId("generation-section-toggle"));
    const line = screen.getByTestId("consultation-generation-refused").textContent ?? "";
    expect(line).toContain("モデルの確認が済んでいません");
    expect(line).toContain("0生成で続行");
  });

  it.each([
    ["generation-model-unverified", "確認が済んでいません"],
    ["generation-budget-exhausted", "上限に達しました"],
    ["generation-stale", "内容が古いため"],
  ])("%s は正直な行を出し、送り直しの嵐を起こさない", async (code, wording) => {
    const { fetchImpl, calls } = recordingFetch((url) =>
      url.endsWith("/consultation/message")
        ? jsonResponse({ error: { code, detail: "no stock" } }, 422)
        : jsonResponse(payloadPlain),
    );
    renderPanel(fetchImpl);
    await screen.findByTestId("consultation-proposal");

    fireEvent.click(screen.getByTestId("generation-section-toggle"));
    fireEvent.change(screen.getByTestId("consultation-message-input"), {
      target: { value: "絵コンテも見たい" },
    });
    fireEvent.click(screen.getByTestId("generation-request"));

    await waitFor(() => {
      expect(screen.getByTestId("consultation-generation-error")).toBeVisible();
    });
    const line = screen.getByTestId("consultation-generation-error").textContent ?? "";
    expect(line).toContain(wording);
    expect(line).toContain("0生成で続行");
    expect(line).toContain(code);
    const sent = messageCalls(calls).length;
    expect(sent).toBe(1);
    await new Promise((resolve) => setTimeout(resolve, 2100));
    expect(messageCalls(calls)).toHaveLength(sent);
  });

  it("旧データは何も増やさず今日どおりに表示する", async () => {
    const { fetchImpl } = recordingFetch(() => jsonResponse(payloadPlain));
    const { container } = renderPanel(fetchImpl);
    await screen.findByTestId("consultation-proposal");

    expect(container.querySelector('[data-testid="consultation-storyboard"]')).toBeNull();
    expect(
      container.querySelector('[data-testid="consultation-generation-refused"]'),
    ).toBeNull();
    expect(
      container.querySelector('[data-testid="consultation-generation-error"]'),
    ).toBeNull();
    expect(screen.getByTestId("consultation-budget")).toBeVisible();
  });
});
