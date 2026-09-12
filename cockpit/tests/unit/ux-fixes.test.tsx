import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

import TaskProgress from "@/components/TaskProgress";
import ConsultationPanel from "@/components/ConsultationPanel";
import IntakeForm from "@/components/IntakeForm";
import RemakeEpisodeButton from "@/components/RemakeEpisodeButton";
import EpisodeView from "@/components/EpisodeView";
import type {
  Consultation,
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

function recordingFetch(
  responseFor: (url: string, init: RequestInit) => Response | Promise<Response>,
): { fetchImpl: typeof fetch; calls: { url: string; init: RequestInit }[] } {
  const calls: { url: string; init: RequestInit }[] = [];
  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const requestInit = init ?? {};
    calls.push({ url, init: requestInit });
    return responseFor(url, requestInit);
  }) as typeof fetch;
  return { fetchImpl, calls };
}

function stageStatus(stage: string, status = "ANALYZED"): EpisodeStatus {
  return {
    episode_id: "ep-ux",
    job_id: "ep-ux",
    status,
    current_stage: stage,
    created_at_seq: 1,
    updated_at_seq: 1,
    stage_runs: [],
  };
}

const budget = {
  llm_calls_used: 1,
  llm_calls_limit: 10,
  intervals_used: 1,
  intervals_limit: 6,
  wall_seconds_used: 30,
  wall_seconds_limit: 600,
  cost_display: "費用は直接計測できません（回数で管理）",
};

const proposal: ConsultationProposal = {
  proposal_id: "p-1",
  title: "引きから見せる構成",
  summary: "引きの映像から始める構成です。",
  details: {
    audience_message: "手間が減ることを伝えます。",
    structure: "引き → まとめ",
    duration_estimate: "45秒前後",
    candidate_scenes: ["冒頭の空撮"],
    subtitle_policy: "短く区切る",
    audio_policy: "いつもの選曲",
    tempo_policy: "前半は速め",
    reference_mapping: "ep12を踏襲",
    unused_reasons: "玄関は使いません",
    unconfirmed: [],
  },
};

const entryWithProposal: Consultation = {
  consultation_id: "c-1",
  created_at: "2026-09-12T10:00:00Z",
  message: "冒頭の見せ方を相談したい",
  proposals: [proposal],
  judgments: [],
  budget,
};

const adoptedPolicy = {
  consultation_id: "c-1",
  judgment_id: "j-1",
  proposal_id: "p-1",
  decision: "adopt" as const,
  scope: { composition: true, appearance: false, audio: true },
  audience_message: "",
  structure: "",
  duration_estimate: "",
  candidate_scenes: [],
  subtitle_policy: "",
  audio_policy: "",
  tempo_policy: "",
  reference_mapping: "",
  unused_reasons: "",
  unconfirmed: [],
  note: "",
};

const judgedEntry: Consultation = {
  ...entryWithProposal,
  judgments: [
    {
      judgment_id: "j-1",
      proposal_id: "p-1",
      decision: "adopt",
      scope: { composition: true, appearance: false, audio: true },
      note: null,
      created_at: "2026-09-12T10:05:00Z",
    },
  ],
};

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("TaskProgress（共有の待ち表示）", () => {
  it("動くバー・作業名・経過mm:ssを出し、%は出さない", () => {
    const now = Date.now();
    render(<TaskProgress taskName="AIに相談中" startedAt={now - 65_000} now={now} />);
    expect(screen.getByTestId("task-progress-bar")).toBeVisible();
    expect(screen.getByTestId("task-progress-name").textContent).toBe("AIに相談中");
    expect(screen.getByTestId("task-progress-elapsed").textContent).toBe("01:05");
    expect(screen.getByTestId("task-progress").textContent).not.toContain("%");
  });

  it("技術詳細は「詳しく見る」の後に置く", () => {
    const now = Date.now();
    render(
      <TaskProgress
        taskName="プレビューを準備中"
        startedAt={now}
        now={now}
        details={<p>run-123</p>}
      />,
    );
    expect(screen.getByText("詳しく見る")).toBeVisible();
    expect(screen.getByText("run-123")).not.toBeVisible();
  });
});

