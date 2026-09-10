import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import SelfCheckSection from "@/components/SelfCheckSection";
import type { EpisodeStatus } from "@/lib/api";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const emptyStatus = {
  episode_id: "ep-sc01",
  job_id: "ep-sc01",
  status: "PREVIEW_READY",
  current_stage: "review",
  created_at_seq: 1,
  updated_at_seq: 9,
  stage_runs: [],
} as unknown as EpisodeStatus;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SelfCheckSection — 送信形状（未回答nullを含む）", () => {
  it("3問すべて未回答のまま＋メモなしでnull3つ・空メモを送る", async () => {
    const seen: { url: string; body: unknown }[] = [];
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/self-check") && (init?.method ?? "GET") === "GET") {
        return Promise.resolve(
          jsonResponse(200, { episode_id: "ep-sc01", self_check: null }),
        );
      }
      seen.push({ url, body: init?.body !== undefined ? JSON.parse(String(init.body)) : null });
      return Promise.resolve(
        jsonResponse(200, {
          episode_id: "ep-sc01",
          answered_at: "2026-09-10T00:00:00+00:00",
          q_instruction_transmitted: null,
          q_better_than_before: null,
          q_want_to_publish: null,
          note: "",
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SelfCheckSection episodeId="ep-sc01" status={emptyStatus} />);

    const submit = await screen.findByTestId("self-check-submit");
    fireEvent.click(submit);

    await waitFor(() =>
      expect(screen.getByTestId("self-check-sent")).toHaveTextContent("送信しました"),
    );
    expect(seen).toHaveLength(1);
    expect(seen[0].body).toEqual({
      q_instruction_transmitted: null,
      q_better_than_before: null,
      q_want_to_publish: null,
      note: "",
    });
  });

  it("はい・いいえ・未回答の混在とメモをそのまま送る", async () => {
    let sentBody: unknown = null;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/self-check") && (init?.method ?? "GET") === "GET") {
        return Promise.resolve(
          jsonResponse(200, { episode_id: "ep-sc01", self_check: null }),
        );
      }
      sentBody = init?.body !== undefined ? JSON.parse(String(init.body)) : null;
      return Promise.resolve(
        jsonResponse(200, {
          episode_id: "ep-sc01",
          answered_at: "2026-09-10T00:00:00+00:00",
          ...(sentBody as Record<string, unknown>),
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SelfCheckSection episodeId="ep-sc01" status={emptyStatus} />);
    await screen.findByTestId("self-check-submit");

    fireEvent.click(screen.getByTestId("self-check-q1-yes"));
    fireEvent.click(screen.getByTestId("self-check-q2-no"));
    fireEvent.change(screen.getByTestId("self-check-note"), {
      target: { value: "字幕だけ気になる" },
    });
    fireEvent.click(screen.getByTestId("self-check-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("self-check-sent")).toHaveTextContent("送信しました"),
    );
    expect(sentBody).toEqual({
      q_instruction_transmitted: true,
      q_better_than_before: false,
      q_want_to_publish: null,
      note: "字幕だけ気になる",
    });
  });
});

describe("SelfCheckSection — 正直な失敗表示", () => {
  it("送信失敗は理由を表示し、送信しましたを出さない", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/self-check") && (init?.method ?? "GET") === "GET") {
        return Promise.resolve(
          jsonResponse(200, { episode_id: "ep-sc01", self_check: null }),
        );
      }
      return Promise.resolve(
        jsonResponse(500, { error: { code: "self-check-store-failed", detail: "disk full" } }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SelfCheckSection episodeId="ep-sc01" status={emptyStatus} />);
    fireEvent.click(await screen.findByTestId("self-check-submit"));

    const alert = await screen.findByTestId("self-check-error");
    expect(alert.textContent).toContain("self-check-store-failed");
    expect(screen.queryByTestId("self-check-sent")).toBeNull();
  });
});

describe("SelfCheckSection — 旧バックエンド耐性と再読込復元", () => {
  it("GET 404ではセクション全体を出さない", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        jsonResponse(404, { error: { code: "not-found", detail: "no route" } }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<SelfCheckSection episodeId="ep-sc01" status={emptyStatus} />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByTestId("self-check-section")).toBeNull();
    expect(screen.queryByTestId("self-check-submit")).toBeNull();
  });

  it("statusのself_checkから再読込で回答を復元する", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        jsonResponse(200, { episode_id: "ep-sc01", self_check: null }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const withSaved = {
      ...emptyStatus,
      self_check: {
        episode_id: "ep-sc01",
        answered_at: "2026-09-10T00:00:00+00:00",
        q_instruction_transmitted: true,
        q_better_than_before: false,
        q_want_to_publish: null,
        note: "前回メモ",
      },
    } as unknown as EpisodeStatus;

    render(<SelfCheckSection episodeId="ep-sc01" status={withSaved} />);

    expect(await screen.findByTestId("self-check-section")).toBeVisible();
    await waitFor(() =>
      expect(screen.getByTestId("self-check-q1-current")).toHaveTextContent("はい"),
    );
    expect(screen.getByTestId("self-check-q2-current")).toHaveTextContent("いいえ");
    expect(screen.getByTestId("self-check-q3-current")).toHaveTextContent("未回答");
    expect(screen.getByTestId("self-check-note")).toHaveValue("前回メモ");
  });
});
