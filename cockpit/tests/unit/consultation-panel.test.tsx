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
  it("plan確定後の段階（compile）では表示しない・fetchもしない", () => {
    const { fetchImpl, calls } = recordingFetch(() => jsonResponse(payloadOne));
    render(
      <ConsultationPanel
        episodeId="ep-c01"
        status={stageStatus("compile")}
        fetchImpl={fetchImpl}
      />,
    );
    expect(screen.queryByTestId("consultation-panel")).toBeNull();
    expect(calls).toHaveLength(0);
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
      "冒頭から引きで見せる構成",
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

  it("budgetは累計と上限を出し、費用は回数管理・リセットされないと明示する", async () => {
    const { fetchImpl } = recordingFetch(() => jsonResponse(payloadOne));
    renderPanel(fetchImpl);

    await waitFor(() => {
      expect(screen.getByTestId("consultation-budget")).toBeVisible();
    });
    expect(screen.getByTestId("consultation-budget-llm-calls").textContent).toBe("3 / 10回");
    expect(screen.getByTestId("consultation-budget-intervals").textContent).toBe("2 / 6");
    expect(screen.getByTestId("consultation-budget-wall-seconds").textContent).toBe(
      "120 / 600秒",
    );
    const policy = screen.getByTestId("consultation-budget-policy").textContent ?? "";
    expect(policy).toContain("直接計測できない");
    expect(policy).toContain("呼び出し回数で管理");
    expect(policy).toContain("リセットされません");
    expect(screen.getByTestId("consultation-budget-cost-display").textContent).toBe(
      budget.cost_display,
    );
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
    expect(send.textContent).toBe("送信");
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

  const honoredOutcome: ConsultationPolicyOutcomeEntry = {
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

  it("採用の判断（202）は再編集の準備告知と「反映中」を出し、pollで成功→失敗が切り替わり、失敗後も判断できる", async () => {
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
      "反映中",
    );

    rebuild = { status: "succeeded", target_version: "v3", detail: null };
    await vi.advanceTimersByTimeAsync(2000);
    expect(screen.getByTestId("consultation-rebuild-state").textContent).toContain(
      "反映しました（対象版 v3）",
    );

    rebuild = {
      status: "failed",
      target_version: null,
      detail: "対象の版が見つかりませんでした",
    };
    await vi.advanceTimersByTimeAsync(2000);
    const failed = screen.getByTestId("consultation-rebuild-state").textContent ?? "";
    expect(failed).toContain("反映できませんでした");
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

  it("採用した方針の反映結果エントリを表示する（反映／失敗と理由）", async () => {
    const { fetchImpl } = recordingFetch(() =>
      jsonResponse({
        consultations: [{ ...entryOne, policy_outcomes: [honoredOutcome, failedOutcome] }],
      }),
    );
    renderPanel(fetchImpl);

    const outcomes = await screen.findAllByTestId("consultation-policy-outcome");
    expect(outcomes).toHaveLength(2);
    expect(outcomes[0]!.textContent).toContain("反映されました（対象版 v3）");
    expect(outcomes[1]!.textContent).toContain(
      "反映できませんでした：対象の版が見つかりませんでした、方針の範囲が空でした",
    );
  });

  it("新フィールドのない旧viewは従来どおり表示し、再編集の行も不明も出さない", async () => {
    const { fetchImpl } = recordingFetch(() => jsonResponse(payloadOne));
    renderPanel(fetchImpl);

    await screen.findAllByTestId("consultation-proposal");
    expect(screen.getByTestId("consultation-budget")).toBeVisible();
    expect(screen.queryByTestId("consultation-rebuild-state")).toBeNull();
    expect(screen.queryByTestId("consultation-policy-outcome")).toBeNull();
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
        "反映しました（対象版 v3）",
      );
    });
    first.unmount();

    renderPanel(fetchImpl);
    await waitFor(() => {
      expect(screen.getByTestId("consultation-rebuild-state").textContent).toContain(
        "反映しました（対象版 v3）",
      );
    });
  });
});
