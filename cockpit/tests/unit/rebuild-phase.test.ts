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
      rebuild_requests: [
        {
          ...CHAIN_ENTRY,
          sequence: 18,
          spawned: true,
          run_id: "run-18b",
          target_version: "v3",
        },
      ],
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

  it("scheduled=falseは記録どまり（表示なし）", () => {
    expect(
      deriveRebuildPhase({ stage_hint: null, scheduled: false }, baseStatus({}), null),
    ).toBe("recorded");
    expect(rebuildPhaseText("recorded")).toBeNull();
  });
});

describe("useRebuildPhase — 再読込復元（U30 hydration / codex独立レビューP1-1）", () => {
  it("null読み出しでも予約中のserver連鎖があれば予約済みを復元する", () => {
    expect(deriveRebuildPhase(null, U30_OLD_RUN_RESIDUE, null)).toBe("scheduled");
  });

  it("null読み出しでも今回runの失敗行があれば停止を復元する", () => {
    const blocked = baseStatus({
      current_run: "run-18b",
      stage_runs: [
        {
          stage_name: "preview",
          status: "failed_blocked",
          retry_count: 0,
          last_error_code: "preview-failed",
          run_id: "run-18b",
        },
      ],
    });
    expect(deriveRebuildPhase(null, blocked, null)).toBe("failed");
  });

  it("server連鎖の無いnull読み出しは表示しない（無関係episodeで線を出さない）", () => {
    expect(deriveRebuildPhase(null, baseStatus({}), null)).toBeNull();
  });
});

