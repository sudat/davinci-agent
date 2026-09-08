import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import EpisodeView from "@/components/EpisodeView";
import EpisodeProgress from "@/components/EpisodeProgress";
import type { EpisodeStatus } from "@/lib/api";

/**
 * 工程2Pの残りの表示契約（step2p-design.md §C + codex条件1/5）のうち、
 * episode-view-poll / episode-wait-info / rebuild-phase / chat-send-busy
 * の各試験がcoverしない部分を固定する:
 *  - stage table の 4段階写像の列（§3.4）
 *  - PREVIEW_READY は「試し編集完了」表示で全体完了と言わない（codex条件1）
 *  - progress領域の aria-live / aria-busy（工程2P状態語彙）
 *  - 止まっている段階への「安全な中止は未実装です」+ 再照会（U29 partial）
 */

const NOW = Date.parse("2026-09-08T12:00:30Z");
const INTAKE = "2026-09-08T12:00:00Z";

function statusWith(overrides: Partial<EpisodeStatus>): EpisodeStatus {
  return {
    episode_id: "ep-p2p01",
    job_id: "ep-p2p01",
    status: "PLAN_COMMITTED",
    current_stage: "compile",
    created_at_seq: 1,
    updated_at_seq: 20,
    stage_runs: [],
    current_run: "run-9",
    unreviewed_proposal_set: false,
    intake_created_at: INTAKE,
    ...overrides,
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("EpisodeProgress — 4段階写像の列（stage table）", () => {
  it("各行の段階列に平易な段階名を出す（生stage名だけにしない）", () => {
    render(
      <EpisodeProgress
        status={statusWith({
          stage_runs: [
            {
              stage_name: "compile",
              status: "running",
              retry_count: 0,
              last_error_code: null,
              run_id: "run-9",
            },
            {
              stage_name: "ingest",
              status: "succeeded",
              retry_count: 0,
              last_error_code: null,
            },
          ],
        })}
        now={NOW}
      />,
    );
    const table = screen.getByTestId("stage-runs").textContent ?? "";
    expect(table).toContain("段階");
    expect(table).toContain("試し編集");
    expect(table).toContain("素材の受付と確認");
    expect(table).toContain("compile");
  });
});

describe("EpisodeProgress — 止まっている段階のU29表示", () => {
  it("failed_blockedの段階があると中止未実装を明示し再照会を出す", () => {
    const onRequery = vi.fn();
    render(
      <EpisodeProgress
        status={statusWith({
          stage_runs: [
            {
              stage_name: "compile",
              status: "failed_blocked",
              retry_count: 1,
              last_error_code: "compile-failed",
              run_id: "run-9",
            },
          ],
        })}
        now={NOW}
        onRequery={onRequery}
      />,
    );
    const stall = screen.getByTestId("wait-stalled");
    expect(stall.textContent).toContain("止まっている段階があります");
    expect(stall.textContent).toContain("安全な中止は未実装です");
    screen.getByTestId("requery-button").click();
    expect(onRequery).toHaveBeenCalledTimes(1);
  });
});

describe("EpisodeProgress — aria（工程2P）", () => {
  it("progress領域にaria-live=politeとaria-busyを持つ", () => {
    render(
      <EpisodeProgress
        status={statusWith({
          stage_runs: [
            {
              stage_name: "compile",
              status: "running",
              retry_count: 0,
              last_error_code: null,
              run_id: "run-9",
            },
          ],
        })}
        now={NOW}
      />,
    );
    const region = screen.getByTestId("episode-progress");
    expect(region.getAttribute("aria-live")).toBe("polite");
    expect(region.getAttribute("aria-busy")).toBe("true");
  });
});

describe("EpisodeView — PREVIEW_READYの語彙（codex条件1）", () => {
  it("試し編集完了と表示し、全体完了という語は使わない", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL): Promise<Response> => {
        const url = String(input);
        if (url.endsWith("/flags")) {
          return Promise.resolve(
            new Response(JSON.stringify({ flags: [], not_yet_generated: true }), {
              status: 200,
              headers: { "content-type": "application/json" },
            }),
          );
        }
        if (url.endsWith("/preview")) return Promise.resolve(new Response(null, { status: 404 }));
        if (url.includes("/finishing")) {
          return Promise.resolve(new Response(JSON.stringify({}), { status: 200 }));
        }
        return Promise.resolve(
          new Response(
            JSON.stringify(
              statusWith({
                status: "PREVIEW_READY",
                current_stage: "review",
                current_run: null,
              }),
            ),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
        );
      }),
    );
    render(<EpisodeView episodeId="ep-p2p01" />);
    await vi.advanceTimersByTimeAsync(2100);
    const statusText = screen.getByTestId("episode-status").textContent ?? "";
    expect(statusText).toContain("PREVIEW_READY");
    expect(statusText).toContain("試し編集完了");
    expect(statusText).not.toContain("全体完了");
  });
});
