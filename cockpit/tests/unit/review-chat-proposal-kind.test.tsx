import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import ReviewChatPanel from "@/components/ReviewChatPanel";
import type { EpisodeStatus } from "@/lib/api";

// 工程2 rework: the chat response's proposal_kind routes HOW a multi-draft
// set is adopted (backend ProposalKind + the _require_kind_route guards in
// review_proposals.py). One test per matrix cell:
//   bundle multi      → apply-ALL (full-set echo), NO adopt, NO 両方違う
//   alternatives multi → per-draft 採用 + 両方違う, NO apply-all
//   kind absent (old payload) → bundle
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
  text: "字幕をもっと短くして",
  target_seconds: 2,
};
const DRAFT_AMBIGUOUS = {
  ...DRAFT_A,
  command_id: "rcmd-ffffffffffff",
  command_kind: null,
  text: "ありがとうございます",
  needs_confirmation: true,
  confirmation_reason: "no known command kind matched the message; restate the correction",
  target_seconds: null,
  seconds_delta: null,
};
const APPLIED_A = {
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
const APPLIED_B = {
  ...APPLIED_A,
  command_id: DRAFT_B.command_id,
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

async function sendMultiDrafts(drafts: object[], proposalKind?: string) {
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/review-chat")) {
      return jsonResponse({
        received: true,
        sequence: 4,
        draft: drafts[0],
        drafts,
        ...(proposalKind !== undefined ? { proposal_kind: proposalKind } : {}),
      });
    }
    if (url.endsWith("/review-chat/apply")) {
      return jsonResponse({ applied: APPLIED_A, rebuild: REBUILD_PLAN });
    }
    if (url.endsWith("/rebuild")) {
      return jsonResponse(
        {
          ...REBUILD_PLAN,
          stage_hint: REBUILD_PLAN.stages.join(","),
          scheduled: true,
          applied_command: DRAFT_A.command_id,
          runner_log: "/tmp/runner.log",
        },
        202,
      );
    }
    throw new Error(`unexpected url: ${url}`);
  });
  renderPanel(fetchImpl as unknown as typeof fetch);
  fireEvent.change(screen.getByLabelText("気になるところを伝える"), {
    target: { value: DRAFT_A.text },
  });
  fireEvent.click(screen.getByTestId("review-chat-send"));
  await waitFor(() => {
    expect(screen.getByTestId("review-draft-1")).toBeTruthy();
  });
  return fetchImpl;
}

describe("ReviewChatPanel（proposal_kind別の採用経路）", () => {
  it("bundle複数案は一括適用ボタンになり、全案＋sequenceのfull-set echoを送る", async () => {
    const fetchImpl = await sendMultiDrafts([DRAFT_A, DRAFT_B], "command-bundle");
    const calls = fetchImpl.mock.calls as Array<[RequestInfo | URL, RequestInit?]>;
    calls.length = 0;

    expect(screen.getByTestId("review-apply-all-button").textContent).toBe(
      "この位置を修正する（すべて）",
    );
    expect(screen.queryByTestId("review-draft-adopt-1")).toBeNull();
    expect(screen.queryByTestId("review-draft-adopt-2")).toBeNull();
    expect(screen.queryByTestId("review-both-different")).toBeNull();
    // bundleは選択ではない: 採用・両方違うの入口は存在しない

    fireEvent.click(screen.getByTestId("review-apply-all-button"));
    await waitFor(() => {
      expect(calls.some(([url]) => String(url).endsWith("/review-chat/apply"))).toBe(
        true,
      );
    });
    const [, init] = calls.find(([url]) => String(url).endsWith("/review-chat/apply"))!;
    // full-set echo（bundle-requires-full-apply の正経路）: 全案＋保存setを名指すsequence
    expect(JSON.parse(String(init?.body))).toEqual({
      text: DRAFT_A.text,
      at_seconds: null,
      drafts: [DRAFT_A, DRAFT_B],
      sequence: 4,
    });
    await waitFor(() => {
      expect(screen.getByTestId("rebuild-indicator")).toBeTruthy();
    });
  });

  it("alternatives複数案は各案の採用ボタン＋両方違うになり、一括適用ボタンは出ない", async () => {
    await sendMultiDrafts([DRAFT_A, DRAFT_B], "alternatives");
    expect(screen.queryByTestId("review-apply-all-button")).toBeNull();
    expect(screen.getByTestId("review-draft-adopt-1").textContent).toBe("この案を採用");
    expect(screen.getByTestId("review-draft-adopt-2").textContent).toBe("この案を採用");
    expect(screen.getByTestId("review-both-different").textContent).toBe("両方違う");
  });

  it("proposal_kindが無い旧payloadの複数案はbundleとして扱う（一括適用）", async () => {
    await sendMultiDrafts([DRAFT_A, DRAFT_B]);
    expect(screen.getByTestId("review-apply-all-button").textContent).toBe(
      "この位置を修正する（すべて）",
    );
    expect(screen.queryByTestId("review-draft-adopt-1")).toBeNull();
    expect(screen.queryByTestId("review-both-different")).toBeNull();
  });

  it("bundle一括適用も曖昧な案が1つでもあると無効になる", async () => {
    await sendMultiDrafts([DRAFT_A, DRAFT_AMBIGUOUS], "command-bundle");
    expect(
      (screen.getByTestId("review-apply-all-button") as HTMLButtonElement).disabled,
    ).toBe(true);
  });
});
