// allow: SIZE_OK — 本ファイルは工程1以前から存在した 407 pure LOC のテスト群であり、
// 工程1の追加分（+350）は review-chat-investigation-display.test.tsx と
// review-revert-panel.test.tsx に責任単位で分割済み。工程2 rework では既存の
// alternatives複数案テスト2件を kind-aware 化したのみ（+3）。既存の古い試験部分まで
// 無関係に整理しない（reviewer の minimal-diff 指示）ためこれ以上の分割は行わない。
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
      "再build予約済み（起動待ち）",
    );
    expect(screen.getByTestId("rebuild-stage-hint").textContent).toContain("plan");
    expect(screen.getByTestId("rebuild-stage-hint").textContent).toContain("render");
    expect(screen.getByTestId("rebuild-indicator").textContent).toContain("rcmd-0123456789ab");

    // runner picks the rebuild up: a running stage row flips the phase.
    // W6: 行は現行runに属さないと今回活動の証明にならない — current_runと
    // run_id付きで出す（run無しの履歴running行拾いは旧誤動作）。
    episodeStatus = statusOf([
      { stage_name: "preview", status: "succeeded", retry_count: 0, last_error_code: null },
      { stage_name: "plan", status: "succeeded", retry_count: 0, last_error_code: null },
      {
        stage_name: "compile",
        status: "running",
        retry_count: 0,
        last_error_code: null,
        run_id: "run-2",
      },
    ]);
    (episodeStatus as { current_run?: string | null }).current_run = "run-2";
    view.rerender(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => 1.5}
        status={episodeStatus}
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );
    expect(screen.getByTestId("rebuild-phase").textContent).toBe("再build実行中");

    // rebuild finished: THIS-run rows (run-2) reach preview first-output
    // arrival and the preview probe is 2xx — 工程2P: 完了は今回runの成果
    // 確認（probe 2xx）後だけ（旧runの成功行では完了にしない）。
    episodeStatus = statusOf([
      { stage_name: "preview", status: "succeeded", retry_count: 0, last_error_code: null },
      {
        stage_name: "plan",
        status: "succeeded",
        retry_count: 0,
        last_error_code: null,
        run_id: "run-2",
      },
      {
        stage_name: "compile",
        status: "succeeded",
        retry_count: 0,
        last_error_code: null,
        run_id: "run-2",
        first_output_arrived_at: "2026-09-08T12:01:00+00:00",
      },
      {
        stage_name: "preview",
        status: "succeeded",
        retry_count: 0,
        last_error_code: null,
        run_id: "run-2",
        first_output_arrived_at: "2026-09-08T12:02:00+00:00",
      },
    ]);
    (episodeStatus as { current_run?: string | null }).current_run = "run-2";
    (episodeStatus as { preview_first_arrived_at?: string }).preview_first_arrived_at =
      "2026-09-08T12:02:00+00:00";
    view.rerender(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => 1.5}
        status={episodeStatus}
        previewOk={true}
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );
    expect(screen.getByTestId("rebuild-phase").textContent).toBe(
      "再build完了（試し編集の更新を確認済み）",
    );
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

  it("alternatives複数ドラフトは番号付きで表示され、まとめて適用のボタンは各案の採用ボタンに置き換わる", async () => {
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
    const fetchImpl = vi.fn(async () =>
      jsonResponse({
        received: true,
        sequence: 4,
        draft: DRAFT_REMOVE_0,
        drafts: [DRAFT_REMOVE_0, DRAFT_REMOVE_2],
        proposal_kind: "alternatives",
      }),
    );

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
    // alternatives（互いに排他的な案）では一括適用はしない: 各案の採用ボタンが一括ボタンを置き換える
    expect(screen.queryByTestId("review-apply-button")).toBeNull();
    expect(screen.queryByTestId("review-apply-all-button")).toBeNull();
    expect(screen.getByTestId("review-draft-adopt-1")).toBeTruthy();
    expect(screen.getByTestId("review-draft-adopt-2")).toBeTruthy();
    expect(screen.getByTestId("review-draft-adopt-1").textContent).toBe("この案を採用");
    // 案への反応として「両方違う」も出せる
    expect(screen.getByTestId("review-both-different").textContent).toBe("両方違う");
  });

  it("APIエラー以外の失敗は unexpected-client-error に丸めて表示する", async () => {
    // request() は fetch の throw を network-error に変換するため、
    // 本文読み込み自身の失敗がフォールバック経路の唯一の実経路。
    const fetchImpl: typeof fetch = async () =>
      new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.error(new Error("レスポンスの読み込みに失敗"));
          },
        }),
        { status: 200 },
      );

    render(
      <ReviewChatPanel
        episodeId="ep-abc"
        getAtSeconds={() => null}
        status={null}
        fetchImpl={fetchImpl}
      />,
    );

    fireEvent.change(screen.getByLabelText("修正指示（自然言語）"), {
      target: { value: "この後2秒残して" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));

    await waitFor(() => {
      expect(screen.getByTestId("error-notice")).toBeTruthy();
    });
    expect(screen.getByTestId("error-notice").textContent).toContain(
      "[unexpected-client-error]",
    );
    expect(screen.getByTestId("error-notice").textContent).toContain(
      "Error: レスポンスの読み込みに失敗",
    );
    expect(screen.queryByTestId("review-draft")).toBeNull();
  });

  it("alternatives複数ドラフトのうち曖昧な案だけ採用できず、明確な案は採用できる", async () => {
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
        proposal_kind: "alternatives",
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
    // 安全規律は案単位でも変わらない: 曖昧な案はその場で採用できない
    expect(
      (screen.getByTestId("review-draft-adopt-2") as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByTestId("review-draft-adopt-1") as HTMLButtonElement).disabled,
    ).toBe(false);
    expect(screen.getByTestId("review-draft-needs-confirmation-2").textContent).toContain(
      "どの修正にも当てはまりませんでした",
    );
  });
});