describe("useRebuildPhase — stale-poll honesty（V44-1 live lane）", () => {
  /** Live flash replica: POST /rebuild accepted (client result set) but the
   *  2s poll still serves the PRE-rebuild server state — old current_run,
   *  old preview arrival, old bound probe (previewOk true). */
  const STALE_PRE_REBUILD = baseStatus({
    current_run: "run-A",
    current_target_version: "v1",
    preview_first_arrived_at: iso(300),
    rebuild_requests: [],
    stage_runs: [
      {
        stage_name: "compile",
        status: "succeeded",
        retry_count: 0,
        last_error_code: null,
        run_id: "run-A",
        first_output_arrived_at: iso(320),
      },
      {
        stage_name: "preview",
        status: "succeeded",
        retry_count: 0,
        last_error_code: null,
        run_id: "run-A",
        first_output_arrived_at: iso(300),
      },
    ],
  });

  function spawnedEntry(sequence: number, runId: string): {
    schema_version: string;
    stage_hint: string;
    marker: null;
    reserves_sequence: null;
    sequence: number;
    spawned: boolean;
    run_id: string;
    target_version: string;
  } {
    return {
      ...CHAIN_ENTRY,
      sequence,
      spawned: true,
      run_id: runId,
      target_version: "v2",
    };
  }

  function completedRunStatus(runId: string, sequences: number[]): EpisodeStatus {
    return baseStatus({
      current_run: runId,
      current_target_version: "v2",
      preview_first_arrived_at: iso(30),
      rebuild_requests: sequences.map((sequence) => spawnedEntry(sequence, runId)),
      stage_runs: [
        {
          stage_name: "compile",
          status: "succeeded",
          retry_count: 0,
          last_error_code: null,
          run_id: runId,
          first_output_arrived_at: iso(60),
        },
        {
          stage_name: "preview",
          status: "succeeded",
          retry_count: 0,
          last_error_code: null,
          run_id: runId,
          first_output_arrived_at: iso(30),
        },
      ],
    });
  }

  it("受付直後の旧poll（旧run到達+旧probe 2xx）は完了にしない——受付済みどまり", () => {
    const baseline = { currentRun: "run-A", latestSequence: null };
    expect(deriveRebuildPhase(SCHEDULED, STALE_PRE_REBUILD, true, baseline)).toBe(
      "scheduled",
    );
    const text = rebuildPhaseText(
      deriveRebuildPhase(SCHEDULED, STALE_PRE_REBUILD, true, baseline),
    );
    expect(text).toContain("再build予約済み");
    expect(text).not.toContain("完了");
  });

  it("baseline無しでも連鎖証拠の無い旧到達だけでは完了にしない", () => {
    expect(deriveRebuildPhase(SCHEDULED, STALE_PRE_REBUILD, true)).toBe("scheduled");
    expect(deriveRebuildPhase(SCHEDULED, STALE_PRE_REBUILD, true)).not.toBe("done");
  });

  it("serverがTHIS runを示したら完了する（新run+到達+probe 2xx）", () => {
    const baseline = { currentRun: "run-A", latestSequence: null };
    const done = completedRunStatus("run-B", [1]);
    expect(deriveRebuildPhase(SCHEDULED, done, true, baseline)).toBe("done");
    expect(deriveRebuildPhase(SCHEDULED, done, false, baseline)).toBe(
      "awaiting_confirmation",
    );
  });

  it("cross-run pin: 旧runの成功到達はNEW requestを完了にしない", () => {
    // 2回目のrebuild受付直後: serverはまだ前回run-Bの完了状態を示す。
    const baseline = { currentRun: "run-B", latestSequence: 0 };
    const stale = completedRunStatus("run-B", [0]);
    expect(deriveRebuildPhase(SCHEDULED, stale, true, baseline)).toBe("scheduled");
    expect(deriveRebuildPhase(SCHEDULED, stale, true, baseline)).not.toBe("done");
  });

  it("連鎖が受付後に伸びたらTHIS runの証拠になる（同run名でも完了できる）", () => {
    const baseline = { currentRun: "run-new", latestSequence: null };
    const done = completedRunStatus("run-new", [0]);
    expect(deriveRebuildPhase(SCHEDULED, done, true, baseline)).toBe("done");
  });

  it("W6 codex再現: 旧runのfailed_blockedのみ+世代が受付時のまま→今回の失敗と言わず予約済み", () => {
    const oldFailedOnly = baseStatus({
      current_run: "run-old",
      current_target_version: "v1",
      rebuild_requests: [],
      stage_runs: [
        {
          stage_name: "preview",
          status: "failed_blocked",
          retry_count: 1,
          last_error_code: "preview-failed",
          run_id: "run-old",
        },
      ],
    });
    const baseline = { currentRun: "run-old", latestSequence: null };
    expect(deriveRebuildPhase(SCHEDULED, oldFailedOnly, null, baseline)).toBe(
      "scheduled",
    );
    expect(deriveRebuildPhase(SCHEDULED, oldFailedOnly, null, baseline)).not.toBe(
      "failed",
    );
  });

  it("W6: 旧runのrunning残留+新予約の世代未反映→実行中と偽らず予約済み（baseline有り）", () => {
    const oldRunning = baseStatus({
      current_run: "run-old",
      rebuild_requests: [],
      stage_runs: [
        {
          stage_name: "preview",
          status: "running",
          retry_count: 0,
          last_error_code: null,
          run_id: "run-old",
        },
      ],
    });
    const baseline = { currentRun: "run-old", latestSequence: null };
    expect(deriveRebuildPhase(SCHEDULED, oldRunning, null, baseline)).toBe(
      "scheduled",
    );
  });

  it("W6: 現行runなしで履歴running行を拾わない（今回活動の証明ではない）", () => {
    const noCurrentRun = baseStatus({
      current_run: null,
      pending_rebuild: null,
      rebuild_requests: [],
      stage_runs: [
        {
          stage_name: "preview",
          status: "running",
          retry_count: 0,
          last_error_code: null,
          run_id: "run-old",
        },
      ],
    });
    expect(deriveRebuildPhase(SCHEDULED, noCurrentRun, null)).toBe("scheduled");
    expect(deriveRebuildPhase(SCHEDULED, noCurrentRun, null)).not.toBe("running");
  });

  it("W6: serverがTHIS runの失敗を示したら停止を出す（新run+failed_blocked+連鎖証拠）", () => {
    const baseline = { currentRun: "run-old", latestSequence: null };
    const thisRunFailed = baseStatus({
      current_run: "run-new",
      rebuild_requests: [spawnedEntry(1, "run-new")],
      stage_runs: [
        {
          stage_name: "preview",
          status: "failed_blocked",
          retry_count: 0,
          last_error_code: "preview-failed",
          run_id: "run-new",
        },
      ],
    });
    expect(deriveRebuildPhase(SCHEDULED, thisRunFailed, null, baseline)).toBe(
      "failed",
    );
  });
});
