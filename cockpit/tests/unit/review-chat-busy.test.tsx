import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import ReviewChatPanel from "@/components/ReviewChatPanel";
import type { EpisodeStatus } from "@/lib/api";

/**
 * 工程2P 送信中ラベルと長時間注意（D表: 30秒/120秒はchat送信の追加閾値）。
 * ボタンは自分の処理中だけ処理中語に変わり、30秒で「長くかかっています」、
 * 120秒で再送検討の語が出る。時計は固定して判定を決定論的にする。
 */

const DRAFT = {
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

const APPLY_RESULT = {
  applied: {
    schema_version: "cockpit-applied-command-v1",
    command_id: DRAFT.command_id,
    command_kind: "keep_longer",
    affected_domain: "edit_plan",
    event_id: "e".repeat(64),
    base_plan_version: "v1",
    result_plan_version: "v2",
    deferred: false,
    reason: null,
    target_seconds: 1,
    seconds_delta: 2,
  },
  rebuild: {
    schema_version: "cockpit-rebuild-plan-v1",
    command_id: DRAFT.command_id,
    command_kind: "keep_longer",
    affected_domain: "edit_plan",
    stages: ["plan", "compile", "preview"],
    excluded_stages: [],
  },
};

const REBUILD_SCHEDULED = {
  stage_hint: "plan,compile,preview",
  scheduled: true,
  applied_command: DRAFT.command_id,
};

function jsonResponse(payload: object, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const status: EpisodeStatus = {
  episode_id: "ep-busy01",
  job_id: "ep-busy01",
  status: "PREVIEW_READY",
  current_stage: "review",
  created_at_seq: 1,
  updated_at_seq: 2,
  stage_runs: [],
};

function panelWithPendingChat(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/review-chat")) {
        return new Promise<Response>(() => {}); // pending forever
      }
      return Promise.resolve(jsonResponse(REBUILD_SCHEDULED, 202));
    }),
  );
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function sendAndAssertBusy(): Promise<void> {
  render(
    <ReviewChatPanel episodeId="ep-busy01" getAtSeconds={() => null} status={status} />,
  );
  fireEvent.change(screen.getByLabelText("気になるところを伝える"), {
    target: { value: "この後2秒残して" },
  });
  fireEvent.click(screen.getByTestId("review-chat-send"));
  await vi.advanceTimersByTimeAsync(0);
}

describe("ReviewChatPanel — 送信中ラベルと長時間注意", () => {
  it("送信処理中はボタンが送信中…になり、30秒で長くかかっています", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-08T12:00:00Z"));
    panelWithPendingChat();
    await sendAndAssertBusy();
    const send = screen.getByTestId("review-chat-send");
    expect(send.textContent).toBe("送信中…");
    expect(screen.queryByTestId("chat-long-warn")).toBeNull();

    await vi.advanceTimersByTimeAsync(30_000);
    expect(send.textContent).toBe("送信中…");
    expect(screen.getByTestId("chat-long-warn").textContent).toContain("長くかかっています");

    // 120秒で再送検討の語に変わる（frames付きcodex経路のtimeout根拠）
    await vi.advanceTimersByTimeAsync(90_000);
    expect(screen.getByTestId("chat-long-warn").textContent).toContain("もう一度送信");
  });

  it("送信が終わればラベルと注意は消える", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-08T12:00:00Z"));
    let resolveChat!: (value: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn((_input: RequestInfo | URL) => {
        return new Promise<Response>((resolve) => {
          resolveChat = resolve;
        });
      }),
    );
    render(
      <ReviewChatPanel episodeId="ep-busy01" getAtSeconds={() => null} status={status} />,
    );
    fireEvent.change(screen.getByLabelText("気になるところを伝える"), {
      target: { value: "この後2秒残して" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));
    await vi.advanceTimersByTimeAsync(31_000);
    expect(screen.getByTestId("chat-long-warn")).toBeTruthy();
    resolveChat(
      jsonResponse({ received: true, sequence: 1, draft: DRAFT }),
    );
    // waitFor cannot advance vitest fake timers — flush deterministically.
    await vi.advanceTimersByTimeAsync(32_000);
    expect(screen.getByTestId("review-chat-send").textContent).toBe("この位置を修正する");
    expect(screen.queryByTestId("chat-long-warn")).toBeNull();
  });

  it("適用処理中はボタンが適用中…になる", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/review-chat")) {
          return Promise.resolve(jsonResponse({ received: true, sequence: 1, draft: DRAFT }));
        }
        if (url.endsWith("/review-chat/apply")) {
          return new Promise<Response>(() => {}); // pending forever
        }
        return Promise.resolve(jsonResponse(REBUILD_SCHEDULED, 202));
      }),
    );
    render(
      <ReviewChatPanel episodeId="ep-busy01" getAtSeconds={() => null} status={status} />,
    );
    fireEvent.change(screen.getByLabelText("気になるところを伝える"), {
      target: { value: "この後2秒残して" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));
    await waitFor(() => {
      expect(screen.getByTestId("review-draft")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("review-apply-button"));
    await waitFor(() => {
      expect(screen.getByTestId("review-apply-button").textContent).toBe("適用中…");
    });
  });

  it("復帰処理中はボタンが復帰中…になる", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/review-chat/revert")) {
          return new Promise<Response>(() => {}); // pending forever
        }
        if (url.endsWith("/review-chat")) {
          return Promise.resolve(jsonResponse({ received: true, sequence: 1, draft: DRAFT }));
        }
        return Promise.resolve(jsonResponse(REBUILD_SCHEDULED, 202));
      }),
    );
    render(
      <ReviewChatPanel episodeId="ep-busy01" getAtSeconds={() => null} status={status} />,
    );
    fireEvent.click(screen.getByTestId("review-revert-button"));
    await waitFor(() => {
      expect(screen.getByTestId("review-revert-button").textContent).toBe("復帰中…");
    });
  });
});
