import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import EpisodeWaitInfo from "@/components/EpisodeWaitInfo";
import type { EpisodeStatus } from "@/lib/api";
import {
  lastStartedRowOfActiveRun,
  runningStageRows,
  stageWorkName,
} from "@/lib/stageGroups";

/**
 * 工程2Pの10秒必須待機情報（改訂条件5）の成分試験: 経過時間・現在の作業・
 * 完了済み/現在/未着手の段階・操作要否を、実在するpayload項目「み」から
 * 表示すること。捏造百分率なし・未計測の正直表示・応答不明・再試行中・
 * 中止未実装の各文言を固定する。時計は固定して判定を決定論的にする。
 */

function statusAt(anchorIso: string, overrides: Partial<EpisodeStatus>): EpisodeStatus {
  return {
    episode_id: "ep-wait01",
    job_id: "ep-wait01",
    status: "ANALYZED",
    current_stage: "compile",
    created_at_seq: 1,
    updated_at_seq: 10,
    stage_runs: [],
    current_run: "run-1",
    unreviewed_proposal_set: false,
    intake_created_at: anchorIso,
    ...overrides,
  };
}

const T0 = new Date("2026-09-08T12:00:30Z");

function renderGuidance(overrides: Partial<EpisodeStatus>, secondsElapsed = 30) {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
  const anchor = new Date(T0.getTime() - secondsElapsed * 1000).toISOString();
  const view = render(<EpisodeWaitInfo status={statusAt(anchor, overrides)} />);
  return view;
}

afterEach(() => {
  vi.useRealTimers();
});

