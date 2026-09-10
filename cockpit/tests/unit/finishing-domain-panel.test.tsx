import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import FinishingDomainPanel from "@/components/FinishingDomainPanel";
import type { FinishingStatusPayload } from "@/lib/api";

const SEVEN: FinishingStatusPayload = {
  available: true,
  episode_id: "ep-abc",
  run_id: "run-1",
  domains: [
    { domain: "editorial_construction", status: "applied", justification: null, blocked: false },
    { domain: "subtitle", status: "applied", justification: null, blocked: false },
    { domain: "audio_finishing", status: "applied", justification: null, blocked: false },
    { domain: "color_finishing", status: "applied", justification: null, blocked: false },
    {
      domain: "framing_motion",
      status: "intentionally_not_needed",
      justification: "no framing/motion treatment requested for this episode",
      blocked: false,
    },
    {
      domain: "graphics_presentation",
      status: "intentionally_not_needed",
      justification: "no graphics/presentation treatment requested for this episode",
      blocked: false,
    },
    {
      domain: "delivery_qc",
      status: "blocked",
      justification: "delivery QC did not pass for this episode",
      blocked: true,
    },
  ],
};

function jsonResponse(payload: object, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("FinishingDomainPanel", () => {
  it("未実施では何も出さない（プレースホルダなし・エラー扱いしない・chipなし）", async () => {
    const fetchImpl: typeof fetch = async () =>
      jsonResponse({ available: false, domains: [] });

    const { container } = render(<FinishingDomainPanel episodeId="ep-abc" fetchImpl={fetchImpl} />);

    await waitFor(() => {
      expect(container.querySelector('[data-testid="finishing-domain-panel"]')).toBeNull();
    });
    expect(container.querySelector('[data-testid="finishing-chip"]')).toBeNull();
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it("手動対応・ブロックだけ通常表示し、7ドメイン全覧は詳しい記録に移す", async () => {
    const fetchImpl: typeof fetch = async () => jsonResponse(SEVEN);

    render(<FinishingDomainPanel episodeId="ep-abc" fetchImpl={fetchImpl} />);

    const actions = await waitFor(() => {
      const found = within(screen.getByTestId("finishing-action-list")).getAllByTestId(
        "finishing-domain-row",
      );
      expect(found).toHaveLength(1);
      return found;
    });
    expect(actions[0]!.getAttribute("data-domain")).toBe("delivery_qc");
    expect(actions[0]!.getAttribute("data-status")).toBe("blocked");
    expect(within(actions[0] as HTMLElement).getByText("ブロック中")).toBeVisible();
    expect(within(actions[0] as HTMLElement).getByTestId("finishing-action").textContent).toContain(
      "止まっている理由を確認して",
    );

    const all = within(screen.getByTestId("finishing-all-domains")).getAllByTestId(
      "finishing-domain-row",
    );
    expect(all).toHaveLength(7);
    expect(all.map((row) => row.getAttribute("data-domain"))).toEqual([
      "editorial_construction",
      "subtitle",
      "audio_finishing",
      "color_finishing",
      "framing_motion",
      "graphics_presentation",
      "delivery_qc",
    ]);
    const details = screen.getByTestId("finishing-all-domains");
    expect(within(details).getByText("編集組み立て")).toBeTruthy();
    expect(within(details).getByText("字幕")).toBeTruthy();
    expect(within(details).getByText("音声仕上げ")).toBeTruthy();
    expect(within(details).getByText("色補正")).toBeTruthy();
    expect(within(details).getByText("フレーミング／拡大縮小")).toBeTruthy();
    expect(within(details).getByText("画面表示（テロップ等）")).toBeTruthy();
    expect(within(details).getByText("納品前品質確認")).toBeTruthy();
    expect(within(details).getAllByText("適用済み")).toHaveLength(4);
    expect(within(details).getAllByText("意図的に不要")).toHaveLength(2);
    expect(within(details).getByText("ブロック中")).toBeTruthy();
    expect(within(details).getAllByText("今回はこの項目を実施していません。")).toHaveLength(2);
    expect(within(details).getByText("この項目で処理が止まっています。")).toBeTruthy();
    const justifications = within(details).getAllByTestId("finishing-justification");
    expect(justifications).toHaveLength(3);
    expect(justifications[0]!.textContent).toBe(
      "no framing/motion treatment requested for this episode",
    );
    expect(justifications[2]!.textContent).toBe("delivery QC did not pass for this episode");
    expect(screen.queryByText("editorial_construction")).toBeNull();
    expect(within(details).getAllByText("記録された理由（原文）")).toHaveLength(3);
  });

  it("意図的に不要は適用済みと視覚的に区別される（chip classが異なる）", async () => {
    const fetchImpl: typeof fetch = async () => jsonResponse(SEVEN);

    render(<FinishingDomainPanel episodeId="ep-abc" fetchImpl={fetchImpl} />);

    await waitFor(() => {
      expect(
        within(screen.getByTestId("finishing-all-domains")).getAllByTestId(
          "finishing-domain-row",
        ),
      ).toHaveLength(7);
    });
    const chips = within(screen.getByTestId("finishing-all-domains")).getAllByTestId(
      "finishing-chip",
    );
    const appliedChip = chips.find((chip) => chip.getAttribute("data-status") === "applied")!;
    const intentionallyChip = chips.find(
      (chip) => chip.getAttribute("data-status") === "intentionally_not_needed",
    )!;
    expect(appliedChip.className).not.toBe(intentionallyChip.className);
    expect(appliedChip.className).toContain("finishing-chip-applied");
    expect(intentionallyChip.className).toContain("finishing-chip-intentionally-not-needed");
  });

  it("4つの既知statusはそれぞれ固有のchip classを持つ", async () => {
    const payload: FinishingStatusPayload = {
      available: true,
      episode_id: "ep-mix",
      run_id: "run-mix",
      domains: [
        { domain: "editorial_construction", status: "applied", justification: null, blocked: false },
        {
          domain: "subtitle",
          status: "intentionally_not_needed",
          justification: "skip",
          blocked: false,
        },
        {
          domain: "audio_finishing",
          status: "manual_fallback_required",
          justification: "needs manual",
          blocked: false,
        },
        { domain: "color_finishing", status: "blocked", justification: "blocked", blocked: true },
        { domain: "framing_motion", status: "applied", justification: null, blocked: false },
        { domain: "graphics_presentation", status: "applied", justification: null, blocked: false },
        { domain: "delivery_qc", status: "applied", justification: null, blocked: false },
      ],
    };
    const fetchImpl: typeof fetch = async () => jsonResponse(payload);

    render(<FinishingDomainPanel episodeId="ep-mix" fetchImpl={fetchImpl} />);

    await waitFor(() => {
      expect(
        within(screen.getByTestId("finishing-action-list")).getAllByTestId(
          "finishing-domain-row",
        ),
      ).toHaveLength(2);
    });
    const actionDomains = within(screen.getByTestId("finishing-action-list"))
      .getAllByTestId("finishing-domain-row")
      .map((row) => row.getAttribute("data-domain"));
    expect(actionDomains).toEqual(["audio_finishing", "color_finishing"]);
    const byStatus = Object.fromEntries(
      within(screen.getByTestId("finishing-all-domains"))
        .getAllByTestId("finishing-chip")
        .map((chip) => [chip.getAttribute("data-status"), chip.className]),
    );
    expect(byStatus["applied"]).toContain("finishing-chip-applied");
    expect(byStatus["intentionally_not_needed"]).toContain("finishing-chip-intentionally-not-needed");
    expect(byStatus["manual_fallback_required"]).toContain("finishing-chip-manual");
    expect(byStatus["blocked"]).toContain("finishing-chip-blocked");
    expect(screen.getAllByText("手動対応必要")).toHaveLength(2);
  });

  it("未知のstatusは生のstatus文字列を表示し、unknown chipになる", async () => {
    const payload: FinishingStatusPayload = {
      available: true,
      episode_id: "ep-x",
      run_id: "run-x",
      domains: [
        { domain: "editorial_construction", status: "weird_future_status", justification: "future", blocked: false },
        { domain: "subtitle", status: "applied", justification: null, blocked: false },
        { domain: "audio_finishing", status: "applied", justification: null, blocked: false },
        { domain: "color_finishing", status: "applied", justification: null, blocked: false },
        { domain: "framing_motion", status: "applied", justification: null, blocked: false },
        { domain: "graphics_presentation", status: "applied", justification: null, blocked: false },
        { domain: "delivery_qc", status: "applied", justification: null, blocked: false },
      ],
    };
    const fetchImpl: typeof fetch = async () => jsonResponse(payload);

    render(<FinishingDomainPanel episodeId="ep-x" fetchImpl={fetchImpl} />);

    await waitFor(() => {
      expect(screen.getAllByText("weird_future_status")).toHaveLength(2);
    });
    const unknown = within(screen.getByTestId("finishing-action-list")).getByText(
      "weird_future_status",
    );
    expect(unknown).toBeVisible();
    expect(unknown.className).toContain("finishing-chip-unknown");
  });

  it("422はエラー envelope の code/detail をそのまま表示する", async () => {
    const fetchImpl: typeof fetch = async () =>
      jsonResponse({ error: { code: "finishing-run-invalid", detail: "not a valid report" } }, 422);

    render(<FinishingDomainPanel episodeId="ep-abc" fetchImpl={fetchImpl} />);

    await waitFor(() => {
      expect(screen.getByTestId("error-notice")).toBeTruthy();
    });
    expect(screen.getByTestId("error-notice").textContent).toContain("[finishing-run-invalid]");
    expect(screen.getByTestId("error-notice").textContent).toContain("not a valid report");
  });

  it("404はエラー扱いせずパネルごと黙って消える（episode存在はstatus pollが担う）", async () => {
    const fetchImpl: typeof fetch = async () => new Response("", { status: 404 });

    const { container } = render(<FinishingDomainPanel episodeId="ep-abc" fetchImpl={fetchImpl} />);

    await waitFor(() => {
      expect(container.querySelector('[data-testid="finishing-domain-panel"]')).toBeNull();
    });
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it("未実施から完了へ自動更新する", async () => {
    vi.useFakeTimers();
    let callCount = 0;
    const fetchImpl: typeof fetch = async () => {
      callCount += 1;
      return jsonResponse(callCount === 1 ? { available: false, domains: [] } : SEVEN);
    };

    const { container } = render(<FinishingDomainPanel episodeId="ep-abc" fetchImpl={fetchImpl} />);
    await vi.waitFor(() => {
      expect(container.querySelector('[data-testid="finishing-domain-panel"]')).toBeNull();
    });

    await vi.advanceTimersByTimeAsync(2000);

    await vi.waitFor(() => {
      expect(
        within(screen.getByTestId("finishing-action-list")).getAllByTestId(
          "finishing-domain-row",
        ),
      ).toHaveLength(1);
    });
    expect(
      within(screen.getByTestId("finishing-all-domains")).getAllByTestId(
        "finishing-domain-row",
      ),
    ).toHaveLength(7);
    expect(callCount).toBe(2);
    vi.useRealTimers();
  });
});
