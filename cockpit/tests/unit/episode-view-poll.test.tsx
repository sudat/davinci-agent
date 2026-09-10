import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import EpisodeView from "@/components/EpisodeView";

/** 工程2P poll hardening（step2p-design.md §A/§F + codex条件6）:
 *  - 状態取得時刻 fetchedAt を成功毎に記録し、15秒途切れで専用banner
 *  - 失敗でも poll を続ける
 *  - visibilitychange/focus で即時再照会
 *  - 補助fetch（flags/preview）が poll 予定を遅らせない */

function jsonResponse(url: string, status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", url },
  });
}

function okStatus(seq: number): Response {
  return jsonResponse("s", 200, {
    episode_id: "ep-poll01",
    job_id: "ep-poll01",
    status: "CREATED",
    current_stage: "intake",
    created_at_seq: 1,
    updated_at_seq: seq,
    stage_runs: [],
    current_run: null,
    current_target_version: null,
    pending_rebuild: null,
    rebuild_requests: [],
    last_worker_report_at: null,
    last_worker_report_event: null,
    unreviewed_proposal_set: false,
  });
}

const flagsOk = () =>
  jsonResponse("f", 200, { flags: [], not_yet_generated: true });
const preview404 = () => new Response(null, { status: 404 });
const consultationOk = () =>
  jsonResponse("c", 200, { consultations: [] });

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("EpisodeView poll hardening（工程2P）", () => {
  it("補助fetch（flags/preview）が停止しても status poll は止まらない", async () => {
    let statusCalls = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const url = String(input);
      if (url.endsWith("/flags")) return new Promise(() => undefined);
      if (url.endsWith("/preview")) return new Promise(() => undefined);
      if (url.endsWith("/outputs")) return Promise.resolve(new Response(null, { status: 404 }));
      if (url.endsWith("/self-check")) {
        return Promise.resolve(
          jsonResponse("sc", 200, { episode_id: "ep-poll01", self_check: null }),
        );
      }
      if (url.includes("/finishing")) return Promise.resolve(okStatus(0));
      if (url.includes("/consultation")) {
        return Promise.resolve(jsonResponse("c", 200, { consultations: [] }));
      }
      statusCalls += 1;
      return Promise.resolve(okStatus(statusCalls));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<EpisodeView episodeId="ep-poll01" />);
    await vi.advanceTimersByTimeAsync(0);
    expect(statusCalls).toBe(1);

    await vi.advanceTimersByTimeAsync(2000);
    await vi.advanceTimersByTimeAsync(2000);
    expect(statusCalls).toBeGreaterThanOrEqual(3);
  });

  it("15秒更新途切れで専用banner（error noticeとは別、復帰で消える）。中止は未実装と明示", async () => {
    let failing = false;
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const url = String(input);
      if (url.endsWith("/flags")) return Promise.resolve(flagsOk());
      if (url.endsWith("/preview")) return Promise.resolve(preview404());
      if (url.endsWith("/consultation")) {
        // 相談は常に成功扱い: status失敗時のerror noticeは1つだけにする
        return Promise.resolve(consultationOk());
      }
      if (url.includes("/finishing")) return Promise.resolve(okStatus(0));
      if (failing) return Promise.reject(new TypeError("network down"));
      return Promise.resolve(okStatus(1));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<EpisodeView episodeId="ep-poll01" />);
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.queryByTestId("stale-banner")).toBeNull();

    failing = true;
    await vi.advanceTimersByTimeAsync(16000);
    const banner = screen.getByTestId("stale-banner");
    expect(banner.textContent).toContain("状態の更新が途切れています");
    expect(banner.textContent).toContain("安全な中止は未実装です");
    expect(banner.textContent).toContain("再照会");
    // poll失敗のerror noticeとは別物として両方出る
    expect(screen.getByTestId("error-notice")).toBeTruthy();

    failing = false;
    await vi.advanceTimersByTimeAsync(3000);
    expect(screen.queryByTestId("stale-banner")).toBeNull();
    expect(screen.queryByTestId("error-notice")).toBeNull();
  });

  it("途切れbannerは最終取得時刻（時計+経過）を表示する", async () => {
    let failing = false;
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const url = String(input);
      if (url.endsWith("/flags")) return Promise.resolve(flagsOk());
      if (url.endsWith("/preview")) return Promise.resolve(preview404());
      if (url.endsWith("/consultation")) return Promise.resolve(consultationOk());
      if (url.includes("/finishing")) return Promise.resolve(okStatus(0));
      if (failing) return Promise.reject(new TypeError("network down"));
      return Promise.resolve(okStatus(1));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<EpisodeView episodeId="ep-poll01" />);
    await vi.advanceTimersByTimeAsync(0);
    const lastFetchAt = Date.now();

    failing = true;
    await vi.advanceTimersByTimeAsync(16000);
    const line = screen.getByTestId("stale-last-fetch");
    const at = new Date(lastFetchAt);
    const expectedClock = [at.getHours(), at.getMinutes(), at.getSeconds()]
      .map((part) => String(part).padStart(2, "0"))
      .join(":");
    expect(line.textContent).toBe(`最終取得 ${expectedClock}（00:16前）`);

    failing = false;
    await vi.advanceTimersByTimeAsync(3000);
    expect(screen.queryByTestId("stale-last-fetch")).toBeNull();
  });

  it("一度も取得成功がない場合は最終取得を正直に「不明」と出す（開始時計で代用しない）", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const url = String(input);
      if (url.endsWith("/flags")) return Promise.resolve(flagsOk());
      if (url.endsWith("/preview")) return Promise.resolve(preview404());
      if (url.endsWith("/consultation")) return Promise.resolve(consultationOk());
      if (url.includes("/finishing")) return Promise.resolve(okStatus(0));
      return Promise.reject(new TypeError("network down"));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<EpisodeView episodeId="ep-poll01" />);
    await vi.advanceTimersByTimeAsync(16000);
    expect(screen.getByTestId("stale-banner")).toBeTruthy();
    expect(screen.getByTestId("stale-last-fetch").textContent).toBe(
      "最終取得 不明（一度も成功していません）",
    );
  });

  it("focusとvisibilitychangeで即時再照会する（次の2秒tickを待たない）", async () => {
    let statusCalls = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const url = String(input);
      if (url.endsWith("/flags")) return Promise.resolve(flagsOk());
      if (url.endsWith("/preview")) return Promise.resolve(preview404());
      if (url.endsWith("/outputs")) return Promise.resolve(new Response(null, { status: 404 }));
      if (url.endsWith("/self-check")) {
        return Promise.resolve(
          jsonResponse("sc", 200, { episode_id: "ep-poll01", self_check: null }),
        );
      }
      if (url.includes("/consultation")) return Promise.resolve(consultationOk());
      if (url.includes("/finishing")) return Promise.resolve(okStatus(0));
      statusCalls += 1;
      return Promise.resolve(okStatus(statusCalls));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<EpisodeView episodeId="ep-poll01" />);
    await vi.advanceTimersByTimeAsync(0);
    expect(statusCalls).toBe(1);

    await vi.advanceTimersByTimeAsync(500);
    window.dispatchEvent(new Event("focus"));
    await vi.advanceTimersByTimeAsync(0);
    expect(statusCalls).toBe(2);

    await vi.advanceTimersByTimeAsync(500);
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    expect(statusCalls).toBe(3);
  });
});

