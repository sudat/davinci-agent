import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import EpisodeView from "@/components/EpisodeView";

/** 工程2P poll hardening（step2p-design.md §A/§F + codex条件6）:
 *  - 状態取得時刻 fetchedAt を成功毎に記録し、15秒途切れで専用banner
 *  - 失敗でも poll を続ける
 *  - visibilitychange/focus で即時再照会
 *  - 補助fetch（flags/preview）が poll 予定を遅らせない */

function jsonResponse(url: string, status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", url },
  });
}

function okStatus(seq: number): Response {
  return jsonResponse("s", 200, {
    episode_id: "ep-poll01",
    job_id: "ep-poll01",
    status: "CREATED",
    current_stage: "intake",
    created_at_seq: 1,
    updated_at_seq: seq,
    stage_runs: [],
    current_run: null,
    current_target_version: null,
    pending_rebuild: null,
    rebuild_requests: [],
    last_worker_report_at: null,
    last_worker_report_event: null,
    unreviewed_proposal_set: false,
  });
}

const flagsOk = () =>
  jsonResponse("f", 200, { flags: [], not_yet_generated: true });
const preview404 = () => new Response(null, { status: 404 });

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("EpisodeView poll hardening（工程2P）", () => {
  it("補助fetch（flags/preview）が停止しても status poll は止まらない", async () => {
    let statusCalls = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const url = String(input);
      if (url.endsWith("/flags")) return new Promise(() => undefined);
      if (url.endsWith("/preview")) return new Promise(() => undefined);
      if (url.includes("/finishing")) return Promise.resolve(okStatus(0));
      statusCalls += 1;
      return Promise.resolve(okStatus(statusCalls));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<EpisodeView episodeId="ep-poll01" />);
    await vi.advanceTimersByTimeAsync(0);
    expect(statusCalls).toBe(1);

    await vi.advanceTimersByTimeAsync(2000);
    await vi.advanceTimersByTimeAsync(2000);
    expect(statusCalls).toBeGreaterThanOrEqual(3);
  });

  it("15秒更新途切れで専用banner（error noticeとは別、復帰で消える）。中止は未実装と明示", async () => {
    let failing = false;
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const url = String(input);
      if (url.endsWith("/flags")) return Promise.resolve(flagsOk());
      if (url.endsWith("/preview")) return Promise.resolve(preview404());
      if (url.includes("/finishing")) return Promise.resolve(okStatus(0));
      if (failing) return Promise.reject(new TypeError("network down"));
      return Promise.resolve(okStatus(1));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<EpisodeView episodeId="ep-poll01" />);
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.queryByTestId("stale-banner")).toBeNull();

    failing = true;
    await vi.advanceTimersByTimeAsync(16000);
    const banner = screen.getByTestId("stale-banner");
    expect(banner.textContent).toContain("状態の更新が途切れています");
    expect(banner.textContent).toContain("安全な中止は未実装です");
    expect(banner.textContent).toContain("再照会");
    // poll失敗のerror noticeとは別物として両方出る
    expect(screen.getByTestId("error-notice")).toBeTruthy();

    failing = false;
    await vi.advanceTimersByTimeAsync(3000);
    expect(screen.queryByTestId("stale-banner")).toBeNull();
    expect(screen.queryByTestId("error-notice")).toBeNull();
  });

  it("focusとvisibilitychangeで即時再照会する（次の2秒tickを待たない）", async () => {
    let statusCalls = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const url = String(input);
      if (url.endsWith("/flags")) return Promise.resolve(flagsOk());
      if (url.endsWith("/preview")) return Promise.resolve(preview404());
      if (url.includes("/finishing")) return Promise.resolve(okStatus(0));
      statusCalls += 1;
      return Promise.resolve(okStatus(statusCalls));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<EpisodeView episodeId="ep-poll01" />);
    await vi.advanceTimersByTimeAsync(0);
    expect(statusCalls).toBe(1);

    await vi.advanceTimersByTimeAsync(500);
    window.dispatchEvent(new Event("focus"));
    await vi.advanceTimersByTimeAsync(0);
    expect(statusCalls).toBe(2);

    await vi.advanceTimersByTimeAsync(500);
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    expect(statusCalls).toBe(3);
  });
});
