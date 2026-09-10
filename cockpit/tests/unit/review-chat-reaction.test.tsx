// allow: SIZE_OK — 本ファイルは反応フロー（choice ack・両方違う・採用成功経路）という
// 1概念のテスト群で、工程2 rework の codex-required 採用成功経路（+71）を同概念として
// 追加したため 339 pure LOC。fixture を他ファイルへ分散させない判断（reviewer の
// minimal-diff 指示）によりこれ以上の分割は行わない。
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import ReviewChatPanel from "@/components/ReviewChatPanel";
import type { EpisodeStatus } from "@/lib/api";

// 工程2 reaction flow (U02-U05): choice / both-different against the
// previewed multi-draft set, per the backend contract in review_reactions.py.
const DRAFT_A = {
  schema_version: "cockpit-review-command-draft-v1",
  command_id: "rcmd-aaaaaaaaaaaa",
  command_kind: "remove_section",
  text: "冒頭のあいさつを削除して",
  target_seconds: 0,
  seconds_delta: null,
  scope: "episode",
  needs_confirmation: false,
  confirmation_reason: null,
};
const DRAFT_B = {
  ...DRAFT_A,
  command_id: "rcmd-bbbbbbbbbbbb",
  text: "冒頭を2秒残して",
  target_seconds: 2,
};
const DRAFT_ADJUSTED = {
  ...DRAFT_A,
  command_id: "rcmd-cccccccccccc",
  text: "もう少しゆっくりめに",
  target_seconds: 1,
};
const APPLIED = {
  schema_version: "cockpit-applied-command-v1",
  command_id: DRAFT_A.command_id,
  command_kind: "remove_section",
  affected_domain: "edit_plan",
  event_id: "e".repeat(64),
  base_plan_version: "v1",
  result_plan_version: "v2",
  deferred: false,
  reason: null,
  target_seconds: 0,
  seconds_delta: null,
};
const REBUILD_PLAN = {
  schema_version: "cockpit-rebuild-plan-v1",
  command_id: DRAFT_A.command_id,
  command_kind: "remove_section",
  affected_domain: "edit_plan",
  stages: ["plan", "compile", "preview", "resolve_build", "qc", "render"],
  excluded_stages: ["ingest", "normalize", "analyze", "selection", "publish"],
};

function statusOf(): EpisodeStatus {
  return {
    episode_id: "ep-abc",
    job_id: "ep-abc",
    status: "PREVIEW_READY",
    current_stage: "preview",
    created_at_seq: 1,
    updated_at_seq: 1,
    stage_runs: [],
  };
}

