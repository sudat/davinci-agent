import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import ApprovalSessions from "@/components/ApprovalSessions";

const TWO_SESSIONS = {
  available: true,
  blocking_session_count: 2,
  sessions: [
    {
      session_key: "editorial-presentation",
      kind: "normal",
      purposes: ["editorial", "presentation"],
      items: [
        { record_id: "opr-00000001", purpose: "editorial", target_hash: "1".repeat(64) },
        { record_id: "opr-00000002", purpose: "presentation", target_hash: "2".repeat(64) },
      ],
      explanation: null,
    },
    {
      session_key: "final-publication",
      kind: "normal",
      purposes: ["final", "publication"],
      items: [
        { record_id: "opr-00000003", purpose: "final", target_hash: "3".repeat(64) },
        { record_id: "opr-00000004", purpose: "publication", target_hash: "4".repeat(64) },
      ],
      explanation: null,
    },
  ],
  decided: [],
};

const ALL_DECIDED = {
  available: true,
  blocking_session_count: 0,
  sessions: [],
  decided: [
    { record_id: "opr-00000005", purpose: "editorial", decision: "approve" },
    { record_id: "opr-00000006", purpose: "presentation", decision: "approve" },
    { record_id: "opr-00000007", purpose: "final", decision: "approve" },
    { record_id: "opr-00000008", purpose: "publication", decision: "approve" },
  ],
};

const EXCEPTION_SESSION = {
  available: true,
  blocking_session_count: 3,
  sessions: [
    ...TWO_SESSIONS.sessions,
    {
      session_key: "privacy",
      kind: "exception",
      purposes: ["privacy"],
      items: [
        { record_id: "opr-00000009", purpose: "privacy", target_hash: "9".repeat(64) },
      ],
      explanation: "プライバシー確認は他の承認と併合できません。",
    },
  ],
  decided: [],
};

function jsonResponse(payload: object): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

describe("ApprovalSessions（bundling + 承認フロー）", () => {
  it("2つのnormalセッションが表示され、1クリックで両方の承認を実行する", async () => {
    let approved: string[] = [];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === "POST") {
        approved.push(url.split("/").pop() ?? "");
        return jsonResponse({
          record_id: url.split("/").pop(),
          decision: "approve",
          superseded_record_id: null,
          runner_class: "automation",
          fixture_only: true,
        });
      }
      return jsonResponse(approved.length >= 2 ? ALL_DECIDED : TWO_SESSIONS);
    });

    render(
      <ApprovalSessions
        episodeId="ep-abc"
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("blocking-session-count").textContent).toContain("2");
    });
    expect(screen.getAllByTestId("approval-session")).toHaveLength(2);
    expect(screen.getAllByTestId("approval-item")).toHaveLength(4);

    const firstSession = screen
      .getAllByTestId("approval-session")
      .find((node) => node.dataset.sessionKey === "editorial-presentation");
    expect(firstSession).toBeDefined();
    fireEvent.click(
      within(firstSession!).getByTestId("approve-session-button") as HTMLButtonElement,
    );

    await waitFor(() => {
      expect(screen.getByTestId("approvals-empty")).toBeTruthy();
    });
    expect(screen.getAllByTestId("decided-item")).toHaveLength(4);
    expect(screen.getByTestId("blocking-session-count").textContent).toContain("0");
    expect(approved).toEqual(["opr-00000001", "opr-00000002"]);
  });

  it("例外セッションはgrouped説明付きで追加stopとして出る", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse(EXCEPTION_SESSION));
    render(
      <ApprovalSessions
        episodeId="ep-abc"
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("blocking-session-count").textContent).toContain("3");
    });
    const privacy = screen
      .getAllByTestId("approval-session")
      .find((node) => node.dataset.sessionKey === "privacy");
    expect(privacy).toBeDefined();
    expect(
      within(privacy!).getByTestId("session-explanation").textContent,
    ).toContain("プライバシー確認");
  });
});
