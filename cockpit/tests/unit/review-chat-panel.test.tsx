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

const APPLIED_BGM = {
  ...APPLIED,
  command_id: "rcmd-bgm000000001",
  command_kind: "lower_bgm",
  affected_domain: "presentation",
  event_id: null,
  result_plan_version: null,
};

const REBUILD_PLAN = {
  schema_version: "cockpit-rebuild-plan-v1",
  command_id: "rcmd-0123456789ab",
  command_kind: "keep_longer",
  affected_domain: "edit_plan",
  stages: ["plan", "compile", "preview", "resolve_build", "qc", "render"],
  excluded_stages: ["ingest", "normalize", "analyze", "selection", "publish"],
};

const REBUILD_SCHEDULED = {
  stage_hint: "plan,compile,preview,resolve_build,qc,render",
  scheduled: true,
  applied_command: "rcmd-0123456789ab",
  stages: REBUILD_PLAN.stages,
  runner_log: "/tmp/episodes/ep-abc/runner.log",
};

const REBUILD_NOT_EXECUTABLE = {
  stage_hint: "compile,preview,resolve_build,qc,render",
  scheduled: false,
  reason: "command kind not rebuild-executable yet",
  applied_command: "rcmd-bgm000000001",
  rebuild_stages: ["compile", "preview", "resolve_build", "qc", "render"],
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

describe("ReviewChatPanel（NL修正→構造化プレビュー→部分rebuild実行）", () => {
  it("適用すると再build予約済み→実行中→完了が job status の poll から derive される", async () => {
    let episodeStatus = statusOf([
      { stage_name: "preview", status: "succeeded", retry_count: 0, last_error_code: null },
    ]);
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/review-chat")) {
        return jsonResponse({ received: true, sequence: 1, draft: DRAFT_KEEP_LONGER });
      }
      if (url.endsWith("/review-chat/apply")) {
        return jsonResponse({ applied: APPLIED, rebuild: REBUILD_PLAN });
      }
      if (url.endsWith("/rebuild")) {
        return jsonResponse(REBUILD_SCHEDULED, 202);
      }
      throw new Error(`unexpected url: ${url}`);
    });

    const view = render(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => 1.5}
        status={episodeStatus}
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );

    fireEvent.change(screen.getByLabelText("修正指示（自然言語）"), {
      target: { value: "この後2秒残して" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));
    await waitFor(() => {
      expect(screen.getByTestId("review-draft")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("review-apply-button"));

    await waitFor(() => {
      expect(screen.getByTestId("rebuild-indicator")).toBeTruthy();
    });
    expect(screen.getByTestId("rebuild-phase").textContent).toBe(
      "再build予約済み（runner起動待ち・進捗は自動更新）",
    );
    expect(screen.getByTestId("rebuild-stage-hint").textContent).toContain("plan");
    expect(screen.getByTestId("rebuild-stage-hint").textContent).toContain("render");
    expect(screen.getByTestId("rebuild-indicator").textContent).toContain("rcmd-0123456789ab");

    // runner picks the rebuild up: a running stage row flips the phase
    episodeStatus = statusOf([
      { stage_name: "preview", status: "succeeded", retry_count: 0, last_error_code: null },
      { stage_name: "plan", status: "succeeded", retry_count: 0, last_error_code: null },
      { stage_name: "compile", status: "running", retry_count: 0, last_error_code: null },
    ]);
    view.rerender(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => 1.5}
        status={episodeStatus}
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );
    expect(screen.getByTestId("rebuild-phase").textContent).toBe("再build実行中");

    // rebuild finished: compile row succeeded (rebuild-only) and nothing running
    episodeStatus = statusOf([
      { stage_name: "preview", status: "succeeded", retry_count: 0, last_error_code: null },
      { stage_name: "plan", status: "succeeded", retry_count: 0, last_error_code: null },
      { stage_name: "compile", status: "succeeded", retry_count: 0, last_error_code: null },
      { stage_name: "preview", status: "succeeded", retry_count: 0, last_error_code: null },
    ]);
    view.rerender(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => 1.5}
        status={episodeStatus}
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );
    expect(screen.getByTestId("rebuild-phase").textContent).toBe("再build完了");
  });

  it("実行可能な種以外は理由付きで記録どまりになる", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/review-chat")) {
        return jsonResponse({ received: true, sequence: 1, draft: DRAFT_KEEP_LONGER });
      }
      if (url.endsWith("/review-chat/apply")) {
        return jsonResponse({ applied: APPLIED_BGM, rebuild: REBUILD_PLAN });
      }
      if (url.endsWith("/rebuild")) {
        return jsonResponse(REBUILD_NOT_EXECUTABLE, 202);
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

    fireEvent.change(screen.getByLabelText("修正指示（自然言語）"), {
      target: { value: "この後2秒残して" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));
    await waitFor(() => {
      expect(screen.getByTestId("review-draft")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("review-apply-button"));

    await waitFor(() => {
      expect(screen.getByTestId("rebuild-indicator")).toBeTruthy();
    });
    expect(screen.getByTestId("rebuild-phase").textContent).toBe(
      "再build未実行（コマンドは記録済み）",
    );
    expect(screen.getByTestId("rebuild-reason").textContent).toBe(
      "command kind not rebuild-executable yet",
    );
  });

  it("曖昧な入力は確認理由付きで出るだけで適用できない", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ received: true, sequence: 2, draft: DRAFT_AMBIGUOUS }),
    );
    render(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => null}
        status={null}
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

  it("確認理由は日本語に置き換わる", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ received: true, sequence: 3, draft: DRAFT_AMBIGUOUS }),
    );
    render(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => null}
        status={null}
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
    expect(screen.getByTestId("review-draft-needs-confirmation").textContent).toContain(
      "どの修正にも当てはまりませんでした",
    );
  });

  it("複数ドラフトは番号付きで表示され、1回の適用で全部まとめて適用される", async () => {
    const DRAFT_REMOVE_0 = {
      ...DRAFT_KEEP_LONGER,
      command_id: "rcmd-remove0000001",
      command_kind: "remove_section",
      text: "0:00と0:02の「はじめまーす」「テスト動画だよ」を削除して",
      target_seconds: 0,
      seconds_delta: null,
    };
    const DRAFT_REMOVE_2 = {
      ...DRAFT_REMOVE_0,
      command_id: "rcmd-remove0000002",
      target_seconds: 2,
    };
    const APPLIED_1 = { ...APPLIED, command_id: DRAFT_REMOVE_0.command_id, target_seconds: 0, seconds_delta: null };
    const APPLIED_2 = { ...APPLIED, command_id: DRAFT_REMOVE_2.command_id, target_seconds: 2, seconds_delta: null };
    const applyBodies: object[] = [];
    const rebuildBodies: object[] = [];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/review-chat")) {
        return jsonResponse({
          received: true,
          sequence: 4,
          draft: DRAFT_REMOVE_0,
          drafts: [DRAFT_REMOVE_0, DRAFT_REMOVE_2],
        });
      }
      if (url.endsWith("/review-chat/apply")) {
        applyBodies.push(JSON.parse(String(init?.body)));
        return jsonResponse({
          applied: APPLIED_1,
          applied_commands: [APPLIED_1, APPLIED_2],
          rebuild: REBUILD_PLAN,
        });
      }
      if (url.endsWith("/rebuild")) {
        rebuildBodies.push(JSON.parse(String(init?.body)));
        return jsonResponse(
          { ...REBUILD_SCHEDULED, applied_command: APPLIED_1.command_id, applied_commands: [APPLIED_1.command_id, APPLIED_2.command_id] },
          202,
        );
      }
      throw new Error(`unexpected url: ${url}`);
    });

    render(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => null}
        status={statusOf([])}
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );
    fireEvent.change(screen.getByLabelText("修正指示（自然言語）"), {
      target: { value: DRAFT_REMOVE_0.text },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));

    await waitFor(() => {
      expect(screen.getByTestId("review-draft-1")).toBeTruthy();
    });
    expect(screen.getByTestId("review-draft-2").textContent).toContain("区間を削除");
    expect(screen.getByTestId("review-draft-2").textContent).toContain("2s");
    expect((screen.getByTestId("review-apply-button") as HTMLButtonElement).disabled).toBe(
      false,
    );
    expect(screen.getByTestId("review-apply-button").textContent).toBe(
      "この修正をすべて適用",
    );

    fireEvent.click(screen.getByTestId("review-apply-button"));
    await waitFor(() => {
      expect(screen.getByTestId("rebuild-indicator")).toBeTruthy();
    });
    expect(applyBodies).toEqual([
      {
        text: DRAFT_REMOVE_0.text,
        at_seconds: null,
        drafts: [DRAFT_REMOVE_0, DRAFT_REMOVE_2],
      },
    ]);
    expect(rebuildBodies).toEqual([
      {
        applied_command: APPLIED_1.command_id,
        applied_commands: [APPLIED_1.command_id, APPLIED_2.command_id],
      },
    ]);
    expect(screen.getByTestId("rebuild-indicator").textContent).toContain(
      "rcmd-remove0000001、rcmd-remove0000002",
    );
  });

  it("複数ドラフトのうち1つでも曖昧なら適用できない", async () => {
    const clear = {
      ...DRAFT_KEEP_LONGER,
      command_id: "rcmd-clear00000001",
      command_kind: "remove_section",
      target_seconds: 3,
      seconds_delta: null,
    };
    const ambiguous = { ...DRAFT_AMBIGUOUS, command_id: "rcmd-ambig00000001" };
    const fetchImpl = vi.fn(async () =>
      jsonResponse({
        received: true,
        sequence: 5,
        draft: clear,
        drafts: [clear, ambiguous],
      }),
    );
    render(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => null}
        status={null}
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );
    fireEvent.change(screen.getByLabelText("修正指示（自然言語）"), {
      target: { value: "これは曖昧" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));
    await waitFor(() => {
      expect(screen.getByTestId("review-draft-2")).toBeTruthy();
    });
    expect(
      (screen.getByTestId("review-apply-button") as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(screen.getByTestId("review-draft-needs-confirmation-2").textContent).toContain(
      "どの修正にも当てはまりませんでした",
    );
  });
});