describe("ConsultationPanel（初期表示と言語）", () => {
  it("初期は希望入力だけを示し、矛盾する副文・提案欄は出さない", async () => {
    const { fetchImpl } = recordingFetch(() => jsonResponse({ consultations: [] }));
    render(
      <ConsultationPanel
        episodeId="ep-ux"
        status={stageStatus("selection")}
        fetchImpl={fetchImpl}
      />,
    );
    await waitFor(() => {
      expect(screen.getByTestId("consultation-message-input")).toBeVisible();
    });
    expect(screen.getByText("どんな雰囲気にしたいですか？")).toBeVisible();
    expect(screen.getByTestId("consultation-send").textContent).toBe("希望を伝える");
    expect(
      screen.queryByText("提案の中から選ぶか、言葉で直してください。"),
    ).toBeNull();
    expect(screen.queryByTestId("consultation-proposals")).toBeNull();
    expect(screen.queryByTestId("consultation-proposal")).toBeNull();
  });

  it("応答が届くまでは「AIの提案」欄を出さない。届いたら出す", async () => {
    const { fetchImpl } = recordingFetch(() =>
      jsonResponse({ consultations: [entryWithProposal] } as ConsultationPayload),
    );
    render(
      <ConsultationPanel
        episodeId="ep-ux"
        status={stageStatus("selection")}
        fetchImpl={fetchImpl}
      />,
    );
    const section = await screen.findByTestId("consultation-proposals");
    expect(section).toBeVisible();
    expect(section.textContent).toContain("AIの提案");
    expect(screen.getByTestId("consultation-proposal")).toBeVisible();
  });

  it("送信中は共有の待ち表示（AIに相談中・経過）が出る", async () => {
    let releasePost!: (value: Response) => void;
    const gate = new Promise<Response>((resolve) => {
      releasePost = resolve;
    });
    const { fetchImpl } = recordingFetch((url) => {
      if (url.endsWith("/consultation/message")) return gate;
      return jsonResponse({ consultations: [] });
    });
    render(
      <ConsultationPanel
        episodeId="ep-ux"
        status={stageStatus("selection")}
        fetchImpl={fetchImpl}
      />,
    );
    await waitFor(() => {
      expect(screen.getByTestId("consultation-message-input")).toBeVisible();
    });
    fireEvent.change(screen.getByTestId("consultation-message-input"), {
      target: { value: "冒頭を引きから" },
    });
    fireEvent.click(screen.getByTestId("consultation-send"));
    await waitFor(() => {
      expect(screen.getByTestId("task-progress")).toBeVisible();
    });
    expect(screen.getByTestId("task-progress-name").textContent).toBe("AIに相談中");
    expect(
      screen.getByTestId("task-progress").textContent,
    ).not.toContain("%");
    releasePost(
      jsonResponse({
        consultation_id: "c-9",
        created_at: "2026-09-12T10:00:00Z",
        message: "冒頭を引きから",
        proposals: [],
        judgments: [],
        budget,
      }),
    );
    await waitFor(() => {
      expect(screen.queryByTestId("task-progress")).toBeNull();
    });
  });
});