function jsonResponse(payload: object, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function renderPanel(fetchImpl: typeof fetch) {
  return render(
    <ReviewChatPanel
      episodeId="ep-abc"
      getAtSeconds={() => null}
      status={statusOf()}
      fetchImpl={fetchImpl}
    />,
  );
}

async function sendMultiDrafts() {
  fireEvent.change(screen.getByLabelText("気になるところを伝える"), {
    target: { value: DRAFT_A.text },
  });
  fireEvent.click(screen.getByTestId("review-chat-send"));
  await waitFor(() => {
    expect(screen.getByTestId("review-draft-1")).toBeTruthy();
  });
}

describe("ReviewChatPanel（工程2 反応フロー）", () => {
  it("各案の採用ボタンはその1案だけのsingle-draft echoを適用する", async () => {
    const applyBodies: object[] = [];
    const rebuildBodies: object[] = [];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/review-chat")) {
        return jsonResponse({
          received: true,
          sequence: 4,
          draft: DRAFT_A,
          drafts: [DRAFT_A, DRAFT_B],
          proposal_kind: "alternatives",
        });
      }
      if (url.endsWith("/review-chat/apply")) {
        applyBodies.push(JSON.parse(String(init?.body)));
        return jsonResponse({ applied: APPLIED, rebuild: REBUILD_PLAN });
      }
      if (url.endsWith("/rebuild")) {
        rebuildBodies.push(JSON.parse(String(init?.body)));
        return jsonResponse(
          { ...REBUILD_PLAN, stage_hint: REBUILD_PLAN.stages.join(","), scheduled: true, applied_command: DRAFT_A.command_id, runner_log: "/tmp/runner.log" },
          202,
        );
      }
      throw new Error(`unexpected url: ${url}`);
    });
    renderPanel(fetchImpl as unknown as typeof fetch);
    await sendMultiDrafts();

    fireEvent.click(screen.getByTestId("review-draft-adopt-1"));
    await waitFor(() => {
      expect(screen.getByTestId("rebuild-indicator")).toBeTruthy();
    });
    // single-draft echo（U04）: 保存setを名指すsequence + 選んだ1案だけ
    expect(applyBodies).toEqual([
      {
        text: DRAFT_A.text,
        at_seconds: null,
        drafts: [DRAFT_A],
        sequence: 4,
      },
    ]);
    expect(rebuildBodies).toEqual([{ applied_command: DRAFT_A.command_id }]);
    expect(screen.getByTestId("rebuild-indicator").textContent).toContain(
      DRAFT_A.command_id,
    );
  });

  it("「両方違う」はチャットメッセージとして送られ、紐付いた新しいsetに差し替わる", async () => {
    const chatBodies: object[] = [];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/review-chat")) {
        chatBodies.push(JSON.parse(String(init?.body)));
        if (chatBodies.length === 1) {
          return jsonResponse({
            received: true,
            sequence: 4,
            draft: DRAFT_A,
            drafts: [DRAFT_A, DRAFT_B],
            proposal_kind: "alternatives",
          });
        }
        return jsonResponse({
          received: true,
          sequence: 5,
          reaction: "both-different",
          responds_to_set: 4,
          draft: DRAFT_ADJUSTED,
          investigation_state: "materials-checked",
          checked_materials: { transcript: true, shot: false },
          proposal_kind: "alternatives",
        });
      }
      throw new Error(`unexpected url: ${url}`);
    });
    renderPanel(fetchImpl as unknown as typeof fetch);
    await sendMultiDrafts();

    fireEvent.click(screen.getByTestId("review-both-different"));
    await waitFor(() => {
      expect(screen.getByTestId("review-draft")).toBeTruthy();
    });
    expect(chatBodies[1]).toEqual({ text: "両方違う", at_seconds: null });
    // 新しいlinked setが通常の調査結果として描き替わる
    expect(screen.getByTestId("review-draft").textContent).toContain(
      DRAFT_ADJUSTED.command_id,
    );
    expect(screen.getByTestId("review-checked-materials").textContent).toBe(
      "確認できたもの: 周辺の字幕",
    );
    expect(screen.queryByTestId("review-choice-ack")).toBeNull();
    // 1案のsetに戻ったので一括適用ボタンが復活し、採用ボタンは消える
    expect(screen.getByTestId("review-apply-button").textContent).toBe("この位置を修正する");
    expect(screen.queryByTestId("review-draft-adopt-1")).toBeNull();
    expect(screen.queryByTestId("review-both-different")).toBeNull();
  });

  it("選択の返信は確認メモを出し、採用はまだ表示中の提案set（sequence 4）に向かう", async () => {
    const applyBodies: object[] = [];
    let chatCount = 0;
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/review-chat")) {
        chatCount += 1;
        if (chatCount === 1) {
          return jsonResponse({
            received: true,
            sequence: 4,
            draft: DRAFT_A,
            drafts: [DRAFT_A, DRAFT_B],
            proposal_kind: "alternatives",
          });
        }
        // choice response: NO draft(s) — the FACT only
        return jsonResponse({
          received: true,
          sequence: 5,
          reaction: "choice-a",
          in_response_to_set: 4,
          chosen_draft_index: 0,
          proposal_kind: "alternatives",
        });
      }
      if (url.endsWith("/review-chat/apply")) {
        applyBodies.push(JSON.parse(String(init?.body)));
        return jsonResponse({ applied: APPLIED, rebuild: REBUILD_PLAN });
      }
      if (url.endsWith("/rebuild")) {
        return jsonResponse(
          { ...REBUILD_PLAN, stage_hint: REBUILD_PLAN.stages.join(","), scheduled: true, applied_command: DRAFT_A.command_id, runner_log: "/tmp/runner.log" },
          202,
        );
      }
      throw new Error(`unexpected url: ${url}`);
    });
    renderPanel(fetchImpl as unknown as typeof fetch);
    await sendMultiDrafts();

    fireEvent.change(screen.getByLabelText("気になるところを伝える"), {
      target: { value: "Aがいい" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));

    await waitFor(() => {
      expect(screen.getByTestId("review-choice-ack")).toBeTruthy();
    });
    expect(screen.getByTestId("review-choice-ack").textContent).toBe(
      "選択を記録しました。採用する案のボタンを押してください",
    );
    // プレビューは消えない: 選ばれた案の採用ボタンがそのまま使える
    expect(screen.getByTestId("review-draft-2")).toBeTruthy();
    fireEvent.click(screen.getByTestId("review-draft-adopt-2"));
    await waitFor(() => {
      expect(applyBodies).toHaveLength(1);
    });
    // ack自身のsequence（5）ではなく、提案setのsequence（4）で適用する
    expect(applyBodies[0]).toEqual({
      text: DRAFT_B.text,
      at_seconds: null,
      drafts: [DRAFT_B],
      sequence: 4,
    });
  });

  it("choice 422（単一案への選択）は error-notice に表示される（実機発見の回帰）", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/review-chat")) {
        return jsonResponse(
          {
            error: {
              code: "choice-requires-multiple-drafts",
              detail: "a choice names one of several drafts; the latest proposal set has 1 draft(s)",
            },
          },
          422,
        );
      }
      throw new Error(`unexpected ${url}`);
    });
    render(
      <ReviewChatPanel
        episodeId="ep1"
        getAtSeconds={() => null}
        status={null}
        fetchImpl={fetchImpl as unknown as typeof fetch}
      />,
    );
    fireEvent.change(screen.getByTestId("review-chat-input"), {
      target: { value: "Bが好き" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));
    await waitFor(() => {
      expect(screen.getByTestId("error-notice").textContent).toContain(
        "choice-requires-multiple-drafts",
      );
    });
  });

  it("alternatives案Bの採用は成功し、Bだけのsingle-draft echo→rebuild readoutまで通る（reasonはどこにも乗らない）", async () => {
    const APPLIED_B = {
      ...APPLIED,
      command_id: DRAFT_B.command_id,
      event_id: "f".repeat(64),
    };
    const REBUILD_PLAN_B = { ...REBUILD_PLAN, command_id: DRAFT_B.command_id };
    const applyBodies: object[] = [];
    const rebuildBodies: object[] = [];
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/review-chat")) {
        return jsonResponse({
          received: true,
          sequence: 4,
          draft: DRAFT_A,
          drafts: [DRAFT_A, DRAFT_B],
          proposal_kind: "alternatives",
        });
      }
      if (url.endsWith("/review-chat/apply")) {
        applyBodies.push(JSON.parse(String(init?.body)));
        return jsonResponse({
          applied: APPLIED_B,
          rebuild: REBUILD_PLAN_B,
          applied_commands: [APPLIED_B],
        });
      }
      if (url.endsWith("/rebuild")) {
        rebuildBodies.push(JSON.parse(String(init?.body)));
        return jsonResponse(
          {
            ...REBUILD_PLAN_B,
            stage_hint: REBUILD_PLAN_B.stages.join(","),
            scheduled: true,
            applied_command: DRAFT_B.command_id,
            runner_log: "/tmp/runner.log",
          },
          202,
        );
      }
      throw new Error(`unexpected url: ${url}`);
    });
    renderPanel(fetchImpl as unknown as typeof fetch);
    await sendMultiDrafts();

    fireEvent.click(screen.getByTestId("review-draft-adopt-2"));
    await waitFor(() => {
      expect(screen.getByTestId("rebuild-indicator")).toBeTruthy();
    });
    // single-draft echo: Bだけ＋保存set（sequence 4）。reasonフィールドはどこにも乗らない（U04）。
    expect(applyBodies).toEqual([
      {
        text: DRAFT_B.text,
        at_seconds: null,
        drafts: [DRAFT_B],
        sequence: 4,
      },
    ]);
    expect(Object.keys(applyBodies[0] as object)).not.toContain("reason");
    expect(rebuildBodies).toEqual([{ applied_command: DRAFT_B.command_id }]);
    expect(screen.getByTestId("rebuild-indicator").textContent).toContain(
      DRAFT_B.command_id,
    );
    expect(screen.getByTestId("rebuild-phase").textContent).toBe(
      "再build予約済み（起動待ち）",
    );
  });
});
