import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import EpisodeWaitInfo from "@/components/EpisodeWaitInfo";
import type { EpisodeStatus } from "@/lib/api";

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
    expect(screen.getByTestId("wait-current-work").textContent).toContain("試し編集");
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

  it("現在の工程の開始時刻が無ければ工程経過は不明と明示", () => {
    renderGuidance({});
    expect(screen.getByTestId("wait-stage-elapsed").textContent).toContain("不明");
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

  it("今回runの再試行回数を表示する（理由や最大回数は捏造しない）", () => {
    renderGuidance({ current_run_retry_count: 2 });
    const retry = screen.getByTestId("wait-retry").textContent ?? "";
    expect(retry).toContain("再試行中です（2回目）");
    expect(retry).not.toContain("最大");
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
});
