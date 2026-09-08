import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import ReviewChatPanel from "@/components/ReviewChatPanel";
import type { EpisodeStatus } from "@/lib/api";

const DRAFT_INVESTIGATED = {
  schema_version: "cockpit-review-command-draft-v1",
  command_id: "rcmd-feelings00001",
  command_kind: "mark_boring",
  text: "ここ退屈",
  target_seconds: 6,
  seconds_delta: null,
  needs_confirmation: true,
  confirmation_reason:
    "this expresses a feeling, not a concrete change; the cause is investigated before any change is proposed",
  investigated: true,
  hypothesis: "同じ説明が続いているのが原因の可能性",
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

describe("ReviewChatPanel（調査状態の感想表示）", () => {
  it("hypothesis-proposed は AIの仮説ラベル付きで仮説本文を表示し、適用できる", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({
        received: true,
        sequence: 7,
        draft: DRAFT_INVESTIGATED,
        investigated: true,
        hypothesis: DRAFT_INVESTIGATED.hypothesis,
        investigation_state: "hypothesis-proposed",
      }),
    );
    render(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => 6.0}
        status={statusOf([])}
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );
    fireEvent.change(screen.getByLabelText("修正指示（自然言語）"), {
      target: { value: "ここ退屈" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));

    await waitFor(() => {
      expect(screen.getByTestId("review-draft-investigation")).toBeTruthy();
    });
    expect(screen.getByTestId("review-draft-investigation").textContent).toContain(
      "AIの仮説",
    );
    expect(screen.getByTestId("review-draft-investigation").textContent).toContain(
      "同じ説明が続いているのが原因の可能性",
    );
    // 調査済みドラフトの明示的な適用が確認になるため、適用は有効のまま。
    expect((screen.getByTestId("review-apply-button") as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  it("materials-checked は原因特定に至らなかったことを完成形なく表示する", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({
        received: true,
        sequence: 8,
        draft: { ...DRAFT_INVESTIGATED, hypothesis: null },
        investigated: true,
        hypothesis: null,
        investigation_state: "materials-checked",
      }),
    );
    render(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => 6.0}
        status={statusOf([])}
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );
    fireEvent.change(screen.getByLabelText("修正指示（自然言語）"), {
      target: { value: "ここ退屈" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));

    await waitFor(() => {
      expect(screen.getByTestId("review-draft-investigation")).toBeTruthy();
    });
    expect(screen.getByTestId("review-draft-investigation").textContent).toBe(
      "周辺の字幕と場面情報を確認しましたが、原因はまだ特定できていません",
    );
  });

  it("unconfirmed は原因未確認を表示し、完了形の調査文は決して出さない", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({
        received: true,
        sequence: 9,
        draft: { ...DRAFT_INVESTIGATED, hypothesis: null },
        investigated: true,
        hypothesis: null,
        investigation_state: "unconfirmed",
      }),
    );
    render(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => 6.0}
        status={statusOf([])}
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );
    fireEvent.change(screen.getByLabelText("修正指示（自然言語）"), {
      target: { value: "ここ退屈" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));

    await waitFor(() => {
      expect(screen.getByTestId("review-draft-investigation")).toBeTruthy();
    });
    expect(screen.getByTestId("review-draft-investigation").textContent).toBe(
      "原因はまだ確認できていません",
    );
    // 禁止形（原因を調査しました＝完了）はどの状態でも表示しない。
    expect(screen.getByTestId("review-draft-investigation").textContent).not.toContain(
      "原因を調査しました",
    );
  });

  it("investigation_state が無い応答でも仮説の有無から仮説表示を導出する", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({
        received: true,
        sequence: 10,
        draft: DRAFT_INVESTIGATED,
        investigated: true,
        hypothesis: DRAFT_INVESTIGATED.hypothesis,
      }),
    );
    render(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => 6.0}
        status={statusOf([])}
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );
    fireEvent.change(screen.getByLabelText("修正指示（自然言語）"), {
      target: { value: "ここ退屈" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));

    await waitFor(() => {
      expect(screen.getByTestId("review-draft-investigation")).toBeTruthy();
    });
    expect(screen.getByTestId("review-draft-investigation").textContent).toContain(
      "AIの仮説",
    );
    expect(screen.getByTestId("review-draft-investigation").textContent).toContain(
      "同じ説明が続いているのが原因の可能性",
    );
  });
});
