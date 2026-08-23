import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import KitPreviewPanel from "@/components/KitPreviewPanel";
import type { KitPreviewsPayload } from "@/lib/api";

const PAYLOAD: KitPreviewsPayload = {
  available: true,
  domains: [
    {
      domain: "subtitle",
      intents: ["keyword_text", "subtitle_track"],
      snippet: {
        origin: "explicit",
        media_path: "/tmp/fixture.mp4",
        start_frame: 0,
        end_frame: 45,
        duration_seconds: 3,
      },
      candidates: [
        {
          recipe_id: "subtitle/emphasis",
          semantic_intent: "keyword_text",
          resolved_params: { emphasis_scale: 1.2, hold_frames: 12 },
          parameter_bounds: {
            emphasis_scale: { min: 1, max: 1.5, default: 1.2 },
            hold_frames: { min: 6, max: 30, default: 12 },
          },
          file: "subtitle/subtitle__emphasis.mp4",
        },
        {
          recipe_id: "subtitle/default",
          semantic_intent: "subtitle_track",
          resolved_params: { font_size: 24, line_length: 32 },
          parameter_bounds: {
            font_size: { min: 12, max: 48, default: 24 },
            line_length: { min: 20, max: 42, default: 32 },
          },
          file: "subtitle/subtitle__default.mp4",
        },
      ],
      selection: null,
    },
  ],
};

function jsonResponse(payload: object, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("KitPreviewPanel", () => {
  it("未生成では構造化された待機状態（videoなし・エラー扱いしない）", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ available: false, domains: [] }),
    );

    const { container } = render(
      <KitPreviewPanel episodeId="ep-abc" fetchImpl={fetchImpl as unknown as typeof fetch} />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("kit-previews-empty")).toBeVisible();
    });
    expect(container.querySelector("video")).toBeNull();
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it("候補ごとの video と param が表示され、採択で POST → 選択状態が反映される", async () => {
    const calls: Array<{ url: string; body: unknown }> = [];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/kit-previews") && init?.method === "GET") {
        const selected = calls.some((call) => call.url.endsWith("/select"));
        const next: KitPreviewsPayload = selected
          ? { ...PAYLOAD, domains: [{ ...PAYLOAD.domains[0]!, selection: "subtitle/default" }] }
          : PAYLOAD;
        return jsonResponse(next);
      }
      if (url.endsWith("/subtitle/select")) {
        calls.push({ url, body: JSON.parse(init!.body as string) });
        return jsonResponse({
          domain: "subtitle",
          recipe_id: "subtitle/default",
          semantic_intent: "subtitle_track",
          note: null,
          recorded_at: "2026-08-23T00:00:00Z",
          entries: 1,
        });
      }
      throw new Error(`unexpected url: ${url}`);
    });

    render(
      <KitPreviewPanel episodeId="ep-abc" fetchImpl={fetchImpl as unknown as typeof fetch} />,
    );

    const videos = await waitFor(() => {
      const found = screen.getAllByTestId("kit-candidate-video");
      expect(found).toHaveLength(2);
      return found;
    });
    expect(videos[0]!.getAttribute("src")).toBe(
      "/cockpit-api/episodes/ep-abc/kit-previews/subtitle/subtitle__emphasis.mp4",
    );
    expect(screen.getByText("font_size=24 line_length=32")).toBeVisible();

    fireEvent.click(screen.getAllByTestId("kit-adopt-button")[1]!);

    await waitFor(() => {
      expect(screen.getByTestId("kit-selection-state")).toBeVisible();
    });
    expect(screen.getByTestId("kit-selection-state").textContent).toContain(
      "subtitle/default",
    );
    expect(calls).toEqual([
      { url: "/cockpit-api/episodes/ep-abc/kit-previews/subtitle/select", body: { recipe_id: "subtitle/default" } },
    ]);
  });

  it("どちらも不要ボタンは recipe_id=null で記録する", async () => {
    const posts: unknown[] = [];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/kit-previews")) {
        return jsonResponse(PAYLOAD);
      }
      if (url.endsWith("/subtitle/select")) {
        posts.push(JSON.parse(init!.body as string));
        return jsonResponse({
          domain: "subtitle",
          recipe_id: null,
          semantic_intent: null,
          note: "どちらも不要/現状維持",
          recorded_at: "2026-08-23T00:00:00Z",
          entries: 1,
        });
      }
      throw new Error(`unexpected url: ${url}`);
    });

    render(
      <KitPreviewPanel episodeId="ep-abc" fetchImpl={fetchImpl as unknown as typeof fetch} />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("kit-none-button")).toBeVisible();
    });
    fireEvent.click(screen.getByTestId("kit-none-button"));

    await waitFor(() => {
      expect(posts).toHaveLength(1);
    });
    expect(posts[0]).toEqual({ recipe_id: null, note: "どちらも不要/現状維持" });
  });
});
