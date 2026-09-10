import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import ConsultationSampleSection from "@/components/ConsultationSampleSection";

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

const SCOPE = { composition: true, appearance: false, audio: true };

function renderSection(fetchImpl: typeof fetch, onView = vi.fn()) {
  return render(
    <ConsultationSampleSection
      episodeId="ep-s01"
      consultationId="c-1"
      judgmentId="j-1"
      scope={SCOPE}
      fetchImpl={fetchImpl}
      onView={onView}
    />,
  );
}

function publishedSample(id: string) {
  return {
    sample_id: id,
    status: "published",
    total_seconds: 28.5,
    created_at: "2026-09-10T10:00:00Z",
    published_at: "2026-09-10T10:01:00Z",
  };
}

describe("ConsultationSampleSection（30秒試し動画・全編許可）", () => {
  it("要求→完成でプレビュー要素が配信URLを持ち、区分を表示する", async () => {
    let requested = false;
    const { fetchImpl, calls } = recordingFetch((url, init) => {
      if (url.endsWith("/consultation/samples") && init.method === "POST") {
        requested = true;
        return jsonResponse({ state: "published", sample_id: "sample-abc123", manifest: {} });
      }
      if (url.endsWith("/consultation/samples")) {
        return jsonResponse({
          samples: requested ? [publishedSample("sample-abc123")] : [],
        });
      }
      throw new Error(`unexpected url: ${url}`);
    });
    renderSection(fetchImpl);

    await waitFor(() => {
      expect(screen.getByTestId("consultation-sample-section")).toBeVisible();
    });
    fireEvent.click(screen.getByTestId("sample-request"));

    await waitFor(() => {
      expect(screen.getByTestId("sample-request-state").textContent).toContain(
        "試し動画を保存しました",
      );
    });
    const posts = calls.filter(
      (call) => call.url.endsWith("/consultation/samples") && call.init.method === "POST",
    );
    expect(posts).toHaveLength(1);
    expect(JSON.parse(String(posts[0]!.init.body))).toEqual({
      consultation_id: "c-1",
      judgment_id: "j-1",
      operation_id: expect.any(String),
    });
    const preview = screen.getByTestId("sample-preview") as HTMLVideoElement;
    expect(preview.getAttribute("src")).toContain(
      "/consultation/samples/sample-abc123/preview",
    );
    expect(screen.getByTestId("consultation-sample-kind").textContent).toContain(
      "試し動画（撮影素材から作成）",
    );
  });

  it("作成中は「作成中です」と出し、全編ボタンは出さない", async () => {
    const { fetchImpl } = recordingFetch((url) => {
      if (url.endsWith("/consultation/samples")) {
        return jsonResponse({
          samples: [
            {
              sample_id: "sample-work",
              status: "in_progress",
              total_seconds: null,
              created_at: "2026-09-10T10:00:00Z",
            },
          ],
        });
      }
      throw new Error(`unexpected url: ${url}`);
    });
    renderSection(fetchImpl);

    await waitFor(() => {
      expect(screen.getByTestId("consultation-sample-status").textContent).toContain(
        "作成中です",
      );
    });
    expect(screen.queryByTestId("sample-preview")).toBeNull();
    expect(screen.queryByTestId("full-authorize")).toBeNull();
  });

  it("失敗は理由と「相談を続けられます」を出す", async () => {
    const { fetchImpl } = recordingFetch((url, init) => {
      if (url.endsWith("/consultation/samples") && init.method === "POST") {
        return jsonResponse(
          { error: { code: "sample-windows-invalid", detail: "窓が範囲外です" } },
          422,
        );
      }
      if (url.endsWith("/consultation/samples")) {
        return jsonResponse({ samples: [] });
      }
      throw new Error(`unexpected url: ${url}`);
    });
    renderSection(fetchImpl);

    await waitFor(() => {
      expect(screen.getByTestId("consultation-sample-section")).toBeVisible();
    });
    fireEvent.click(screen.getByTestId("sample-request"));

    await waitFor(() => {
      expect(screen.getByTestId("sample-request-error").textContent).toContain(
        "窓が範囲外です",
      );
    });
    expect(screen.getByTestId("sample-request-error").textContent).toContain(
      "相談を続けられます",
    );
  });

  it("全編の承認は表示中の試し動画のIDを送り、結果にそのIDを名乗る", async () => {
    const { fetchImpl, calls } = recordingFetch((url, init) => {
      if (url.endsWith("/consultation/judgment")) {
        return jsonResponse({ consultations: [] });
      }
      if (url.endsWith("/consultation/samples")) {
        return jsonResponse({
          samples: [publishedSample("sample-old"), publishedSample("sample-new")],
        });
      }
      throw new Error(`unexpected url: ${url}`);
    });
    const onView = vi.fn();
    renderSection(fetchImpl, onView);

    await waitFor(() => {
      expect(screen.getByTestId("full-authorize")).toBeVisible();
    });
    fireEvent.click(screen.getByTestId("full-authorize"));

    await waitFor(() => {
      // The ID itself now lives in 詳しい記録; the done line is ID-free.
      expect(screen.getByTestId("full-authorize-done").textContent).toContain(
        "この方向で全編へ進めます",
      );
      expect(screen.getByTestId("consultation-sample-id").textContent).toContain(
        "sample-new",
      );
    });
    const judgments = calls.filter((call) =>
      call.url.endsWith("/consultation/judgment"),
    );
    expect(judgments).toHaveLength(1);
    expect(JSON.parse(String(judgments[0]!.init.body))).toEqual({
      consultation_id: "c-1",
      proposal_id: null,
      decision: "full_authorized",
      scope: SCOPE,
      note: null,
      sample_id: "sample-new",
      operation_id: expect.any(String),
    });
    expect(screen.getByTestId("full-authorize-done").textContent).toContain(
      "この方向で全編へ進めます（確認した試し動画に基づく）",
    );
    expect(onView).toHaveBeenCalledTimes(1);
  });

  it("再読み込みは一覧の取得から復元する（要求なしでプレビューが出る）", async () => {
    const { fetchImpl, calls } = recordingFetch((url) => {
      if (url.endsWith("/consultation/samples")) {
        return jsonResponse({ samples: [publishedSample("sample-kept")] });
      }
      throw new Error(`unexpected url: ${url}`);
    });
    renderSection(fetchImpl);

    await waitFor(() => {
      expect(screen.getByTestId("sample-preview")).toBeVisible();
    });
    expect(
      (screen.getByTestId("sample-preview") as HTMLVideoElement)
        .getAttribute("src"),
    ).toContain("/consultation/samples/sample-kept/preview");
    expect(
      calls.filter(
        (call) =>
          call.url.endsWith("/consultation/samples") && call.init.method === "POST",
      ),
    ).toHaveLength(0);
  });

  it("旧バックエンド（404）では試し動画UIを出さない", async () => {
    const { fetchImpl } = recordingFetch((url) => {
      if (url.endsWith("/consultation/samples")) {
        return jsonResponse({ error: { code: "not-found", detail: "ない" } }, 404);
      }
      throw new Error(`unexpected url: ${url}`);
    });
    const { container } = renderSection(fetchImpl);

    await waitFor(() => {
      expect(
        container.querySelector('[data-testid="consultation-sample-section"]'),
      ).toBeNull();
    });
  });
});
