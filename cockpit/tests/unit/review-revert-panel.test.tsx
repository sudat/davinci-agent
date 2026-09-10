import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import ReviewChatPanel from "@/components/ReviewChatPanel";
import type { EpisodeStatus } from "@/lib/api";

const DRAFT_KEEP_LONGER = {
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

const REVERT_SUCCESS = {
  restored_from_version: "v1",
  new_version: "v3",
  rebuild: {
    stage_hint: "plan,compile,preview,resolve_build,qc,render",
    scheduled: true,
    stages: ["plan", "compile", "preview", "resolve_build", "qc", "render"],
    runner_log: "/tmp/episodes/ep-abc/runner.log",
    applied_command: "revert-v1",
  },
};

const REVERT_REBUILD_START_FAILED = {
  ...REVERT_SUCCESS,
  rebuild: {
    ...REVERT_SUCCESS.rebuild,
    scheduled: false,
    reason: "runner-start-failed",
    detail: "spawn failed",
  },
};

function statusOf(stageRuns: EpisodeStatus["stage_runs"]): EpisodeStatus {
  return {
    episode_id: "ep-abc",
    job_id: "ep-abc",
    status: "PREVIEW_READY",
    current_stage: "preview",
    created_at_seq: 1,
    updated_at_seq: 1,
    stage_runs: stageRuns,
  };
}

function jsonResponse(payload: object, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("ReviewChatPanel（版の復帰）", () => {
  it("復帰ボタンは前版への復帰をPOSTし、版メモと再build表示を更新する", async () => {
    const posted: { url: string; init?: RequestInit }[] = [];
    let resolveRevert!: (value: Response) => void;
    const fetchImpl = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/review-chat")) {
        return Promise.resolve(
          jsonResponse({ received: true, sequence: 1, draft: DRAFT_KEEP_LONGER }),
        );
      }
      if (url.endsWith("/review-chat/revert")) {
        posted.push({ url, init });
        return new Promise<Response>((resolve) => {
          resolveRevert = resolve;
        });
      }
      throw new Error(`unexpected url: ${url}`);
    });

    render(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => 1.5}
        status={statusOf([])}
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );
    fireEvent.change(screen.getByLabelText("気になるところを伝える"), {
      target: { value: "この後2秒残して" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));
    await waitFor(() => {
      expect(screen.getByTestId("review-draft")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("review-revert-button"));
    expect((screen.getByTestId("review-revert-button") as HTMLButtonElement).disabled).toBe(
      true,
    );
    resolveRevert(jsonResponse(REVERT_SUCCESS));

    await waitFor(() => {
      expect(screen.getByTestId("review-revert-note")).toBeTruthy();
    });
    expect(posted).toHaveLength(1);
    expect(posted[0].url).toContain("/episodes/ep-abc/review-chat/revert");
    expect(posted[0].init?.method).toBe("POST");
    expect(screen.getByTestId("review-revert-note").textContent).toContain(
      "版を戻しました（v3）",
    );
    expect(screen.queryByTestId("review-draft")).toBeNull();
    expect(screen.getByTestId("rebuild-phase").textContent).toBe(
      "再build予約済み（起動待ち）",
    );
    expect(screen.getByTestId("rebuild-stage-hint").textContent).toContain("plan");
  });

  it("復帰できる版がないときは409の内容を表示し、ドラフトを残す", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/review-chat") && !url.endsWith("/revert")) {
        return jsonResponse({ received: true, sequence: 1, draft: DRAFT_KEEP_LONGER });
      }
      if (url.endsWith("/review-chat/revert")) {
        return jsonResponse(
          {
            error: {
              code: "nothing-to-revert",
              detail: "plan v1 is the bootstrap version; no previous version to restore",
            },
          },
          409,
        );
      }
      throw new Error(`unexpected url: ${url}`);
    });

    render(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => 1.5}
        status={statusOf([])}
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );
    fireEvent.change(screen.getByLabelText("気になるところを伝える"), {
      target: { value: "この後2秒残して" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));
    await waitFor(() => {
      expect(screen.getByTestId("review-draft")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("review-revert-button"));
    await waitFor(() => {
      expect(screen.getByTestId("error-notice")).toBeTruthy();
    });
    expect(screen.getByTestId("error-notice").textContent).toContain("nothing-to-revert");
    expect(screen.getByTestId("review-draft")).toBeTruthy();
    expect(screen.queryByTestId("review-revert-note")).toBeNull();
  });

  it("runner起動失敗の復帰は版が戻った事実と失敗を正確に表示し、同じボタンで再試行できる", async () => {
    const revertBodies: { url: string; init?: RequestInit }[] = [];
    let resolveRevert!: (value: Response) => void;
    const fetchImpl = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/review-chat")) {
        return Promise.resolve(
          jsonResponse({ received: true, sequence: 1, draft: DRAFT_KEEP_LONGER }),
        );
      }
      if (url.endsWith("/review-chat/revert")) {
        revertBodies.push({ url, init });
        return new Promise<Response>((resolve) => {
          resolveRevert = resolve;
        });
      }
      throw new Error(`unexpected url: ${url}`);
    });

    render(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => 1.5}
        status={statusOf([])}
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );
    fireEvent.change(screen.getByLabelText("気になるところを伝える"), {
      target: { value: "この後2秒残して" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));
    await waitFor(() => {
      expect(screen.getByTestId("review-draft")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("review-revert-button"));
    resolveRevert(jsonResponse(REVERT_REBUILD_START_FAILED));

    await waitFor(() => {
      expect(screen.getByTestId("review-revert-note")).toBeTruthy();
    });
    // 版の復帰は成功している（new_version あり）→ 正確にその状態を表示する。
    expect(screen.getByTestId("review-revert-note").textContent).toContain(
      "版は戻っています（v3）",
    );
    expect(screen.getByTestId("review-revert-note").textContent).toContain(
      "再構築の起動に失敗しました",
    );
    // 予約済み表示は嘘になるため出さない。
    expect(screen.queryByTestId("rebuild-phase")).toBeNull();
    expect(screen.getByTestId("review-revert-rebuild-failed").textContent).toContain(
      "もう一度「前の状態に戻す」を押すと",
    );
    // 同じボタンが再試行（再送）の入口として残る。
    expect(
      (screen.getByTestId("review-revert-button") as HTMLButtonElement).disabled,
    ).toBe(false);

    fireEvent.click(screen.getByTestId("review-revert-button"));
    resolveRevert(jsonResponse(REVERT_SUCCESS));

    await waitFor(() => {
      expect(screen.getByTestId("review-revert-note").textContent).toContain(
        "版を戻しました（v3）",
      );
    });
    expect(revertBodies).toHaveLength(2);
    expect(revertBodies[1].init?.method).toBe("POST");
  });
});
