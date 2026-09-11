import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import ConsultationPanel from "@/components/ConsultationPanel";
import type {
  Consultation,
  ConsultationPayload,
  ConsultationPolicyOutcomeEntry,
  ConsultationProposal,
  ConsultationJudgment,
  EpisodeStatus,
} from "@/lib/api";
import type { StageHint } from "@/lib/stage-steps";

/**
 * UX 2.5 slice-1 component tests against the pinned contract: GET lists
 * entries (budget INSIDE each entry), each POST returns the updated
 * single entry. 要約が主表示・詳細は折りたたみ、判断は選ぶまで送れない、
 * 範囲チェックはpayloadに載る、予算は累計、型付きエラーは正直な表示
 * （偽の再試行スピナーなし）、15秒更新途切れで専用banner。
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
    episode_id: "ep-c01",
    job_id: "ep-c01",
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

const proposal: ConsultationProposal = {
  proposal_id: "p-1",
  title: "冒頭から引きで見せる構成",
  summary:
    "いつものシリーズらしく、引きの映像から始めて課題を提示してから解決へ進む構成です。",
  details: {
    audience_message: "台所収納の工夫で毎日の手間が減ることを伝えます。",
    structure: "引き → Before → 改造 → After → まとめ",
    duration_estimate: "45秒前後",
    candidate_scenes: ["冒頭の空撮", "シンク前のBefore"],
    subtitle_policy: "短く区切って読みやすく",
    audio_policy: "BGMはいつもの選曲",
    tempo_policy: "前半は速め、まとめはゆっくり",
    reference_mapping: "ep12の冒頭構成を踏襲する",
    unused_reasons: "玄関のカットは主題から離れるため使いません",
    unconfirmed: ["Afterの撮影が終わっているかは未確認"],
  },
};

const recordedJudgment: ConsultationJudgment = {
  judgment_id: "j-1",
  proposal_id: "p-1",
  decision: "adopt",
  scope: { composition: true, appearance: false, audio: true },
  note: "字幕は減らす",
  created_at: "2026-09-08T10:05:00Z",
};

const entryOne: Consultation = {
  consultation_id: "c-1",
  created_at: "2026-09-08T10:00:00Z",
  message: "冒頭の見せ方を相談したい",
  proposals: [proposal],
  judgments: [],
  budget,
};

const entryOneJudged: Consultation = {
  ...entryOne,
  judgments: [recordedJudgment],
};

const entrySent: Consultation = {
  consultation_id: "c-1",
  created_at: "2026-09-08T10:00:00Z",
  message: "冒頭を引きから",
  proposals: [],
  judgments: [],
  budget,
};

const payloadOne: ConsultationPayload = { consultations: [entryOne] };

describe("ConsultationPanel — 全編許可後も新しい採用は独自の試し動画段階を持つ", () => {
  const authJudgment: ConsultationJudgment = {
    judgment_id: "j-auth",
    proposal_id: null,
    decision: "full_authorized",
    scope: { composition: true, appearance: true, audio: true },
    note: null,
    created_at: "2026-09-08T10:12:00Z",
  };
  const newAdoptJudgment: ConsultationJudgment = {
    judgment_id: "j-2",
    proposal_id: "p-1",
    decision: "adopt",
    scope: { composition: true, appearance: false, audio: true },
    note: "別案も試す",
    created_at: "2026-09-08T10:15:00Z",
  };

  it("adopt→許可の後は採用済み方針だけを表示する", async () => {
    const { fetchImpl } = recordingFetch(() =>
      jsonResponse({
        consultations: [
          { ...entryOneJudged, judgments: [recordedJudgment, authJudgment] },
        ],
      }),
    );
    renderPanel(fetchImpl);
    await screen.findByText("採用した方針");
    expect(screen.queryByTestId("sample-request")).toBeNull();
  });

  it("許可後の新しい採用は試し動画段階に戻る（古い許可は新方針を認めない）", async () => {
    // backendのviewは新しい採用をpolicy.adoptedとして返す（取り下げではない
    // full_authorizedを読み飛ばす——製品の実挙動に合わせたfixture）
    const newAdoptedPolicy = {
      consultation_id: "c-1",
      judgment_id: "j-2",
      proposal_id: "p-1",
      decision: "adopt",
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
    const { fetchImpl } = recordingFetch((url: string) => {
      if (url.endsWith("/consultation/samples")) {
        return jsonResponse({ samples: [] });
      }
      return jsonResponse({
        consultations: [
          {
            ...entryOneJudged,
            judgments: [recordedJudgment, authJudgment, newAdoptJudgment],
            policy: { adopted: newAdoptedPolicy },
          },
        ],
      });
    });
    renderPanel(fetchImpl);
    await waitFor(() => {
      expect(screen.getByTestId("sample-request")).toBeVisible();
    });
    expect(screen.getByTestId("consultation-panel")).toBeVisible();
  });
});
const payloadOneJudged: ConsultationPayload = { consultations: [entryOneJudged] };
const payloadEmpty: ConsultationPayload = { consultations: [] };

function renderPanel(fetchImpl: typeof fetch) {
  return render(
    <ConsultationPanel
      episodeId="ep-c01"
      status={stageStatus("selection")}
      fetchImpl={fetchImpl}
    />,
  );
}

afterEach(() => {
  vi.useRealTimers();
});

describe("ConsultationPanel（UX 2.5 slice-1）", () => {
  it("試し編集段階（compile）でも相談を表示する（再構成後: 流れはcompile以降も続く）", () => {
    const { fetchImpl, calls } = recordingFetch(() => jsonResponse(payloadOne));
    render(
      <ConsultationPanel
        episodeId="ep-c01"
        status={stageStatus("compile")}
        fetchImpl={fetchImpl}
      />,
    );
    expect(screen.getByTestId("consultation-panel")).not.toBeNull();
    expect(calls.length).toBeGreaterThan(0);
  });

  it("status未取得・review段階では出さず、その間はfetchもしない", () => {
    const { fetchImpl, calls } = recordingFetch(() => jsonResponse(payloadOne));
    const noStatus = render(
      <ConsultationPanel episodeId="ep-c01" status={null} fetchImpl={fetchImpl} />,
    );
    expect(noStatus.container.querySelector('[data-testid="consultation-panel"]')).toBeNull();
    const review = render(
      <ConsultationPanel
        episodeId="ep-c01"
        status={stageStatus("review")}
        fetchImpl={fetchImpl}
      />,
    );
    expect(review.container.querySelector('[data-testid="consultation-panel"]')).toBeNull();
    expect(calls).toHaveLength(0);
  });

  it("plan確定前の段階（selection）では表示し、記録を読み込む", async () => {
    const { fetchImpl } = recordingFetch(() => jsonResponse(payloadOne));
    renderPanel(fetchImpl);
    await waitFor(() => {
      expect(screen.getByTestId("consultation-panel")).toBeVisible();
    });
    expect(screen.getByTestId("consultation-entry")).toBeVisible();
    expect(screen.getByTestId("consultation-entry-message").textContent).toContain(
      "冒頭の見せ方を相談したい",
    );
  });

  it("提案は要約が主表示、詳細は<details>を開くまで閉じている", async () => {
    const { fetchImpl } = recordingFetch(() => jsonResponse(payloadOne));
    renderPanel(fetchImpl);

    const cards = await screen.findAllByTestId("consultation-proposal");
    expect(cards).toHaveLength(1);
    expect(screen.getByTestId("consultation-proposal-title").textContent).toBe(
      "AIの提案: 冒頭から引きで見せる構成",
    );
    expect(screen.getByTestId("consultation-proposal-summary").textContent).toContain(
      "いつものシリーズらしく",
    );

    const details = screen.getByTestId("consultation-proposal-details");
    expect(details.hasAttribute("open")).toBe(false);

    fireEvent.click(screen.getByText("詳しい内容を見る"));
    expect(details.hasAttribute("open")).toBe(true);
    expect(screen.getByTestId("consultation-proposal-duration").textContent).toBe("45秒前後");
    expect(screen.getByText("冒頭の空撮")).toBeVisible();
    expect(screen.getByText("ep12の冒頭構成を踏襲する")).toBeVisible();
    expect(screen.getByText("確認できていないこと")).toBeVisible();
    expect(screen.getByText("Afterの撮影が終わっているかは未確認")).toBeVisible();
  });

  it("判断を選ぶまで送信できず、採用は方針の採用だと名乗る。payloadにdecisionが入る", async () => {
    const { fetchImpl, calls } = recordingFetch((url) =>
      url.endsWith("/consultation/judgment")
        ? jsonResponse(entryOne)
        : jsonResponse(payloadOne),
    );
    renderPanel(fetchImpl);
    await screen.findAllByTestId("consultation-proposal");

    const submit = screen.getByTestId("consultation-judgment-submit") as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    expect(screen.getByTestId("consultation-judgment-gate").textContent).toContain(
      "判断を選ぶまで送信できません",
    );

    fireEvent.click(screen.getByTestId("consultation-judgment-adopt"));
    expect(submit.disabled).toBe(false);
    expect(screen.getByTestId("consultation-adopt-note").textContent).toContain(
      "最終動画や公開の承認ではありません",
    );

    fireEvent.click(submit);
    await waitFor(() => {
      const judgmentCalls = calls.filter((call) =>
        call.url.endsWith("/consultation/judgment"),
      );
      expect(judgmentCalls).toHaveLength(1);
      expect(JSON.parse(String(judgmentCalls[0]!.init.body))).toEqual({
        consultation_id: "c-1",
        proposal_id: "p-1",
        decision: "adopt",
        scope: { composition: true, appearance: true, audio: true },
        note: null,
      });
    });
  });

  it("scopeのチェックを外すとpayloadのscopeに反映され、メモも入る", async () => {
    const { fetchImpl, calls } = recordingFetch((url) =>
      url.endsWith("/consultation/judgment")
        ? jsonResponse(entryOne)
        : jsonResponse(payloadOne),
    );
    renderPanel(fetchImpl);
    await screen.findAllByTestId("consultation-proposal");

    fireEvent.click(screen.getByTestId("consultation-scope-audio"));
    fireEvent.click(screen.getByTestId("consultation-judgment-revise"));
    fireEvent.change(screen.getByTestId("consultation-judgment-note"), {
      target: { value: " 字幕を減らして " },
    });
    fireEvent.click(screen.getByTestId("consultation-judgment-submit"));

    await waitFor(() => {
      const judgmentCalls = calls.filter((call) =>
        call.url.endsWith("/consultation/judgment"),
      );
      expect(judgmentCalls).toHaveLength(1);
      expect(JSON.parse(String(judgmentCalls[0]!.init.body))).toEqual({
        consultation_id: "c-1",
        proposal_id: "p-1",
        decision: "revise",
        scope: { composition: true, appearance: true, audio: false },
        note: "字幕を減らして",
      });
    });
  });

  it("記録済みの判断が表示される（判断の範囲とメモ込み）", async () => {
    const { fetchImpl } = recordingFetch(() => jsonResponse(payloadOneJudged));
    renderPanel(fetchImpl);

    const recorded = await screen.findAllByTestId("consultation-judgment-recorded");
    expect(recorded).toHaveLength(1);
    expect(recorded[0]!.textContent).toContain("この方針を採用");
    expect(recorded[0]!.textContent).toContain("構成・音声");
    expect(recorded[0]!.textContent).toContain("メモ: 字幕は減らす");
  });

  it("budgetは通常表示しない。残りわずかで1行警告、数値は詳細に置く", async () => {
    const { fetchImpl } = recordingFetch(() => jsonResponse(payloadOne));
    renderPanel(fetchImpl);

    await waitFor(() => {
      expect(screen.getByTestId("consultation-proposal")).toBeVisible();
    });
    // 残り7回なので警告も数値ダッシュボードも通常画面に出ない
    expect(screen.queryByTestId("consultation-budget-warning")).toBeNull();
    expect(screen.queryByTestId("consultation-budget-details")).toBeNull();
    expect(screen.queryByTestId("consultation-budget")).toBeNull();
    expect(screen.queryByTestId("consultation-budget-llm-calls")).toBeNull();
  });

  it("相談がまだ無い間は予算の数値を出さない（実データのない数字は作らない）", async () => {
    const { fetchImpl } = recordingFetch(() => jsonResponse(payloadEmpty));
    const { container } = renderPanel(fetchImpl);

    await waitFor(() => {
      expect(screen.getByTestId("consultation-empty")).toBeVisible();
    });
    expect(container.querySelector('[data-testid="consultation-budget"]')).toBeNull();
  });

  it("consultation-budget-exhaustedは格下げ告知を出し、総称エラーや擬似リトライを出さない", async () => {
    const { fetchImpl } = recordingFetch((url) =>
      url.endsWith("/consultation/message")
        ? jsonResponse(
            { error: { code: "consultation-budget-exhausted", detail: "limit" } },
            429,
          )
        : jsonResponse(payloadEmpty),
    );
    renderPanel(fetchImpl);

    await waitFor(() => {
      expect(screen.getByTestId("consultation-empty")).toBeVisible();
    });
    fireEvent.change(screen.getByTestId("consultation-message-input"), {
      target: { value: "冒頭を引きから" },
    });
    fireEvent.click(screen.getByTestId("consultation-send"));

    await waitFor(() => {
      expect(screen.getByTestId("consultation-budget-exhausted")).toBeVisible();
    });
    const notice = screen.getByTestId("consultation-budget-exhausted").textContent ?? "";
    expect(notice).toContain("上限に達しました");
    expect(notice).toContain("文章のみ");
    expect(notice).toContain("リセットされません");
    expect(notice).toContain("送り直しても回復しません");
    expect(screen.queryByTestId("error-notice")).toBeNull();
    expect(screen.queryByText("送信中…")).toBeNull();
    const send = screen.getByTestId("consultation-send") as HTMLButtonElement;
    expect(send.textContent).toBe("希望を伝える");
  });

  it("consultation-llm-unavailableは本番用AI実行環境が必要だと正直に言う", async () => {
    const { fetchImpl } = recordingFetch((url) =>
      url.endsWith("/consultation/message")
        ? jsonResponse(
            { error: { code: "consultation-llm-unavailable", detail: "runtime down" } },
            503,
          )
        : jsonResponse(payloadEmpty),
    );
    renderPanel(fetchImpl);

    await waitFor(() => {
      expect(screen.getByTestId("consultation-empty")).toBeVisible();
    });
    fireEvent.change(screen.getByTestId("consultation-message-input"), {
      target: { value: "冒頭を引きから" },
    });
    fireEvent.click(screen.getByTestId("consultation-send"));

    await waitFor(() => {
      expect(screen.getByTestId("consultation-llm-unavailable")).toBeVisible();
    });
    expect(
      screen.getByTestId("consultation-llm-unavailable").textContent,
    ).toContain("本番用AIの実行環境が必要です");
    expect(screen.queryByTestId("error-notice")).toBeNull();
  });

  it("15秒更新途切れで専用banner、focusで即時再照会、復帰で消える", async () => {
    vi.useFakeTimers();
    let failing = false;
    let getCalls = 0;
    const fetchImpl = (async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/consultation/message")) return jsonResponse(entrySent);
      if (url.endsWith("/consultation")) {
        getCalls += 1;
        if (failing) throw new TypeError("network down");
        return jsonResponse(payloadOne);
      }
      return jsonResponse(payloadEmpty);
    }) as typeof fetch;

    renderPanel(fetchImpl);
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.queryByTestId("consultation-stale-banner")).toBeNull();

    failing = true;
    await vi.advanceTimersByTimeAsync(16000);
    const banner = screen.getByTestId("consultation-stale-banner");
    expect(banner.textContent).toContain("途切れています");
    expect(banner.textContent).toContain("再照会");

    const before = getCalls;
    window.dispatchEvent(new Event("focus"));
    await vi.advanceTimersByTimeAsync(0);
    expect(getCalls).toBeGreaterThan(before);

    failing = false;
    await vi.advanceTimersByTimeAsync(3000);
    expect(screen.queryByTestId("consultation-stale-banner")).toBeNull();
  });

  it("相談を送ると記録が出て、提案が届けばaria-liveで告知する", async () => {
    vi.useFakeTimers();
    let poll = 0;
    const fetchImpl = (async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/consultation/message")) return jsonResponse(entrySent);
      if (url.endsWith("/consultation")) {
        poll += 1;
        return jsonResponse(poll === 1 ? payloadEmpty : payloadOne);
      }
      return jsonResponse(payloadEmpty);
    }) as typeof fetch;

    renderPanel(fetchImpl);
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.getByTestId("consultation-empty")).toBeVisible();

    fireEvent.change(screen.getByTestId("consultation-message-input"), {
      target: { value: "冒頭を引きから" },
    });
    fireEvent.click(screen.getByTestId("consultation-send"));
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.getByTestId("consultation-entry-message").textContent).toContain(
      "冒頭を引きから",
    );

    await vi.advanceTimersByTimeAsync(2000);
    expect(screen.getByTestId("consultation-announcement").textContent).toContain(
      "新しい提案が届きました",
    );
    expect(screen.getAllByTestId("consultation-proposal")).toHaveLength(1);
  });
});

describe("ConsultationPanel（UX 2.5 slice-2：採用→再編集の反映）", () => {
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

  const legacyHonoredOutcome: ConsultationPolicyOutcomeEntry = {
    kind: "policy_outcome",
    outcome_id: "o-1",
    consultation_id: "c-1",
    judgment_id: "j-1",
    proposal_id: "p-1",
    plan_version: "v3",
    status: "honored",
    reasons: [],
    note: null,
    recorded_at: "2026-09-08T10:10:00Z",
  };

  const failedOutcome: ConsultationPolicyOutcomeEntry = {
    kind: "policy_outcome",
    outcome_id: "o-2",
    consultation_id: "c-1",
    judgment_id: "j-2",
    proposal_id: "p-1",
    plan_version: null,
    status: "failed",
    reasons: ["対象の版が見つかりませんでした", "方針の範囲が空でした"],
    note: null,
    recorded_at: "2026-09-08T10:11:00Z",
  };

  const connectedOutcome: ConsultationPolicyOutcomeEntry = {
    kind: "policy_outcome",
    outcome_id: "o-3",
    consultation_id: "c-1",
    judgment_id: "j-3",
    proposal_id: "p-1",
    plan_version: "v4",
    status: "connected",
    reasons: [],
    note: null,
    recorded_at: "2026-09-08T10:12:00Z",
    director_connection: "confirmed",
    realized_checks: ["planner_feasibility", "edit_plan_generation"],
    unaddressed: ["字幕と見た目が実映像で方針どおりか"],
    unconfirmed: ["結末の方針どおりか"],
  };

  const failedConfirmedOutcome: ConsultationPolicyOutcomeEntry = {
    kind: "policy_outcome",
    outcome_id: "o-4",
    consultation_id: "c-1",
    judgment_id: "j-4",
    proposal_id: "p-1",
    plan_version: null,
    status: "failed",
    reasons: ["policy-commit-failed: 版の確定に失敗しました"],
    note: null,
    recorded_at: "2026-09-08T10:13:00Z",
    director_connection: "confirmed",
  };

  const failedNotStartedOutcome: ConsultationPolicyOutcomeEntry = {
    kind: "policy_outcome",
    outcome_id: "o-5",
    consultation_id: "c-1",
    judgment_id: "j-5",
    proposal_id: "p-1",
    plan_version: null,
    status: "failed",
    reasons: ["policy-not-interpretable: 編集長が決定論化モードのため方針を解釈できませんでした"],
    note: null,
    recorded_at: "2026-09-08T10:14:00Z",
    director_connection: "not_started",
  };

  const failedUnknownOutcome: ConsultationPolicyOutcomeEntry = {
    kind: "policy_outcome",
    outcome_id: "o-6",
    consultation_id: "c-1",
    judgment_id: "j-6",
    proposal_id: "p-1",
    plan_version: null,
    status: "failed",
    reasons: ["director_timeout: 応答がありませんでした"],
    note: null,
    recorded_at: "2026-09-08T10:15:00Z",
    director_connection: "unknown",
  };

  const failedRecordedVersionOutcome: ConsultationPolicyOutcomeEntry = {
    kind: "policy_outcome",
    outcome_id: "o-7",
    consultation_id: "c-1",
    judgment_id: "j-7",
    proposal_id: "p-1",
    plan_version: "v2",
    status: "failed",
    reasons: ["policy-commit-recovery-failed: 版ファイルの回復を確認できません"],
    note: null,
    recorded_at: "2026-09-08T10:16:00Z",
    director_connection: "confirmed",
  };

  it("採用の判断（202）は再編集の準備告知を出し、pollで準備→実行→完了→失敗が切り替わり、失敗後も判断できる", async () => {
    vi.useFakeTimers();
    let rebuild: ConsultationPayload["rebuild"] = {
      status: "requested",
      target_version: null,
      detail: null,
    };
    const fetchImpl = (async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/consultation/judgment")) {
        return jsonResponse(
          {
            consultations: [
              { ...entryOneJudged, policy: { adopted: adoptedPolicy }, rebuild },
            ],
          },
          202,
        );
      }
      if (url.endsWith("/consultation")) {
        return jsonResponse({
          consultations: [
            { ...entryOneJudged, policy: { adopted: adoptedPolicy }, rebuild },
          ],
        });
      }
      return jsonResponse(payloadEmpty);
    }) as typeof fetch;

    renderPanel(fetchImpl);
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.getAllByTestId("consultation-proposal")).toHaveLength(1);

    fireEvent.click(screen.getByTestId("consultation-judgment-adopt"));
    fireEvent.click(screen.getByTestId("consultation-judgment-submit"));
    await vi.advanceTimersByTimeAsync(0);

    expect(screen.getByTestId("consultation-announcement").textContent).toContain(
      "採用した方針を反映する再編集を準備しています",
    );
    expect(screen.getByTestId("consultation-rebuild-state").textContent).toContain(
      "採用した方針を反映する再編集を準備しています",
    );

    rebuild = { status: "running", target_version: null, detail: null };
    await vi.advanceTimersByTimeAsync(2000);
    expect(screen.getByTestId("consultation-rebuild-state").textContent).toContain(
      "再編集を実行しています",
    );

    rebuild = { status: "succeeded", target_version: "v3", detail: null };
    await vi.advanceTimersByTimeAsync(2000);
    const done = screen.getByTestId("consultation-rebuild-state").textContent ?? "";
    expect(done).toContain("再編集が完了しました（対象版 v3）");
    expect(done).toContain("結果は下の反映結果行で確認してください");

    rebuild = {
      status: "failed",
      target_version: null,
      detail: "対象の版が見つかりませんでした",
    };
    await vi.advanceTimersByTimeAsync(2000);
    const failed = screen.getByTestId("consultation-rebuild-state").textContent ?? "";
    expect(failed).toContain("再編集に失敗しました");
    expect(failed).toContain("対象の版が見つかりませんでした");
    expect(failed).toContain("相談を続けられます");

    fireEvent.click(screen.getByTestId("consultation-judgment-revise"));
    const submit = screen.getByTestId("consultation-judgment-submit") as HTMLButtonElement;
    expect(submit.disabled).toBe(false);
  });

  it("見送りの判断（200）は再編集の行を出さない（不在は無表示であって不明表示ではない）", async () => {
    const { fetchImpl } = recordingFetch((url) =>
      url.endsWith("/consultation/judgment")
        ? jsonResponse(entryOne, 200)
        : jsonResponse(payloadOne),
    );
    renderPanel(fetchImpl);
    await screen.findAllByTestId("consultation-proposal");

    fireEvent.click(screen.getByTestId("consultation-judgment-reject"));
    fireEvent.click(screen.getByTestId("consultation-judgment-submit"));

    await waitFor(() => {
      expect(screen.getByTestId("consultation-announcement").textContent).toContain(
        "判断を記録しました",
      );
    });
    expect(screen.queryByTestId("consultation-rebuild-state")).toBeNull();
  });

  it("接続された方針の実装結果エントリを表示する（接続・旧記録・接続なし失敗の防御形）", async () => {
    const { fetchImpl } = recordingFetch(() =>
      jsonResponse({
        consultations: [
          {
            ...entryOne,
            policy_outcomes: [connectedOutcome, legacyHonoredOutcome, failedOutcome],
          },
        ],
      }),
    );
    renderPanel(fetchImpl);

    const outcomes = await screen.findAllByTestId("consultation-policy-outcome");
    expect(outcomes).toHaveLength(3);
    const connected = outcomes[0]!.textContent ?? "";
    expect(connected).toContain(
      "採用した方針は編集長への入力に接続されました（対象版 v4）",
    );
    expect(connected).toContain("確認済みの項目だけを表示しています");
    expect(connected).toContain(
      "構造検査で確認済み: 方針が編集手順として成り立つかの検査、編集手順の書き出し検査",
    );
    expect(connected).not.toContain("planner_feasibility");
    expect(connected).not.toContain("edit_plan_generation");
    expect(connected).toContain(
      "内容・見た目・音が方針どおりかは、この検査では確認していません",
    );
    expect(connected).toContain("未確認: 結末の方針どおりか");
    expect(connected).toContain("未対応: 字幕と見た目が実映像で方針どおりか");
    expect(connected).not.toContain("反映されました");
    expect(connected).not.toContain("現在適用済み");
    const legacy = outcomes[1]!.textContent ?? "";
    expect(legacy).toContain(
      "採用した方針は編集長への入力に接続された旧記録です（対象版 v3）",
    );
    expect(legacy).toContain("当時の検証範囲は記録されていません");
    expect(legacy).not.toContain("反映されました");
    expect(outcomes[2]!.textContent).toContain(
      "反映できませんでした：対象の版が見つかりませんでした、方針の範囲が空でした",
    );
  });

  it("失敗の接続状態ごとに正直な文言を出す（渡した・渡していない・届いたか不明）", async () => {
    const { fetchImpl } = recordingFetch(() =>
      jsonResponse({
        consultations: [
          {
            ...entryOne,
            policy_outcomes: [
              failedConfirmedOutcome,
              failedNotStartedOutcome,
              failedUnknownOutcome,
            ],
          },
        ],
      }),
    );
    renderPanel(fetchImpl);

    const outcomes = await screen.findAllByTestId("consultation-policy-outcome");
    expect(outcomes).toHaveLength(3);
    const confirmed = outcomes[0]!.textContent ?? "";
    expect(confirmed).toContain(
      "方針は編集長に渡されましたが、編集版の確定に失敗しました",
    );
    expect(confirmed).toContain("policy-commit-failed");
    const notStarted = outcomes[1]!.textContent ?? "";
    expect(notStarted).toContain(
      "方針は編集長に渡していません。理由を確認して相談へ戻れます",
    );
    expect(notStarted).toContain("policy-not-interpretable");
    const unknown = outcomes[2]!.textContent ?? "";
    expect(unknown).toContain(
      "方針が編集長へ届いたか確認できません。重複利用を避けて停止しました",
    );
    for (const outcome of outcomes) {
      expect(outcome.textContent).not.toContain("反映されました");
    }
  });

  it("失敗でも記録に残った版は記録事実として出す（成功とは言わない）", async () => {
    const { fetchImpl } = recordingFetch(() =>
      jsonResponse({
        consultations: [{ ...entryOne, policy_outcomes: [failedRecordedVersionOutcome] }],
      }),
    );
    renderPanel(fetchImpl);

    const outcomes = await screen.findAllByTestId("consultation-policy-outcome");
    expect(outcomes).toHaveLength(1);
    const line = outcomes[0]!.textContent ?? "";
    expect(line).toContain("方針は編集長に渡されましたが、編集版の確定に失敗しました");
    expect(line).toContain("編集の記録は v2 まで残っています");
    expect(line).not.toContain("反映されました");
    expect(line).not.toContain("接続されました");
  });

  it("新フィールドのない旧viewは従来どおり表示し、再編集の行も不明も出さない", async () => {
    const { fetchImpl } = recordingFetch(() => jsonResponse(payloadOne));
    renderPanel(fetchImpl);

    await screen.findAllByTestId("consultation-proposal");
    expect(screen.queryByTestId("consultation-budget-details")).toBeNull();
    expect(screen.queryByTestId("consultation-rebuild-state")).toBeNull();
    expect(screen.queryByTestId("consultation-policy-outcome")).toBeNull();
  });

  it("W5-a: connectedで未対応が空でも未確認（結末未確認）は表示される", async () => {
    const outcome: ConsultationPolicyOutcomeEntry = {
      kind: "policy_outcome",
      outcome_id: "o-w5a",
      consultation_id: "c-1",
      judgment_id: "j-w5a",
      proposal_id: "p-1",
      plan_version: "v4",
      status: "connected",
      reasons: [],
      note: null,
      recorded_at: "2026-09-08T10:20:00Z",
      director_connection: "confirmed",
      realized_checks: [],
      unaddressed: [],
      unconfirmed: ["結末未確認"],
    };
    const { fetchImpl } = recordingFetch(() =>
      jsonResponse({ consultations: [{ ...entryOne, policy_outcomes: [outcome] }] }),
    );
    renderPanel(fetchImpl);

    const outcomes = await screen.findAllByTestId("consultation-policy-outcome");
    expect(outcomes).toHaveLength(1);
    expect(outcomes[0]!.textContent).toContain("未確認: 結末未確認");
  });

  it("W5-b: failed×director_connection=unknownでも記録版と理由は消えない", async () => {
    const outcome: ConsultationPolicyOutcomeEntry = {
      kind: "policy_outcome",
      outcome_id: "o-w5b",
      consultation_id: "c-1",
      judgment_id: "j-w5b",
      proposal_id: "p-1",
      plan_version: "v2",
      status: "failed",
      reasons: ["director_timeout: 応答がありませんでした"],
      note: null,
      recorded_at: "2026-09-08T10:21:00Z",
      director_connection: "unknown",
    };
    const { fetchImpl } = recordingFetch(() =>
      jsonResponse({ consultations: [{ ...entryOne, policy_outcomes: [outcome] }] }),
    );
    renderPanel(fetchImpl);

    const outcomes = await screen.findAllByTestId("consultation-policy-outcome");
    expect(outcomes).toHaveLength(1);
    const line = outcomes[0]!.textContent ?? "";
    expect(line).toContain(
      "方針が編集長へ届いたか確認できません。重複利用を避けて停止しました",
    );
    expect(line).toContain("理由：director_timeout: 応答がありませんでした");
    expect(line).toContain("編集の記録は v2 まで残っています");
  });

  it("W5-c: connection欠落のfallbackでも記録版は表示される", async () => {
    const outcome: ConsultationPolicyOutcomeEntry = {
      kind: "policy_outcome",
      outcome_id: "o-w5c",
      consultation_id: "c-1",
      judgment_id: "j-w5c",
      proposal_id: "p-1",
      plan_version: "v2",
      status: "failed",
      reasons: ["対象の版が見つかりませんでした"],
      note: null,
      recorded_at: "2026-09-08T10:22:00Z",
    };
    const { fetchImpl } = recordingFetch(() =>
      jsonResponse({ consultations: [{ ...entryOne, policy_outcomes: [outcome] }] }),
    );
    renderPanel(fetchImpl);

    const outcomes = await screen.findAllByTestId("consultation-policy-outcome");
    expect(outcomes).toHaveLength(1);
    const line = outcomes[0]!.textContent ?? "";
    expect(line).toContain("反映できませんでした：対象の版が見つかりませんでした");
    expect(line).toContain("編集の記録は v2 まで残っています");
  });

  it("W10: 現行headより古い冪等返却は記録版と区別し、現在適用済みとは言わない", async () => {
    const superseded: ConsultationPolicyOutcomeEntry = {
      ...connectedOutcome,
      outcome_id: "o-w10a",
      judgment_id: "j-w10a",
      superseded_by_head: true,
    };
    const ineffective: ConsultationPolicyOutcomeEntry = {
      ...connectedOutcome,
      outcome_id: "o-w10b",
      judgment_id: "j-w10b",
      effective: false,
    };
    const { fetchImpl } = recordingFetch(() =>
      jsonResponse({
        consultations: [{ ...entryOne, policy_outcomes: [superseded, ineffective] }],
      }),
    );
    renderPanel(fetchImpl);

    const outcomes = await screen.findAllByTestId("consultation-policy-outcome");
    expect(outcomes).toHaveLength(2);
    for (const outcome of outcomes) {
      const line = outcome.textContent ?? "";
      expect(line).toContain(
        "記録として表示しています（現在は新しい版があります）",
      );
      expect(line).not.toContain("現在適用済み");
    }
  });

  it("W10: 新欄のない旧データは記録版の区別行を出さない", async () => {
    const { fetchImpl } = recordingFetch(() =>
      jsonResponse({
        consultations: [{ ...entryOne, policy_outcomes: [connectedOutcome] }],
      }),
    );
    renderPanel(fetchImpl);

    const outcomes = await screen.findAllByTestId("consultation-policy-outcome");
    expect(outcomes).toHaveLength(1);
    expect(outcomes[0]!.textContent).not.toContain("記録として表示しています");
  });

  it("再読込（作り直し）でもpollのviewから同じ再編集状態が復元する", async () => {
    const succeeded: ConsultationPayload = {
      consultations: [
        {
          ...entryOneJudged,
          policy: { adopted: adoptedPolicy },
          rebuild: { status: "succeeded", target_version: "v3", detail: null },
        },
      ],
    };
    const { fetchImpl } = recordingFetch(() => jsonResponse(succeeded));

    const first = renderPanel(fetchImpl);
    await waitFor(() => {
      expect(screen.getByTestId("consultation-rebuild-state").textContent).toContain(
        "再編集が完了しました（対象版 v3）",
      );
    });
    first.unmount();

    renderPanel(fetchImpl);
    await waitFor(() => {
      expect(screen.getByTestId("consultation-rebuild-state").textContent).toContain(
        "再編集が完了しました（対象版 v3）",
      );
    });
  });

  it("採用済みの方針があると試し動画の要求ボタンが出て、ない旧viewでは出さない", async () => {
    const adopted: ConsultationPayload = {
      consultations: [
        { ...entryOneJudged, policy: { adopted: adoptedPolicy } },
      ],
    };
    const adoptedFetch = recordingFetch((url) => {
      if (url.endsWith("/consultation/samples")) {
        return jsonResponse({ samples: [] });
      }
      return jsonResponse(adopted);
    });
    const first = renderPanel(adoptedFetch.fetchImpl);
    await waitFor(() => {
      expect(screen.getByTestId("sample-request")).toBeVisible();
    });
    expect(screen.getByTestId("sample-request").textContent).toContain(
      "試し動画を作る",
    );
    first.unmount();

    const { fetchImpl } = recordingFetch(() => jsonResponse(payloadOne));
    renderPanel(fetchImpl);
    await waitFor(() => {
      expect(screen.getByTestId("consultation-panel")).toBeVisible();
    });
    expect(
      screen.queryByTestId("consultation-sample-section"),
    ).toBeNull();
  });
});

describe("ConsultationPanel（P4: 採用先行・samples後続でも中間hintを出さない）", () => {
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
  const adoptedView: ConsultationPayload = {
    consultations: [
      { ...entryOneJudged, policy: { adopted: adoptedPolicy } },
    ],
  };
  const publishedSample = {
    sample_id: "sample-1",
    status: "published",
    total_seconds: 28.5,
    created_at: "2026-09-10T10:00:00Z",
    published_at: "2026-09-10T10:01:00Z",
  };

  it("samples確定までhintを出さず、published到着後は確定値だけを出す（方向へ落下しない）", async () => {
    const hints: StageHint[] = [];
    let resolveSamples!: () => void;
    const samplesGate = new Promise<void>((resolve) => {
      resolveSamples = resolve;
    });
    const fetchImpl = (async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/consultation/samples")) {
        await samplesGate;
        return jsonResponse({ samples: [publishedSample] });
      }
      return jsonResponse(adoptedView);
    }) as typeof fetch;

    render(
      <ConsultationPanel
        episodeId="ep-c01"
        status={stageStatus("selection")}
        fetchImpl={fetchImpl}
        onStageHint={(hint) => {
          hints.push(hint);
        }}
      />,
    );

    // 採用は届いたがsamplesは未確定：方向レイアウトの中で要求ボタンが出る
    await waitFor(() => {
      expect(screen.getByTestId("sample-request")).toBeVisible();
    });
    await waitFor(() => {
      expect(screen.getByTestId("consultation-entry")).toBeVisible();
    });
    // 中間hint（adopted:true + hasPublishedSample:false）は出さない
    expect(hints).toHaveLength(0);
    expect(screen.getByText("編集の方向を決める")).toBeVisible();

    // samplesがpublishedで届く → 確定値だけを出す（poll毎の再送があっても値は確定値）
    resolveSamples();
    await waitFor(() => {
      expect(screen.getByTestId("sample-preview")).toBeVisible();
    });
    expect(hints.length).toBeGreaterThanOrEqual(1);
    for (const hint of hints) {
      expect(hint).toEqual({
        hasConsultation: true,
        adopted: true,
        hasPublishedSample: true,
        fullAuthorized: false,
      });
    }
    // 試し動画段階に上がり、方向見出しに戻らない
    expect(screen.queryByText("編集の方向を決める")).toBeNull();
  });
});