describe("EpisodeWaitInfo — 10秒からの必須待機情報", () => {
  it("10秒以下は何も表示しない（待機情報の開始条件）", () => {
    const { container } = renderGuidance({}, 5);
    expect(container.querySelector('[data-testid="wait-guidance"]')).toBeNull();
  });

  it("経過時間・現在の作業・段階の完了済み/現在/未着手を表示する", () => {
    renderGuidance({
      stage_runs: [
        { stage_name: "ingest", status: "succeeded", retry_count: 0, last_error_code: null },
        { stage_name: "analyze", status: "succeeded", retry_count: 0, last_error_code: null },
        { stage_name: "plan", status: "succeeded", retry_count: 0, last_error_code: null },
        {
          stage_name: "compile",
          status: "running",
          retry_count: 0,
          last_error_code: null,
          run_id: "run-1",
        },
      ],
    });
    expect(screen.getByTestId("wait-elapsed").textContent).toContain("00:30");
    expect(screen.getByTestId("wait-current-work").textContent).toContain(
      "編集指示の具体化",
    );
    const groups = screen.getByTestId("wait-groups").textContent ?? "";
    expect(groups).toContain("完了済みの段階: 素材の受付と確認・方針の準備");
    expect(groups).toContain("現在の段階: 試し編集");
    expect(groups).toContain("未着手の段階: 仕上げ・完了");
  });

  it("未消費の修正案があれば操作要否はあなたの確認待ち", () => {
    renderGuidance({ unreviewed_proposal_set: true });
    expect(screen.getByTestId("wait-operator-action").textContent).toBe(
      "あなたの確認待ちです（修正案を確認してください）",
    );
  });

  it("動作報告がある稼働中は今は操作不要", () => {
    renderGuidance({
      stage_runs: [
        {
          stage_name: "compile",
          status: "running",
          retry_count: 0,
          last_error_code: null,
          run_id: "run-1",
        },
      ],
      last_worker_report_at: new Date(T0.getTime() - 5_000).toISOString(),
    });
    expect(screen.getByTestId("wait-operator-action").textContent).toBe(
      "今は操作不要です（自動で進行しています）",
    );
  });

  it("動作報告が無ければ応答不明（自動で進行していると断定しない）", () => {
    renderGuidance({
      stage_runs: [
        {
          stage_name: "compile",
          status: "running",
          retry_count: 0,
          last_error_code: null,
          run_id: "run-1",
        },
      ],
    });
    expect(screen.getByTestId("wait-operator-action").textContent).toBe(
      "進み具合は応答不明です（しばらく待つか再照会してください）",
    );
  });

  it("現在の工程の経過は現行runの開始時刻基準（全体経過とは別項目）", () => {
    renderGuidance({
      stage_runs: [
        {
          stage_name: "compile",
          status: "running",
          retry_count: 0,
          last_error_code: null,
          run_id: "run-1",
          first_started_at: new Date(T0.getTime() - 20_000).toISOString(),
        },
      ],
    });
    expect(screen.getByTestId("wait-stage-elapsed").textContent).toContain("00:20");
    expect(screen.getByTestId("wait-elapsed").textContent).toContain("00:30");
  });

  it("実行中の工程が無ければ実行中なしと明示（停止/終端時の時計の意味を固定）", () => {
    renderGuidance({});
    expect(screen.getByTestId("wait-stage-elapsed").textContent).toBe(
      "実行中の工程はありません",
    );
  });

  it("実行中行に開始時刻が無ければ工程経過は不明と明示", () => {
    renderGuidance({
      stage_runs: [
        {
          stage_name: "compile",
          status: "running",
          retry_count: 0,
          last_error_code: null,
          run_id: "run-1",
        },
      ],
    });
    expect(screen.getByTestId("wait-stage-elapsed").textContent).toContain("不明");
  });

  it("job.current_stageと実行中工程が乖離しても実行中工程から経過を出す（codex回帰）", () => {
    renderGuidance({
      current_stage: "preview",
      stage_runs: [
        {
          stage_name: "compile",
          status: "running",
          retry_count: 0,
          last_error_code: null,
          run_id: "run-1",
          first_started_at: new Date(T0.getTime() - 20_000).toISOString(),
        },
      ],
    });
    expect(screen.getByTestId("wait-stage-elapsed").textContent).toContain("00:20");
    expect(screen.getByTestId("wait-stage-elapsed").textContent).toContain("編集指示の具体化");
    expect(screen.getByTestId("wait-stage-elapsed").textContent).not.toContain("不明");
    expect(screen.getByTestId("wait-current-work").textContent).toContain("編集指示の具体化");
  });

  it("複数工程が並行で動くときは推測してまとめず工程ごとに併記する（codex並行指摘）", () => {
    renderGuidance({
      current_stage: "preview",
      stage_runs: [
        {
          stage_name: "compile",
          status: "running",
          retry_count: 0,
          last_error_code: null,
          run_id: "run-1",
          first_started_at: new Date(T0.getTime() - 20_000).toISOString(),
        },
        {
          stage_name: "analyze",
          status: "running",
          retry_count: 0,
          last_error_code: null,
          run_id: "run-1",
          first_started_at: new Date(T0.getTime() - 10_000).toISOString(),
        },
      ],
    });
    expect(screen.getByTestId("wait-stage-elapsed").textContent).toBe(
      "編集指示の具体化（compile） 00:20・素材の分析（analyze） 00:10",
    );
    expect(screen.getByTestId("wait-current-work").textContent).toBe(
      "編集指示の具体化・素材の分析",
    );
  });

  it("実行中が無く現行runに終端行があれば時計の意味を最後の実行開始として固定表示する（成長する経過を捏造しない）", () => {
    const startedAt = new Date(T0.getTime() - 45_000);
    renderGuidance({
      stage_runs: [
        {
          stage_name: "compile",
          status: "failed_blocked",
          retry_count: 1,
          last_error_code: "compile-failed",
          run_id: "run-1",
          first_started_at: startedAt.toISOString(),
        },
      ],
    });
    const clock = [startedAt.getHours(), startedAt.getMinutes(), startedAt.getSeconds()]
      .map((part) => String(part).padStart(2, "0"))
      .join(":");
    expect(screen.getByTestId("wait-stage-elapsed").textContent).toBe(
      `実行中の工程はありません（最後の実行開始 ${clock}）`,
    );
    expect(screen.getByTestId("wait-current-work").textContent).toContain("実行中の工程なし");
  });

  it("同一段階の並行2工程は具体工程名で判別可能（codex回帰）", () => {
    renderGuidance({
      current_stage: "preview",
      stage_runs: [
        {
          stage_name: "compile",
          status: "running",
          retry_count: 0,
          last_error_code: null,
          run_id: "run-1",
          first_started_at: new Date(T0.getTime() - 20_000).toISOString(),
        },
        {
          stage_name: "preview",
          status: "running",
          retry_count: 0,
          last_error_code: null,
          run_id: "run-1",
          first_started_at: new Date(T0.getTime() - 10_000).toISOString(),
        },
      ],
    });
    const text = screen.getByTestId("wait-stage-elapsed").textContent ?? "";
    expect(text).toContain("（compile） 00:20");
    expect(text).toContain("（preview） 00:10");
  });

  it("現在工程の行がまだ無ければ進み具合は未計測と正直に言う（百分率なし）", () => {
    const { container } = renderGuidance({
      stage_runs: [
        { stage_name: "ingest", status: "succeeded", retry_count: 0, last_error_code: null },
      ],
    });
    expect(screen.getByTestId("wait-unmeasured").textContent).toBe(
      "この工程の進み具合は取得できません",
    );
    expect(container.textContent).not.toContain("%");
  });

  it("runが動いていて動作報告がなければ応答不明（正常とは言わない）", () => {
    renderGuidance({ last_worker_report_at: null });
    expect(screen.getByTestId("wait-worker-report").textContent).toContain("応答不明");
  });

  it("動作報告があればその時刻を表示する", () => {
    renderGuidance({ last_worker_report_at: "2026-09-08T12:00:20Z" });
    expect(screen.getByTestId("wait-worker-report").textContent).toContain("最後の作業報告");
  });

  it("今回runの再試行回数と次動作を表示する（上限・次回失敗時の動作は不明と明示）", () => {
    renderGuidance({
      current_run_retry_count: 2,
      stage_runs: [
        {
          stage_name: "compile",
          status: "failed_blocked",
          retry_count: 1,
          last_error_code: "compile-failed",
          run_id: "run-1",
        },
      ],
    });
    const retry = screen.getByTestId("wait-retry").textContent ?? "";
    expect(retry).toBe(
      "再試行中です（2回目・最大回数は提供されていません）：理由 compile-failed。次の動作: 同じ工程の再実行（この経路の上限・次回失敗時の動作は提供されていません）",
    );
  });

  it("再試行の次動作は同じ工程の再実行とだけ言い上限・失敗時を捏造しない", () => {
    renderGuidance({ current_run_retry_count: 2 });
    const retry = screen.getByTestId("wait-retry").textContent ?? "";
    expect(retry).toContain("次の動作: 同じ工程の再実行");
    expect(retry).toContain("この経路の上限・次回失敗時の動作は提供されていません");
  });

  it("再試行中表示に捏造した方針数値を含まない（秒数や最大N回を作らない）", () => {
    renderGuidance({ current_run_retry_count: 2 });
    const retry = screen.getByTestId("wait-retry").textContent ?? "";
    expect(retry).not.toMatch(/\d+秒|最大\d+回/);
  });

  it("止まっている段階があるときは中止未実装を明示し再照会を出す", () => {
    vi.useFakeTimers();
    vi.setSystemTime(T0);
    const anchor = new Date(T0.getTime() - 30_000).toISOString();
    const onRequery = vi.fn();
    const view = render(
      <EpisodeWaitInfo
        status={statusAt(anchor, {
          stage_runs: [
            {
              stage_name: "compile",
              status: "failed_blocked",
              retry_count: 1,
              last_error_code: "compile-failed",
              run_id: "run-1",
            },
          ],
        })}
        onRequery={onRequery}
      />,
    );
    expect(screen.getByTestId("wait-failed").textContent).toContain("止まっている段階があります");
    expect(screen.getByTestId("wait-stop-unsupported").textContent).toContain(
      "安全な中止は未実装です",
    );
    const button = screen.getByTestId("requery-button");
    button.click();
    expect(onRequery).toHaveBeenCalledTimes(1);
    view.unmount();
  });

  it("今回の実行を特定できないとき旧runの開始時刻を表示しない（codex P1回帰）", () => {
    const oldStarted = new Date(T0.getTime() - 45_000);
    const overrides: Partial<EpisodeStatus> = {
      current_run: null,
      pending_rebuild: {
        sequence: 1,
        stage_hint: null,
        marker: null,
        run_id: null,
        target_version: null,
      },
      stage_runs: [
        {
          stage_name: "compile",
          status: "succeeded",
          retry_count: 0,
          last_error_code: null,
          run_id: "run-0",
          first_started_at: oldStarted.toISOString(),
        },
      ],
    };
    const status = statusAt(
      new Date(T0.getTime() - 30_000).toISOString(),
      overrides,
    );
    expect(runningStageRows(status)).toEqual([]);
    expect(lastStartedRowOfActiveRun(status)).toBeNull();
    renderGuidance(overrides);
    const line = screen.getByTestId("wait-stage-elapsed").textContent ?? "";
    const oldClock = [oldStarted.getHours(), oldStarted.getMinutes(), oldStarted.getSeconds()]
      .map((part) => String(part).padStart(2, "0"))
      .join(":");
    expect(line).toBe(
      "実行中の工程はありません（最後の実行開始 不明（今回の実行を特定できません））",
    );
    expect(line).not.toContain(oldClock);
  });

  it("現行runが既知なら旧runの行に引っ張られず現行runの開始時刻を出す", () => {
    const oldStarted = new Date(T0.getTime() - 90_000);
    const activeStarted = new Date(T0.getTime() - 45_000);
    renderGuidance({
      stage_runs: [
        {
          stage_name: "compile",
          status: "failed_blocked",
          retry_count: 1,
          last_error_code: "compile-failed",
          run_id: "run-0",
          first_started_at: oldStarted.toISOString(),
        },
        {
          stage_name: "compile",
          status: "failed_blocked",
          retry_count: 1,
          last_error_code: "compile-failed",
          run_id: "run-1",
          first_started_at: activeStarted.toISOString(),
        },
      ],
    });
    const activeClock = [
      activeStarted.getHours(),
      activeStarted.getMinutes(),
      activeStarted.getSeconds(),
    ]
      .map((part) => String(part).padStart(2, "0"))
      .join(":");
    const oldClock = [oldStarted.getHours(), oldStarted.getMinutes(), oldStarted.getSeconds()]
      .map((part) => String(part).padStart(2, "0"))
      .join(":");
    const line = screen.getByTestId("wait-stage-elapsed").textContent ?? "";
    expect(line).toBe(`実行中の工程はありません（最後の実行開始 ${activeClock}）`);
    expect(line).not.toContain(oldClock);
  });

  it("両欄は同一の作業名語彙を使う（内部工程名は補助表示のみ）", () => {
    expect(stageWorkName("compile")).toBe("編集指示の具体化");
    expect(stageWorkName("preview")).toBe("試し映像の作成");
    expect(stageWorkName("analyze")).toBe("素材の分析");
    expect(stageWorkName("selection")).toBe("使う場面の選定");
    renderGuidance({
      current_stage: "preview",
      stage_runs: [
        {
          stage_name: "preview",
          status: "running",
          retry_count: 0,
          last_error_code: null,
          run_id: "run-1",
          first_started_at: new Date(T0.getTime() - 10_000).toISOString(),
        },
      ],
    });
    expect(screen.getByTestId("wait-stage-elapsed").textContent).toContain(
      "試し映像の作成（preview）",
    );
    expect(screen.getByTestId("wait-current-work").textContent).toBe("試し映像の作成");
  });
});