describe("試し動画ボタンのgating（E）", () => {
  function adoptedFetch() {
    return recordingFetch((url: string) => {
      if (url.endsWith("/consultation/samples")) {
        return jsonResponse({ samples: [] });
      }
      return jsonResponse({
        consultations: [{ ...judgedEntry, policy: { adopted: adoptedPolicy } }],
      } as ConsultationPayload);
    });
  }

  it("準備前はボタンを隠し「短い試し動画を準備します」を出す", async () => {
    const { fetchImpl } = adoptedFetch();
    render(
      <ConsultationPanel
        episodeId="ep-ux"
        status={stageStatus("selection")}
        fetchImpl={fetchImpl}
      />,
    );
    await waitFor(() => {
      expect(screen.getByTestId("sample-preparing")).toBeVisible();
    });
    expect(screen.queryByTestId("sample-request")).toBeNull();
    expect(screen.getByTestId("sample-preparing").textContent).toContain(
      "短い試し動画を準備します",
    );
  });

  it("準備ができたら「できたらここで確認できます」とボタンを出す", async () => {
    const { fetchImpl } = adoptedFetch();
    render(
      <ConsultationPanel
        episodeId="ep-ux"
        status={stageStatus("selection", "PREVIEW_READY")}
        fetchImpl={fetchImpl}
      />,
    );
    await waitFor(() => {
      expect(screen.getByTestId("sample-request")).toBeVisible();
    });
    expect(screen.getByTestId("sample-ready-line").textContent).toContain(
      "できたらここで確認できます",
    );
  });
});