/** 試し編集と今回の実行/対象版の対応（binding行 + previewOk三値）:
 *  - probeヘッダ（旧run/旧版）とstatus（今回run/新版）の不一致 → 前回の実行
 *    のものです（再生成待ち）+ previewOk false（完了claimを出さない）
 *  - 次のpollで一致ヘッダ → 今回の実行の試し編集です + previewOk true
 *  - ヘッダなし（旧バックエンド）→ 不明 + previewOk null では完了を出さない
 *  previewOkの観測は rebuild-phase 行（awaiting_confirmation か done か）で
 *  行う — これはEpisodeViewの実ツリーを通った三値の挙動そのもの。 */
const BINDING_DRAFT = {
  schema_version: "cockpit-review-command-draft-v1",
  command_id: "rcmd-binding0001",
  command_kind: "keep_longer",
  text: "この後2秒残して",
  target_seconds: 1,
  seconds_delta: 2,
  scope: "episode",
  needs_confirmation: false,
  confirmation_reason: null,
};

const BINDING_APPLIED = {
  schema_version: "cockpit-applied-command-v1",
  command_id: "rcmd-binding0001",
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

const BINDING_REBUILD_PLAN = {
  schema_version: "cockpit-rebuild-plan-v1",
  command_id: "rcmd-binding0001",
  command_kind: "keep_longer",
  affected_domain: "edit_plan",
  stages: ["plan", "compile", "preview", "resolve_build", "qc", "render"],
  excluded_stages: ["ingest", "normalize", "analyze", "selection", "publish"],
};

const BINDING_REBUILD_SCHEDULED = {
  stage_hint: "plan,compile,preview,resolve_build,qc,render",
  scheduled: true,
  applied_command: "rcmd-binding0001",
  stages: BINDING_REBUILD_PLAN.stages,
  runner_log: "/tmp/episodes/ep-bind01/runner.log",
};

function bindingStatusPayload(
  seq: number,
  run: string,
  version: string,
  chain: object[] = [],
): object {
  return {
    episode_id: "ep-bind01",
    job_id: "ep-bind01",
    status: "PREVIEW_READY",
    current_stage: "review",
    created_at_seq: 1,
    updated_at_seq: seq,
    stage_runs: [
      {
        stage_name: "preview",
        status: "succeeded",
        retry_count: 0,
        last_error_code: null,
        run_id: run,
        first_output_arrived_at: "2026-09-09T00:00:00+00:00",
      },
    ],
    current_run: run,
    current_target_version: version,
    pending_rebuild: null,
    rebuild_requests: chain,
    last_worker_report_at: null,
    last_worker_report_event: null,
    unreviewed_proposal_set: false,
    preview_first_arrived_at: "2026-09-09T00:00:00+00:00",
  };
}

function previewResponse(
  status: number,
  headers: Record<string, string>,
): Response {
  return new Response(null, { status, headers });
}

const probeHeaders = (run: string, version: string): Record<string, string> => ({
  "x-cockpit-preview-run-id": run,
  "x-cockpit-preview-target-version": version,
  "x-cockpit-preview-content-sha256": `hash-${run}-${version}`,
  "x-cockpit-preview-output-arrived-at": "2026-09-09T00:00:00+00:00",
});

const probeHeadersNoHash = (run: string, version: string): Record<string, string> => ({
  "x-cockpit-preview-run-id": run,
  "x-cockpit-preview-target-version": version,
  "x-cockpit-preview-output-arrived-at": "2026-09-09T00:00:00+00:00",
});

/** POST /rebuild後にserverが記録するspawned連鎖: 受付前のpoll（空連鎖）
 *  と受付後のpollを区別するTHIS-run証拠。受付前のbaselineは空連鎖なので、
 *  このentryの出現がserverの前進を証明する。 */
const SPAWNED_RUN_NEW = {
  schema_version: "cockpit-rebuild-request-v1",
  sequence: 1,
  stage_hint: "plan,compile,preview,resolve_build,qc,render",
  marker: null,
  spawned: true,
  run_id: "run-new",
  target_version: "v2",
  reserves_sequence: null,
};

async function flushAux(): Promise<void> {
  for (let i = 0; i < 8; i += 1) {
    await vi.advanceTimersByTimeAsync(0);
  }
}

describe("EpisodeView 試し編集binding（実行/対象版対応）", () => {
  it("旧run/旧版ヘッダ→「前回の実行のものです（再生成待ち）」+ previewOk false（完了を出さない）。一致ヘッダの次pollで「今回の実行の試し編集です」+ true", async () => {
    let preview = previewResponse(206, probeHeaders("run-old", "v1"));
    let seq = 0;
    let rebuilt = false;
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const url = String(input);
      if (url.endsWith("/flags")) return Promise.resolve(flagsOk());
      if (url.endsWith("/preview")) return Promise.resolve(preview);
      if (url.endsWith("/review-chat/apply")) {
        return Promise.resolve(
          jsonResponse(url, 200, {
            applied: BINDING_APPLIED,
            rebuild: BINDING_REBUILD_PLAN,
          }),
        );
      }
      if (url.endsWith("/review-chat")) {
        return Promise.resolve(
          jsonResponse(url, 200, { received: true, sequence: 1, draft: BINDING_DRAFT }),
        );
      }
      if (url.endsWith("/rebuild")) {
        rebuilt = true;
        return Promise.resolve(jsonResponse(url, 202, BINDING_REBUILD_SCHEDULED));
      }
      if (url.endsWith("/consultation")) {
        return Promise.resolve(consultationOk());
      }
      if (url.includes("/finishing")) return Promise.resolve(okStatus(0));
      seq += 1;
      return Promise.resolve(
        jsonResponse(
          url,
          200,
          bindingStatusPayload(seq, "run-new", "v2", rebuilt ? [SPAWNED_RUN_NEW] : []),
        ),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<EpisodeView episodeId="ep-bind01" />);
    await flushAux();
    expect(screen.getByTestId("preview-binding").textContent).toBe(
      "前回の実行のものです（再生成待ち。表示中 v1／今回 v2）",
    );

    // rebuild-phase を出して previewOk=false を観測する（今回claimを出さない）
    fireEvent.change(screen.getByLabelText("気になるところを伝える"), {
      target: { value: "この後2秒残して" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));
    await flushAux();
    fireEvent.click(screen.getByTestId("review-apply-button"));
    await flushAux();
    expect(screen.getByTestId("rebuild-indicator")).toBeTruthy();
    // 受付直後の旧pollでは完了も到達claimも出さない（stale-poll honesty）。
    // 次のpollでserverがTHIS runの連鎖を示したら、previewOk=falseのまま
    // 到達確認待ちになる（今回claimを出さない観測はここで保つ）。
    await vi.advanceTimersByTimeAsync(2100);
    await flushAux();
    expect(screen.getByTestId("rebuild-phase").textContent).toBe(
      "試し編集完了・確認してください",
    );

    // 次のpoll: 今回run/対象版に一致する試し編集に差し替わる
    preview = previewResponse(206, probeHeaders("run-new", "v2"));
    await vi.advanceTimersByTimeAsync(2100);
    await flushAux();
    expect(screen.getByTestId("preview-binding").textContent).toBe(
      "今回の実行の試し編集です（対象版 v2）",
    );
    expect(screen.getByTestId("rebuild-phase").textContent).toBe(
      "再build完了（試し編集の更新を確認済み）",
    );
  });

  it("ヘッダなしの旧バックエンド→「不明（対象となる実行を特定できません）」。previewOk null では完了を出さない", async () => {
    let preview = previewResponse(206, {});
    let seq = 0;
    let rebuilt = false;
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const url = String(input);
      if (url.endsWith("/flags")) return Promise.resolve(flagsOk());
      if (url.endsWith("/preview")) return Promise.resolve(preview);
      if (url.endsWith("/review-chat/apply")) {
        return Promise.resolve(
          jsonResponse(url, 200, {
            applied: BINDING_APPLIED,
            rebuild: BINDING_REBUILD_PLAN,
          }),
        );
      }
      if (url.endsWith("/review-chat")) {
        return Promise.resolve(
          jsonResponse(url, 200, { received: true, sequence: 1, draft: BINDING_DRAFT }),
        );
      }
      if (url.endsWith("/rebuild")) {
        rebuilt = true;
        return Promise.resolve(jsonResponse(url, 202, BINDING_REBUILD_SCHEDULED));
      }
      if (url.endsWith("/consultation")) {
        return Promise.resolve(consultationOk());
      }
      if (url.includes("/finishing")) return Promise.resolve(okStatus(0));
      seq += 1;
      return Promise.resolve(
        jsonResponse(
          url,
          200,
          bindingStatusPayload(seq, "run-new", "v2", rebuilt ? [SPAWNED_RUN_NEW] : []),
        ),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<EpisodeView episodeId="ep-bind01" />);
    await flushAux();
    expect(screen.getByTestId("preview-binding").textContent).toBe(
      "不明（対象となる実行を特定できません）",
    );

    fireEvent.change(screen.getByLabelText("気になるところを伝える"), {
      target: { value: "この後2秒残して" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));
    await flushAux();
    fireEvent.click(screen.getByTestId("review-apply-button"));
    await flushAux();
    // 次のpollでserver連鎖が出たら到達確認待ち。previewOk null →
    // 完了（done）には絶対に出ない
    await vi.advanceTimersByTimeAsync(2100);
    await flushAux();
    expect(screen.getByTestId("rebuild-phase").textContent).toBe(
      "試し編集完了・確認してください",
    );
  });

  it("probe失敗（500）→ 直前の再生可能videoを保持したまま binding行は「確認できません…」", async () => {
    let preview = previewResponse(206, probeHeaders("run-new", "v2"));
    let seq = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const url = String(input);
      if (url.endsWith("/flags")) return Promise.resolve(flagsOk());
      if (url.endsWith("/preview")) return Promise.resolve(preview);
      if (url.endsWith("/consultation")) {
        return Promise.resolve(consultationOk());
      }
      if (url.includes("/finishing")) return Promise.resolve(okStatus(0));
      seq += 1;
      return Promise.resolve(
        jsonResponse(url, 200, bindingStatusPayload(seq, "run-new", "v2")),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<EpisodeView episodeId="ep-bind01" />);
    await flushAux();
    expect(screen.getByTestId("preview-player")).toBeTruthy();
    expect(screen.getByTestId("preview-binding").textContent).toBe(
      "今回の実行の試し編集です（対象版 v2）",
    );

    preview = previewResponse(500, {});
    await vi.advanceTimersByTimeAsync(2100);
    await flushAux();
    // 動画は消さない（最後の再生可能なものを保持）、対応claimだけ即座に落とす
    expect(screen.getByTestId("preview-player")).toBeTruthy();
    expect(screen.getByTestId("preview-binding").textContent).toBe(
      "確認できません（試し編集の情報を取得できません）",
    );
  });

  it("probe失敗でもvideo要素・src・再生位置を保持し、今回claimだけ落とす（previewOkはnullへ）", async () => {
    let preview = previewResponse(206, probeHeaders("run-new", "v2"));
    let seq = 0;
    let rebuilt = false;
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const url = String(input);
      if (url.endsWith("/flags")) return Promise.resolve(flagsOk());
      if (url.endsWith("/preview")) return Promise.resolve(preview);
      if (url.endsWith("/review-chat/apply")) {
        return Promise.resolve(
          jsonResponse(url, 200, {
            applied: BINDING_APPLIED,
            rebuild: BINDING_REBUILD_PLAN,
          }),
        );
      }
      if (url.endsWith("/review-chat")) {
        return Promise.resolve(
          jsonResponse(url, 200, { received: true, sequence: 1, draft: BINDING_DRAFT }),
        );
      }
      if (url.endsWith("/rebuild")) {
        rebuilt = true;
        return Promise.resolve(jsonResponse(url, 202, BINDING_REBUILD_SCHEDULED));
      }
      if (url.endsWith("/consultation")) {
        return Promise.resolve(consultationOk());
      }
      if (url.includes("/finishing")) return Promise.resolve(okStatus(0));
      seq += 1;
      return Promise.resolve(
        jsonResponse(
          url,
          200,
          bindingStatusPayload(seq, "run-new", "v2", rebuilt ? [SPAWNED_RUN_NEW] : []),
        ),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<EpisodeView episodeId="ep-bind01" />);
    await flushAux();
    const before = screen.getByTestId("preview-player") as HTMLVideoElement;
    expect(before.getAttribute("src")).toBe(
      "/cockpit-api/episodes/ep-bind01/preview?content_hash=hash-run-new-v2",
    );

    // previewOk=true を rebuild-phase（done）で確定させる: 受付後のpollで
    // server連鎖が出てからTHIS runの完了になる
    fireEvent.change(screen.getByLabelText("気になるところを伝える"), {
      target: { value: "この後2秒残して" },
    });
    fireEvent.click(screen.getByTestId("review-chat-send"));
    await flushAux();
    fireEvent.click(screen.getByTestId("review-apply-button"));
    await flushAux();
    await vi.advanceTimersByTimeAsync(2100);
    await flushAux();
    expect(screen.getByTestId("rebuild-phase").textContent).toBe(
      "再build完了（試し編集の更新を確認済み）",
    );

    // 再生中の位置を付けてから次のpollのprobeを失敗させる
    before.currentTime = 42.5;
    preview = previewResponse(500, {});
    await vi.advanceTimersByTimeAsync(2100);
    await flushAux();

    // 同一DOM要素・同一src・再生位置を保持し、今回claimだけ落とす
    const after = screen.getByTestId("preview-player") as HTMLVideoElement;
    expect(after).toBe(before);
    expect(after.getAttribute("src")).toBe(
      "/cockpit-api/episodes/ep-bind01/preview?content_hash=hash-run-new-v2",
    );
    expect(after.currentTime).toBeCloseTo(42.5, 5);
    expect(screen.getByTestId("preview-binding").textContent).toBe(
      "確認できません（試し編集の情報を取得できません）",
    );
    expect(screen.getByTestId("rebuild-phase").textContent).toBe(
      "試し編集完了・確認してください",
    );
  });

  it("回復時は同hashで要素を維持し、別hashでは正当に作り直す（stale再生をしない）", async () => {
    let preview = previewResponse(206, probeHeaders("run-new", "v2"));
    let seq = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const url = String(input);
      if (url.endsWith("/flags")) return Promise.resolve(flagsOk());
      if (url.endsWith("/preview")) return Promise.resolve(preview);
      if (url.endsWith("/consultation")) {
        return Promise.resolve(consultationOk());
      }
      if (url.includes("/finishing")) return Promise.resolve(okStatus(0));
      seq += 1;
      return Promise.resolve(
        jsonResponse(url, 200, bindingStatusPayload(seq, "run-new", "v2")),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<EpisodeView episodeId="ep-bind01" />);
    await flushAux();
    const first = screen.getByTestId("preview-player") as HTMLVideoElement;
    expect(first.getAttribute("src")).toBe(
      "/cockpit-api/episodes/ep-bind01/preview?content_hash=hash-run-new-v2",
    );
    first.currentTime = 10;

    // 失敗 → 同一要素を保持
    preview = previewResponse(500, {});
    await vi.advanceTimersByTimeAsync(2100);
    await flushAux();
    expect(screen.getByTestId("preview-player")).toBe(first);
    expect((screen.getByTestId("preview-player") as HTMLVideoElement).currentTime).toBeCloseTo(
      10,
      5,
    );

    // 同hashで回復 → 依然同一要素、binding行も復帰
    preview = previewResponse(206, probeHeaders("run-new", "v2"));
    await vi.advanceTimersByTimeAsync(2100);
    await flushAux();
    const recovered = screen.getByTestId("preview-player") as HTMLVideoElement;
    expect(recovered).toBe(first);
    expect(recovered.getAttribute("src")).toBe(
      "/cockpit-api/episodes/ep-bind01/preview?content_hash=hash-run-new-v2",
    );
    expect(screen.getByTestId("preview-binding").textContent).toBe(
      "今回の実行の試し編集です（対象版 v2）",
    );

    // 別hash（新しい試し編集）→ 正当に作り直す
    preview = previewResponse(206, probeHeaders("run-next", "v3"));
    await vi.advanceTimersByTimeAsync(2100);
    await flushAux();
    const rekeyed = screen.getByTestId("preview-player") as HTMLVideoElement;
    expect(rekeyed).not.toBe(first);
    expect(rekeyed.getAttribute("src")).toBe(
      "/cockpit-api/episodes/ep-bind01/preview?content_hash=hash-run-next-v3",
    );
  });

  it("W7: 成功null観測を保存し、続く失敗で古いhashへ戻さない（要素再生成なし）", async () => {
    let preview = previewResponse(206, probeHeaders("run-new", "v2"));
    let seq = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const url = String(input);
      if (url.endsWith("/flags")) return Promise.resolve(flagsOk());
      if (url.endsWith("/preview")) return Promise.resolve(preview);
      if (url.endsWith("/consultation")) {
        return Promise.resolve(consultationOk());
      }
      if (url.includes("/finishing")) return Promise.resolve(okStatus(0));
      seq += 1;
      return Promise.resolve(
        jsonResponse(url, 200, bindingStatusPayload(seq, "run-new", "v2")),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<EpisodeView episodeId="ep-bind01" />);
    await flushAux();
    expect((screen.getByTestId("preview-player") as HTMLVideoElement).getAttribute("src")).toBe(
      "/cockpit-api/episodes/ep-bind01/preview?content_hash=hash-run-new-v2",
    );

    // 成功だがheaderなし(null) → null表示（固定URL）へ遷る
    preview = previewResponse(206, probeHeadersNoHash("run-new", "v2"));
    await vi.advanceTimersByTimeAsync(2100);
    await flushAux();
    const nulled = screen.getByTestId("preview-player") as HTMLVideoElement;
    expect(nulled.getAttribute("src")).toBe("/cockpit-api/episodes/ep-bind01/preview");
    nulled.currentTime = 7.25;

    // 失敗だけでは再生identityを変えない: 同一要素・同一src・再生位置を保持
    preview = previewResponse(500, {});
    await vi.advanceTimersByTimeAsync(2100);
    await flushAux();
    const after = screen.getByTestId("preview-player") as HTMLVideoElement;
    expect(after).toBe(nulled);
    expect(after.getAttribute("src")).toBe("/cockpit-api/episodes/ep-bind01/preview");
    expect(after.getAttribute("src")).not.toContain("hash-run-new-v2");
    expect(after.currentTime).toBeCloseTo(7.25, 5);
    expect(screen.getByTestId("preview-binding").textContent).toBe(
      "確認できません（試し編集の情報を取得できません）",
    );
  });
});
