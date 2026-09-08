import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import EpisodeView from "@/components/EpisodeView";

/**
 * Component-level integration over mocked fetch (documented per task 46:
 * jsdom cannot boot the real backend; payload SHAPES are the task-44
 * contracts read from services/episode_cockpit/api.py + backend.py —
 * both the unbacked (today's real) shape and the forward shape with
 * eta_minutes/work_units/before_after/at_seconds are exercised).
 */

function jsonResponse(url: string, status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", url },
  });
}

type Routes = {
  status: () => Response;
  flags: () => Response;
  preview: () => Response;
};

function stubApi(routes: Routes): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
    const url = String(input);
    if (url.endsWith("/flags")) return Promise.resolve(routes.flags());
    if (url.endsWith("/preview")) return Promise.resolve(routes.preview());
    if (url.includes("/consultation")) {
      return Promise.resolve(jsonResponse("c", 200, { consultations: [] }));
    }
    return Promise.resolve(routes.status());
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const unbackedStatus = () =>
  jsonResponse("s", 200, {
    episode_id: "ep-unit01",
    job_id: "ep-unit01",
    status: "CREATED",
    current_stage: "intake",
    created_at_seq: 1,
    updated_at_seq: 1,
    stage_runs: [],
  });

const notYetFlags = () => jsonResponse("f", 200, { flags: [], not_yet_generated: true });

describe("EpisodeView — 計測裏付けのない実際のpayload形状（task-44現行）", () => {
  it("stage/progressのみ表示：ETA捏造なし・preview未生成・flags未生成", async () => {
    stubApi({
      status: unbackedStatus,
      flags: notYetFlags,
      preview: () => new Response(null, { status: 404 }),
    });

    render(<EpisodeView episodeId="ep-unit01" />);

    expect(await screen.findByTestId("episode-status")).toHaveTextContent("CREATED");
    expect(screen.getByTestId("current-stage")).toHaveTextContent("intake");
    await waitFor(() =>
      expect(screen.getByTestId("preview-pending")).toBeVisible(),
    );
    expect(screen.getByTestId("flags-empty")).toBeVisible();
    expect(screen.queryByTestId("eta")).toBeNull();
    expect(screen.queryByTestId("before-after")).toBeNull();
    expect(screen.queryByTestId("preview-player")).toBeNull();
  });
});

describe("EpisodeView — 計測裏付けのある前方互換payload形状", () => {
  it("ETA・作業単位・before/after・flag seekがすべて動作する", async () => {
    stubApi({
      status: () =>
        jsonResponse("s", 200, {
          episode_id: "ep-unit02",
          job_id: "ep-unit02",
          status: "PREVIEW_READY",
          current_stage: "review",
          created_at_seq: 1,
          updated_at_seq: 9,
          stage_runs: [
            { stage_name: "ingest", status: "succeeded", retry_count: 0, last_error_code: null },
          ],
          eta_minutes: 8,
          work_units: { completed: 5, remaining: 1 },
          before_after: {
            summary: "指摘2件反映",
            items: [{ label: "冒頭", before: "12秒", after: "3秒" }],
          },
        }),
      flags: () =>
        jsonResponse("f", 200, {
          flags: [
            {
              sequence: 1,
              kind: "proposal_recorded",
              reason: "冒頭を削除",
              at_seconds: 1.25,
            },
          ],
          not_yet_generated: false,
        }),
      preview: () => new Response(null, { status: 200 }),
    });

    render(<EpisodeView episodeId="ep-unit02" />);

    expect(await screen.findByTestId("preview-player")).toBeVisible();
    expect(screen.getByTestId("eta").textContent).toContain("8");
    expect(screen.getByTestId("work-units").textContent).toContain("5");
    expect(screen.getByTestId("before-after").textContent).toContain("指摘2件反映");

    const video = screen.getByTestId("preview-player") as HTMLVideoElement;
    const jump = await screen.findByTestId("flag-jump");
    expect(jump).toBeEnabled();
    fireEvent.click(jump);
    expect(video.currentTime).toBeCloseTo(1.25, 5);
  });
});

describe("EpisodeView — 不正なepisode（malformed input）", () => {
  it("404構造化errorを表示し、クラッシュしない", async () => {
    stubApi({
      status: () =>
        jsonResponse("s", 404, {
          error: { code: "episode-not-found", detail: "no episode ep-gone" },
        }),
      flags: notYetFlags,
      preview: () => new Response(null, { status: 404 }),
    });

    render(<EpisodeView episodeId="ep-gone" />);

    const notice = await screen.findByTestId("error-notice");
    expect(notice.textContent).toContain("episode-not-found");
    expect(screen.getByText("このエピソードは見つかりません。")).toBeVisible();
  });
});
