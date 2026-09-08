import { describe, expect, it } from "vitest";
import { deriveRebuildPhase, rebuildPhaseText } from "@/components/useRebuildPhase";
import type { EpisodeStatus, RebuildResult } from "@/lib/api";

/**
 * 工程2P useRebuildPhaseの純粋な状態遷移試験（失敗先行）。run対応が本体:
 * phase は stage行のうち run_id === current_run の行と rebuild連鎖記録の
 * 「み」から導出し、旧runの成功・旧runの実行中残留で完了/実行中固定に
 * ならないこと（U30: codex失敗先行条件）を、実データ（旧run併存+新予約）
 * を模したfixtureで固定する。ベースライン件数比較は撤去済み。
 */

function iso(secondsAgo: number): string {
  return new Date(Date.now() - secondsAgo * 1000).toISOString();
}

function baseStatus(overrides: Partial<EpisodeStatus>): EpisodeStatus {
  return {
    episode_id: "ep-run01",
    job_id: "ep-run01",
    status: "PLAN_COMMITTED",
    current_stage: "compile",
    created_at_seq: 1,
    updated_at_seq: 40,
    stage_runs: [],
    current_run: null,
    current_target_version: null,
    pending_rebuild: null,
    rebuild_requests: [],
    last_worker_report_at: null,
    last_worker_report_event: null,
    unreviewed_proposal_set: false,
    ...overrides,
  };
}

const SCHEDULED: RebuildResult = {
  stage_hint: "compile,preview,resolve_build,qc,render",
  scheduled: true,
  applied_command: "rcmd-0123456789ab",
};

const CHAIN_ENTRY = {
  schema_version: "cockpit-rebuild-request-v1",
  stage_hint: "compile,preview",
  marker: null,
  reserves_sequence: null,
};

/** 実データ模倣: 旧run-17aのcompile成功とpreview実行中が残り、新rebuildは
 *  予約済み（未起動）—— backend derive_current_run は current_run=null を
 *  返し、pending_rebuild に予約が載る（U30 fixture）。 */
const U30_OLD_RUN_RESIDUE = baseStatus({
  current_run: null,
  current_target_version: "v3",
  pending_rebuild: {
    sequence: 18,
    stage_hint: "compile,preview",
    marker: "revert:v2",
    run_id: null,
    target_version: "v3",
  },
  rebuild_requests: [
    {
      ...CHAIN_ENTRY,
      sequence: 16,
      spawned: true,
      run_id: "run-17a",
      target_version: "v2",
    },
    {
      ...CHAIN_ENTRY,
      sequence: 18,
      marker: "revert:v2",
      spawned: false,
      run_id: null,
      target_version: "v3",
    },
  ],
  stage_runs: [
    {
      stage_name: "compile",
      status: "succeeded",
      retry_count: 0,
      last_error_code: null,
      run_id: "run-17a",
      first_started_at: iso(600),
      last_transition_at: iso(500),
      first_output_arrived_at: iso(490),
    },
    {
      stage_name: "preview",
      status: "running",
      retry_count: 0,
      last_error_code: null,
      run_id: "run-17a",
      first_started_at: iso(480),
      last_transition_at: iso(470),
    },
  ],
});

describe("useRebuildPhase — run対応の状態遷移（U30失敗先行fixture）", () => {
  it("旧run成功+旧run実行中残留+新予約 → 完了でも実行中固定でもなく予約済み", () => {
    expect(deriveRebuildPhase(SCHEDULED, U30_OLD_RUN_RESIDUE, true)).toBe("scheduled");
    const text = rebuildPhaseText(deriveRebuildPhase(SCHEDULED, U30_OLD_RUN_RESIDUE, true));
    expect(text).toContain("再build予約済み");
    expect(text).not.toContain("完了");
    expect(text).not.toContain("実行中");
  });

  it("予約→起動: current_runの行がrunningなら実行中（旧runの行では出ない）", () => {
    const spawned = baseStatus({
      current_run: "run-18b",
      pending_rebuild: null,
      rebuild_requests: [
        {
          ...CHAIN_ENTRY,
          sequence: 18,
          spawned: true,
          run_id: "run-18b",
          target_version: "v3",
        },
      ],
      stage_runs: [
        // 旧runの実行中残留 — current_run ではないので無視される
        {
          stage_name: "preview",
          status: "running",
          retry_count: 0,
          last_error_code: null,
          run_id: "run-17a",
        },
        {
          stage_name: "compile",
          status: "running",
          retry_count: 0,
          last_error_code: null,
          run_id: "run-18b",
        },
      ],
    });
    expect(deriveRebuildPhase(SCHEDULED, spawned, null)).toBe("running");
  });

  it("今回runのpreview初回到達で成果確認待ち（probe前）——全体完了とは言わない", () => {
    const arrived = baseStatus({
      current_run: "run-18b",
      preview_first_arrived_at: iso(30),
      stage_runs: [
        {
          stage_name: "compile",
          status: "succeeded",
          retry_count: 0,
          last_error_code: null,
          run_id: "run-18b",
          first_output_arrived_at: iso(60),
        },
        {
          stage_name: "preview",
          status: "succeeded",
          retry_count: 0,
          last_error_code: null,
          run_id: "run-18b",
          first_output_arrived_at: iso(30),
        },
      ],
    });
    expect(deriveRebuildPhase(SCHEDULED, arrived, null)).toBe("awaiting_confirmation");
    const text = rebuildPhaseText("awaiting_confirmation");
    expect(text).toContain("試し編集完了・確認してください");
    expect(text).not.toMatch(/完了$/);
  });

  it("今回runの到達+preview probe 2xxでのみ完了", () => {
    const arrived = baseStatus({
      current_run: "run-18b",
      preview_first_arrived_at: iso(30),
    });
    expect(deriveRebuildPhase(SCHEDULED, arrived, true)).toBe("done");
    // probe結果が2xxでない間は完了にしない
    expect(deriveRebuildPhase(SCHEDULED, arrived, false)).toBe("awaiting_confirmation");
  });

  it("ベースライン件数比較は撤去: 今回runの到達なしに旧成功行だけでは完了しない", () => {
    const legacySuccess = baseStatus({
      current_run: "run-18b",
      stage_runs: [
        {
          stage_name: "preview",
          status: "succeeded",
          retry_count: 0,
          last_error_code: null,
          run_id: "run-18b",
        },
      ],
    });
    expect(deriveRebuildPhase(SCHEDULED, legacySuccess, true)).not.toBe("done");
  });

  it("今回runのfailed_blocked行は停止表示（実行中と偽らない）", () => {
    const blocked = baseStatus({
      current_run: "run-18b",
      stage_runs: [
        {
          stage_name: "compile",
          status: "failed_blocked",
          retry_count: 1,
          last_error_code: "compile-failed",
          run_id: "run-18b",
        },
      ],
    });
    expect(deriveRebuildPhase(SCHEDULED, blocked, null)).toBe("failed");
    expect(rebuildPhaseText("failed")).toContain("止まっています");
  });

  it("scheduled=falseは記録どまり（表示なし）、rebuild結果なしは常にnull", () => {
    expect(
      deriveRebuildPhase({ stage_hint: null, scheduled: false }, baseStatus({}), null),
    ).toBe("recorded");
    expect(rebuildPhaseText("recorded")).toBeNull();
    expect(deriveRebuildPhase(null, U30_OLD_RUN_RESIDUE, true)).toBeNull();
  });
});