describe("IntakeForm（編集許可の明示 F）", () => {
  function fill(id: string, value: string) {
    fireEvent.change(screen.getByLabelText(id), { target: { value } });
  }

  it("同意チェックと言葉（音声・映像の一部）を示し、未同意では短い注意を出す", () => {
    render(<IntakeForm />);
    const check = screen.getByTestId("editorial-grant") as HTMLInputElement;
    expect(check.checked).toBe(false);
    expect(
      screen.getByText(
        "映像の解析と編集判断のために、素材の音声・映像の一部を外部のAIサービスへ送信して処理することに同意する",
      ),
    ).toBeVisible();
    expect(screen.getByTestId("editorial-grant-hint").textContent).toContain(
      "編集判断にはこの同意が必要です",
    );
    fireEvent.click(check);
    expect(check.checked).toBe(true);
    expect(screen.queryByTestId("editorial-grant-hint")).toBeNull();
  });

  it("同意ありではeditorial_grant付きで作成する", async () => {
    render(<IntakeForm />);
    fill("撮影素材のフォルダを選ぶ", "/tmp/a");
    fill("どんな動画にしたいですか？", "テストbrief");
    fireEvent.click(screen.getByTestId("editorial-grant"));
    const bodies: unknown[] = [];
    const fetchMock = vi.fn().mockImplementation((_url: unknown, init?: RequestInit) => {
      bodies.push(JSON.parse(String(init?.body)));
      return Promise.resolve(
        new Response(
          JSON.stringify({
            episode_id: "ep-ok",
            job_id: "ep-ok",
            status: "CREATED",
            brief_status: "draft",
          }),
          { status: 200 },
        ),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    fireEvent.click(screen.getByTestId("create-button"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const sent = bodies[0] as Record<string, unknown>;
    expect(sent["source_folder"]).toBe("/tmp/a");
    expect(sent["editorial_grant"]).toEqual({
      granted: true,
      data_class: "transcript",
      stage: "editorial_direct",
      note: "",
    });
  });

  it("未同意ではgrantなしで作成する（注意は表示済み）", async () => {
    render(<IntakeForm />);
    fill("撮影素材のフォルダを選ぶ", "/tmp/a");
    fill("どんな動画にしたいですか？", "テストbrief");
    expect(screen.getByTestId("editorial-grant-hint")).toBeVisible();
    const bodies: unknown[] = [];
    const fetchMock = vi.fn().mockImplementation((_url: unknown, init?: RequestInit) => {
      bodies.push(JSON.parse(String(init?.body)));
      return Promise.resolve(
        new Response(
          JSON.stringify({
            episode_id: "ep-ok",
            job_id: "ep-ok",
            status: "CREATED",
            brief_status: "draft",
          }),
          { status: 200 },
        ),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    fireEvent.click(screen.getByTestId("create-button"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const sent = bodies[0] as Record<string, unknown>;
    expect("editorial_grant" in sent).toBe(false);
  });
});

describe("RemakeEpisodeButton（G-UI）", () => {
  it("素材フォルダ不明では/new-episodeへの plain リンクになる", () => {
    render(<RemakeEpisodeButton episodeId="ep-ux" sourceFolder={null} />);
    const link = screen.getByTestId("episode-remake");
    expect(link.textContent).toContain("同じ素材で最初から作り直す");
    expect(link.getAttribute("href")).toBe("/new-episode");
  });

  it("同じ素材でcreateEpisodeを呼び、重複拒否は共有エラー表示＋正直な一行になる", async () => {
    const { fetchImpl, calls } = recordingFetch((url: string, init: RequestInit) => {
      if (url.endsWith("/brief")) {
        return jsonResponse({
          episode_id: "ep-ux",
          brief_text: "元のbrief",
          status: "draft",
        });
      }
      return jsonResponse(
        { error: { code: "episode-exists", detail: "already exists" } },
        409,
      );
    });
    render(
      <RemakeEpisodeButton episodeId="ep-ux" sourceFolder="/tmp/a" fetchImpl={fetchImpl} />,
    );
    fireEvent.click(screen.getByTestId("episode-remake"));
    await waitFor(() => {
      expect(screen.getByTestId("error-notice")).toBeVisible();
    });
    const posts = calls.filter((call) => call.url.endsWith("/episodes"));
    expect(posts).toHaveLength(1);
    expect(JSON.parse(String(posts[0]!.init.body))).toEqual({
      source_folder: "/tmp/a",
      brief_text: "元のbrief",
    });
    expect(screen.getByTestId("episode-remake-duplicate").textContent).toContain(
      "同じ素材の作り直しはまだ受け付けていません",
    );
    expect(screen.queryByText(/削除/)).toBeNull();
  });
});

describe("EpisodeView（H: preview待ち→自動遷移 / G-UI: 作り直し表示）", () => {
  it("待ち表示は共有コンポーネントを使い、利用可能になれば再生画面へ自動遷移する", async () => {
    const scrolled: string[] = [];
    const proto = window.HTMLElement.prototype as unknown as Record<string, unknown>;
    const original = proto["scrollIntoView"];
    proto["scrollIntoView"] = vi.fn(function (this: unknown) {
      scrolled.push(String((this as Element).getAttribute?.("data-testid") ?? ""));
    });
    try {
      let polls = 0;
      let probes = 0;
      const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
        const url = String(input);
        if (url.includes("/consultation")) {
          return Promise.resolve(jsonResponse({ consultations: [] }));
        }
        if (url.endsWith("/flags")) {
          return Promise.resolve(
            jsonResponse({ flags: [], not_yet_generated: true }),
          );
        }
        if (url.endsWith("/outputs")) {
          return Promise.resolve(new Response("{}", { status: 404 }));
        }
        if (url.includes("/preview")) {
          probes += 1;
          return Promise.resolve(
            probes <= 1 ? new Response(null, { status: 404 }) : new Response(null, { status: 200 }),
          );
        }
        polls += 1;
        return Promise.resolve(
          jsonResponse({
            episode_id: "ep-ux",
            job_id: "ep-ux",
            status: "ANALYZED",
            current_stage: "selection",
            created_at_seq: 1,
            updated_at_seq: polls,
            stage_runs: [],
          }),
        );
      });
      vi.stubGlobal("fetch", fetchMock);

      render(<EpisodeView episodeId="ep-ux" />);

      await waitFor(() => {
        expect(screen.getByTestId("preview-pending")).toBeVisible();
      });
      expect(
        screen.getByTestId("preview-pending").querySelector('[data-testid="task-progress"]'),
      ).not.toBeNull();
      expect(screen.getByTestId("episode-remake")).toBeVisible();

      await waitFor(
        () => {
          expect(screen.getByTestId("preview-player")).toBeVisible();
        },
        { timeout: 8000 },
      );
      expect(scrolled).toContain("preview-player-anchor");
    } finally {
      proto["scrollIntoView"] = original;
    }
  });
});
