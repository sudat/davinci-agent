import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import ReviewChatPanel from "@/components/ReviewChatPanel";

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

const DRAFT_AMBIGUOUS = {
  ...DRAFT_KEEP_LONGER,
  command_id: "rcmd-ffffffffffff",
  command_kind: null,
  text: "ありがとうございます",
  needs_confirmation: true,
  confirmation_reason: "no known command kind matched the message; restate the correction",
  target_seconds: null,
  seconds_delta: null,
};

const APPLIED = {
  schema_version: "cockpit-applied-command-v1",
  command_id: "rcmd-0123456789ab",
  command_kind: "keep_longer",
  affected_domain: "edit_plan",
  event_id: "e".repeat(64),
  base_plan_version: "v1",
  result_plan_version: "v2",
  deferred: false,
  reason: null,
  target_seconds: 1,
  seconds_delta: 2,
};

const REBUILD_PLAN = {
  schema_version: "cockpit-rebuild-plan-v1",
  command_id: "rcmd-0123456789ab",
  command_kind: "keep_longer",
  affected_domain: "edit_plan",
  stages: ["plan", "compile", "preview", "resolve_build", "qc", "render"],
  excluded_stages: ["ingest", "normalize", "analyze", "selection", "publish"],
};

const REBUILD_202 = {
  stage_hint: "plan,compile,preview,resolve_build,qc,render",
  scheduled: false,
  note: "rebuild scheduling is not implemented yet; intent recorded",
  applied_command: "rcmd-0123456789ab",
  rebuild_stages: REBUILD_PLAN.stages,
};

function jsonResponse(payload: object, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("ReviewChatPanel（NL修正→構造化プレビュー→部分rebuild）", () => {
  it("送信すると解釈ドラフトがエコーされ、適用で202とstage hintが見える", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/review-chat")) {
        expect(init?.method).toBe("POST");
        expect(JSON.parse(init!.body as string)).toEqual({
          text: "この後2秒残して",
          at_seconds: 1.5,
        });
        return jsonResponse({ received: true, sequence: 1, draft: DRAFT_KEEP_LONGER });
      }
      if (url.endsWith("/review-chat/apply")) {
        return jsonResponse({ applied: APPLIED, rebuild: REBUILD_PLAN });
      }
      if (url.endsWith("/rebuild")) {
        return jsonResponse(REBUILD_202, 202);
      }
      throw new Error(`unexpected url: ${url}`);
    });

    render(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => 1.5}
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );

    const send = screen.getByTestId("review-chat-send");
    expect((send as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("修正指示（自然言語）"), {
      target: { value: "この後2秒残して" },
    });
    expect((send as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(send);

    await waitFor(() => {
      expect(screen.getByTestId("review-draft")).toBeTruthy();
    });
    expect(screen.getByTestId("review-draft-kind").textContent).toBe("長めに残す");
    expect(screen.getByTestId("review-draft-delta").textContent).toBe("+2s");
    expect(screen.queryByTestId("review-draft-needs-confirmation")).toBeNull();

    fireEvent.click(screen.getByTestId("review-apply-button"));

    await waitFor(() => {
      expect(screen.getByTestId("rebuild-indicator")).toBeTruthy();
    });
    expect(screen.getByTestId("rebuild-indicator").textContent).toContain("202");
    expect(screen.getByTestId("rebuild-stage-hint").textContent).toContain("plan");
    expect(screen.getByTestId("rebuild-stage-hint").textContent).toContain("render");
  });

  it("曖昧な入力は確認理由付きで出るだけで適用できない", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ received: true, sequence: 2, draft: DRAFT_AMBIGUOUS }),
    );
    render(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => null}
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );

    fireEvent.change(screen.getByLabelText("修正指示（自然言語）"), {
      target: { value: "ありがとうございます" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));

    await waitFor(() => {
      expect(screen.getByTestId("review-draft-needs-confirmation")).toBeTruthy();
    });
    expect((screen.getByTestId("review-apply-button") as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect(screen.queryByTestId("rebuild-indicator")).toBeNull();
  });
});
