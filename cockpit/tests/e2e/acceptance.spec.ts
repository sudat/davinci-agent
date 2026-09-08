import { execFileSync, spawn } from "node:child_process";
import { expect, test } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { record, writeChecklist } from "./gate-v43-4a-checklist";

const API_BASE = process.env.COCKPIT_API ?? "http://127.0.0.1:8765";
const BACKEND_PORT = Number(process.env.COCKPIT_E2E_PORT ?? 8765);
const STATE_ROOT = path.resolve(process.cwd(), ".e2e", "state");
const EPISODES_ROOT = path.join(STATE_ROOT, "episodes");
const STATE_STORE = path.join(STATE_ROOT, "state.db");
const VIDEO_PIPELINE_DIR = path.resolve(process.cwd(), "..", "video-pipeline");
const REPO_ROOT = path.resolve(process.cwd(), "..");

/**
 * Gate V43-4a acceptance flow (task 51) — the whole operator journey on
 * the REAL backend + REAL frontend, driven only through the browser:
 * intake → synthetic fast-path to a reviewable state → timestamp jump →
 * NL correction → structured preview → partial rebuild (202) → bundled
 * approvals (≤2 blocking sessions) → lightweight publishability note →
 * backend SIGTERM/restart resume. Fixtures (state rows, preview clip,
 * review store, approval ledger) are seeded BACKSTAGE by this harness
 * before each operator step; the operator-visible flow itself never
 * touches a terminal, JSON, or Resolve.
 */

test.describe.configure({ mode: "serial" });

function trackConsoleErrors(
  page: import("@playwright/test").Page,
  allowedResourceStatuses: number[] = [],
): string[] {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const text = message.text();
    const match =
      /^Failed to load resource: the server responded with a status of (\d+)/.exec(
        text,
      );
    if (match && allowedResourceStatuses.includes(Number(match[1]))) return;
    errors.push(text);
  });
  page.on("pageerror", (error) => errors.push(String(error)));
  return errors;
}

const SEED_PIPELINE_STATE = `
import sqlite3
import sys
from pathlib import Path

from services.approvals.ingress import record_fixture_operation
from services.approvals.store import OperationRecordStore
from services.job_runner.state_models import StageRunRow
from services.job_runner.state_store import StateStore

episode_id = sys.argv[1]
episodes_root = Path(sys.argv[2])
state_store = Path(sys.argv[3])

with StateStore.open(state_store) as store:
    for stage in (
        "ingest", "normalize", "analyze", "selection", "plan", "compile", "preview"
    ):
        store.record_stage_run(
            StageRunRow(
                job_id=episode_id,
                stage_name=stage,
                idempotency_key=stage + "-run-1",
                input_artifact_hashes=(),
                adopted_artifact_hash=None,
                status="succeeded",
            )
        )
connection = sqlite3.connect(state_store)
connection.execute(
    "UPDATE jobs SET status = 'PREVIEW_READY', current_stage = 'preview'"
    " WHERE job_id = ?",
    (episode_id,),
)
connection.commit()
connection.close()

ledger = OperationRecordStore(
    episodes_root / episode_id / "approvals" / "records.jsonl"
)
for purpose, target in (
    ("editorial", "1" * 64),
    ("presentation", "2" * 64),
    ("final", "3" * 64),
    ("publication", "4" * 64),
):
    ledger.append(
        record_fixture_operation(
            purpose=purpose,
            target_bundle_hash=target,
            decision="reject",
            actor_id="pipeline-request",
        )
    )
print("state-seeded")
`;

// Same review-store shape as the task-46 seed, but total_frames=750 with
// item v1 spanning [0,150): apply keep_longer at 1.0s (frame 30) extends
// v1 to 210 — a real commit_command version bump the apply route reads.
const SEED_REVIEW_STORE = `
import sys
from pathlib import Path

from services.contracts.edit_plan_0c import (
    EditPlan0C,
    EditPlanBody0C,
    EditPlanItem0C,
    EditSourceRef0C,
    ItemIdSelector0C,
)
from services.contracts.primitives import (
    Producer,
    RationalFrameRate,
    SourceFrameSpan,
)
from services.review_command.events import GENESIS_EVENT_HASH, build_event
from services.review_command.models import (
    AdjustSourceSpanProposal0C,
    Confidence0C,
    ProposalAmbiguity0C,
    SpanBounds0C,
)
from services.review_command.store import append_events, initialize_store

rate = RationalFrameRate(num=30, den=1)
plan = EditPlan0C(
    artifact_id="edit-plan-acceptance-seed",
    artifact_type="edit_plan_0c",
    schema_version="edit-plan-0c-v1",
    content_hash="0" * 64,
    producer=Producer(name="cockpit-e2e", version="1"),
    inputs=(),
    frame_rate=rate,
    plan=EditPlanBody0C(
        plan_version="v1",
        edit_source=EditSourceRef0C(source_id="src-e2e", total_frames=750),
        items=(
            EditPlanItem0C(
                item_id="v1",
                kind="video",
                source_id="src-e2e",
                span=SourceFrameSpan(start_frame=0, end_frame=150, rate=rate),
                track_index=1,
            ),
        ),
    ),
)
base = Path(sys.argv[1])
log_path = base / "review" / "events.jsonl"
plan_dir = base / "review" / "store"
initialize_store(plan, log_path, plan_dir)
proposal = AdjustSourceSpanProposal0C(
    proposal_id="prop-acceptance-1",
    command_kind="adjust_source_span",
    base_plan_version="v1",
    actor_intent="model",
    sequence=1,
    confidence=Confidence0C(num=9, den=10),
    ambiguity=ProposalAmbiguity0C(status="clear"),
    evidence=(),
    target=ItemIdSelector0C(kind="item_id", item_id="v1"),
    new_span=SpanBounds0C(start_frame=15, end_frame=45),
)
event = build_event(
    sequence=1,
    kind="proposal_recorded",
    proposal=proposal,
    base_plan_version="v1",
    previous_event_hash=GENESIS_EVENT_HASH,
    actor_intent="model",
)
append_events(log_path, (event,))
print("review-store-seeded")
`;

function seedPipelineState(episodeId: string): void {
  execFileSync(
    "uv",
    [
      "run",
      "python",
      "-c",
      SEED_PIPELINE_STATE,
      episodeId,
      EPISODES_ROOT,
      STATE_STORE,
    ],
    { cwd: VIDEO_PIPELINE_DIR, stdio: "ignore" },
  );
}

function seedReviewStore(episodeId: string): void {
  execFileSync(
    "uv",
    ["run", "python", "-c", SEED_REVIEW_STORE, path.join(EPISODES_ROOT, episodeId)],
    { cwd: VIDEO_PIPELINE_DIR, stdio: "ignore" },
  );
}

function seedPreviewClip(episodeId: string): void {
  const previewDir = path.join(EPISODES_ROOT, episodeId, "previews");
  fs.mkdirSync(previewDir, { recursive: true });
  execFileSync(
    "ffmpeg",
    [
      "-y",
      "-f",
      "lavfi",
      "-i",
      "testsrc=duration=2:size=320x240:rate=15",
      "-pix_fmt",
      "yuv420p",
      "-c:v",
      "libx264",
      path.join(previewDir, "preview.mp4"),
    ],
    { stdio: "ignore" },
  );
}

function backendPids(): number[] {
  try {
    return execFileSync("lsof", ["-nP", "-ti", `tcp:${BACKEND_PORT}`, "-sTCP:LISTEN"])
      .toString()
      .trim()
      .split("\n")
      .filter((line) => line !== "")
      .map((line) => Number(line));
  } catch {
    return [];
  }
}

async function restartBackend(): Promise<void> {
  for (const pid of backendPids()) {
    process.kill(pid, "SIGTERM");
  }
  const deadline = Date.now() + 20_000;
  while (backendPids().length > 0 && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  expect(backendPids(), "SIGTERM must free the backend port").toEqual([]);

  const replacement = spawn(
    "uv",
    [
      "run",
      "python",
      "-m",
      "services.cli",
      "cockpit",
      "--port",
      String(BACKEND_PORT),
      "--episodes-root",
      EPISODES_ROOT,
      "--state-store",
      STATE_STORE,
    ],
    { cwd: VIDEO_PIPELINE_DIR, detached: true, stdio: "ignore" },
  );
  replacement.unref();

  const healthy = Date.now() + 30_000;
  for (;;) {
    try {
      const probe = await fetch(`${API_BASE}/episodes`);
      if (probe.ok) return;
    } catch {
      // not up yet
    }
    if (Date.now() > healthy) throw new Error("replacement backend never became healthy");
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
}

let episodeId = "";

test("intake: brief+ソース+Start一回で開始（artifact欄なし / malformed probe）", async ({
  page,
}) => {
  test.setTimeout(60_000);
  const consoleErrors = trackConsoleErrors(page, [404]);

  await page.goto("/");
  await expect(page).toHaveURL(/\/new-episode$/);

  const createButton = page.getByTestId("create-button");
  await expect(createButton).toBeDisabled();

  await page.getByLabel("この動画は何について？").fill("受け入れ: 話したいテーマを簡潔に");
  const sourceDir = fs.mkdtempSync(path.join(os.tmpdir(), "cockpit-accept-src-"));
  await page.getByLabel("ソースフォルダ").fill(sourceDir);

  const offendingFields = await page.evaluate(() =>
    Array.from(document.querySelectorAll("input, textarea, select"))
      .map((element) =>
        [
          element.id,
          element.getAttribute("name") ?? "",
          element.getAttribute("aria-label") ?? "",
          element.getAttribute("placeholder") ?? "",
        ].join(" "),
      )
      .filter((blob) => /artifact|job[-_\s]?id/i.test(blob)),
  );
  expect(offendingFields, "intake must not expose artifact/job-id fields").toEqual([]);

  await expect(page.locator("label[for='reference-input']")).toBeVisible();
  await expect(page.getByLabel("参照（URL / ローカルパス / 保存済みID）")).toBeAttached();

  await createButton.click();
  await page.waitForURL(/\/episodes\/ep-[0-9a-f]+$/, { timeout: 20_000 });
  episodeId = page.url().split("/").pop() as string;

  await expect(page.getByTestId("episode-status")).toHaveText("CREATED");
  await expect(page.getByTestId("current-stage")).toHaveText("intake");

  await page.goto("/episodes/ep-malformed!!id");
  const malformedNotice = page.getByTestId("error-notice").first();
  await expect(malformedNotice).toBeVisible({ timeout: 15_000 });
  await expect(malformedNotice).toContainText("episode-not-found");

  record(
    "start-from-source-and-brief",
    "pass",
    `UI intakeのみで作成: brief+ソースフォルダ+Create 1回 → ${episodeId} (CREATED)。任意の参照欄は任意入力。job/artifact-id入力欄は不在（機械検証済み）`,
  );

  expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
});

test("合成fast-path: review可能な状態まで到達（stage実行待ちなし）", async ({ page }) => {
  test.setTimeout(90_000);
  const consoleErrors = trackConsoleErrors(page, [404]);

  seedPipelineState(episodeId);
  seedPreviewClip(episodeId);
  seedReviewStore(episodeId);

  await page.goto(`/episodes/${episodeId}`);
  // 受入a（codex指摘反映）: 語彙と正直な接尾辞をそれぞれ確認する。
  await expect(page.getByTestId("episode-status")).toContainText("PREVIEW_READY", {
    timeout: 15_000,
  });
  await expect(page.getByTestId("episode-status")).toContainText(
    "試し編集完了（全体の完了ではありません）",
  );
  await expect(page.getByTestId("current-stage")).toHaveText("preview");
  await expect(page.getByTestId("preview-player")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("flag-item")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("error-notice")).toHaveCount(0);

  expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
});

test("timestamp review jump: flag→player seek", async ({ page }) => {
  test.setTimeout(60_000);
  const consoleErrors = trackConsoleErrors(page);

  const FLAG_TS = 1.0;
  await page.route(`**/cockpit-api/episodes/${episodeId}/flags`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        flags: [
          {
            sequence: 1,
            kind: "proposal_recorded",
            reason: "1秒地点を確認",
            at_seconds: FLAG_TS,
          },
        ],
        not_yet_generated: false,
      }),
    }),
  );

  await page.goto(`/episodes/${episodeId}`);
  await expect(page.getByTestId("preview-player")).toBeVisible({ timeout: 15_000 });
  await page.waitForFunction(() => {
    const video = document.querySelector<HTMLVideoElement>(
      '[data-testid="preview-player"]',
    );
    return video !== null && video.readyState >= 1;
  });

  await page.getByTestId("flag-jump").click();

  await page.waitForFunction(
    (target) => {
      const video = document.querySelector<HTMLVideoElement>(
        '[data-testid="preview-player"]',
      );
      return video !== null && Math.abs(video.currentTime - target) < 0.4;
    },
    FLAG_TS,
    { timeout: 10_000 },
  );

  record(
    "timestamp-review-jump",
    "pass",
    "flagのtimestampクリック → 実previewの<video>が1.0sへseek（task-46と同じdocumented mock: flags payloadにはat_seconds前方形状のみ差し替え、seek先は実page・実preview・実video）",
  );

  expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
});

test("NL修正→構造化プレビュー→適用→部分rebuild 202", async ({ page }) => {
  test.setTimeout(60_000);
  const consoleErrors = trackConsoleErrors(page);

  await page.goto(`/episodes/${episodeId}`);
  await expect(page.getByTestId("preview-player")).toBeVisible({ timeout: 15_000 });
  await page.waitForFunction(() => {
    const video = document.querySelector<HTMLVideoElement>(
      '[data-testid="preview-player"]',
    );
    return video !== null && video.readyState >= 1;
  });
  await page.evaluate(() => {
    const video = document.querySelector<HTMLVideoElement>(
      '[data-testid="preview-player"]',
    );
    if (video !== null) video.currentTime = 1.0;
  });

  await page
    .getByLabel("修正指示（自然言語）")
    .fill("この後2秒残して");
  await page.getByTestId("review-chat-send").click();

  await expect(page.getByTestId("review-draft")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("review-draft-kind")).toHaveText("長めに残す");
  await expect(page.getByTestId("review-draft-delta")).toHaveText("+2s");
  await expect(page.getByTestId("review-draft-needs-confirmation")).toHaveCount(0);

  await page.getByTestId("review-apply-button").click();

  await expect(page.getByTestId("rebuild-indicator")).toBeVisible({ timeout: 20_000 });
  // 受入b（codex指摘反映）: 合成fast-pathはrun/成果物を持たず、rebuildは
  // preview段階の失敗という既知の終端に至る。全許容regexではなく、期待する
  // 停止の終端表示そのものを確認する。成功経路（予約→実行→完了＋版付き
  // 成果物の実測）はここで主張せず、live-episode.spec.ts（実chain・
  // LIVE_V44_E2E=1）が担保する。
  await expect(page.getByTestId("rebuild-phase")).toHaveText(
    "再buildが止まっています（確認が必要です）",
    { timeout: 20_000 },
  );
  const stageHint = await page.getByTestId("rebuild-stage-hint").textContent();
  expect(stageHint ?? "").toContain("plan");
  expect(stageHint ?? "").toContain("render");
  expect(stageHint ?? "").not.toContain("ingest");

  record(
    "nl-correction-structured-partial-rebuild",
    "pass",
    `「この後2秒残して」→ draft echo (keep_longer/+2s/target 1s) → 適用 → rebuild scheduled (202) + stage hint "${stageHint}"（selection/ingest等の無関係stageは再実行対象外）+ 期待終端「再buildが止まっています（確認が必要です）」をassert（合成環境はrun/無しでpreview段階が失敗する既知経路）。実chainでの成功経路はLIVE_V44_E2E=1のlive-episode.spec.tsが担保`,
  );

  expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
});

test("承認bundling: ブロックセッション2回で全承認（≤2）", async ({ page }) => {
  test.setTimeout(90_000);
  const consoleErrors = trackConsoleErrors(page);

  await page.goto(`/episodes/${episodeId}`);
  const sessions = page.getByTestId("approval-session");
  await expect(sessions.first()).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("blocking-session-count")).toHaveText(
    /ブロック承認セッション: 2/,
  );
  await expect(sessions).toHaveCount(2);
  await expect(page.getByTestId("approval-item")).toHaveCount(4);
  await expect(page.getByTestId("error-notice")).toHaveCount(0);

  const finalSession = page.locator(
    '[data-testid="approval-session"][data-session-key="final-publication"]',
  );
  await expect(finalSession).toContainText("公開承認");

  let blockingPrompts = 0;
  for (const sessionKey of ["editorial-presentation", "final-publication"]) {
    await page
      .locator(`[data-testid="approval-session"][data-session-key="${sessionKey}"]`)
      .getByTestId("approve-session-button")
      .click();
    blockingPrompts += 1;
    await expect(page.getByTestId("error-notice")).toHaveCount(0);
  }
  expect(blockingPrompts).toBe(2);

  await expect(page.getByTestId("approvals-empty")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("blocking-session-count")).toHaveText(
    /ブロック承認セッション: 0/,
  );
  await expect(page.getByTestId("decided-item")).toHaveCount(4);

  record(
    "blocking-sessions-le2",
    "pass",
    "editorial+presentation+final+publicationの4承認が2 normalセッションにbundleされ、オペレータのブロックpromptは2回（editorial-presentation / final-publication）。承認後は0。公開承認はセッション構造の一部としてUI上存在（公開フロー実行はGate V43-5参照）",
  );
  record(
    "interruption-policy-no-avoidable-prompts",
    "pass",
    "driven flow全体でオペレータにブロックしたのは上記2セッションのみ。error cardの出現0回・余分な確認prompt 0回・console error 0件（各testで機械検証）",
  );

  expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
});

test("軽量Publishability feedback: 注釈保存・スコアカードなし", async ({ page }) => {
  test.setTimeout(90_000);
  const consoleErrors = trackConsoleErrors(page);

  const runKey = `${Date.now()}-${process.pid}`;
  const refFile = path.join(os.tmpdir(), `cockpit-accept-ref-${runKey}.mp4`);
  fs.writeFileSync(refFile, `acceptance-ref-bytes-${runKey}`);

  await page.goto(`/episodes/${episodeId}`);
  await expect(page.getByTestId("reference-annotator")).toBeVisible({
    timeout: 15_000,
  });

  await page.getByLabel("参照動画のパス（ローカルファイル）").fill(refFile);
  await page.getByLabel("注釈コメント（日本語で自由記述）").fill("色が良い");
  await page.getByTestId("parse-preview-button").click();

  await expect(page.getByTestId("domain-chip-color")).toBeVisible({ timeout: 15_000 });
  await page.getByTestId("save-annotation-button").click();
  await expect(page.getByTestId("saved-annotation-item")).toBeVisible({
    timeout: 20_000,
  });

  await expect(page.locator('input[type="range"]')).toHaveCount(0);
  await expect(page.getByTestId("error-notice")).toHaveCount(0);

  record(
    "publishability-lightweight-feedback",
    "pass",
    "reference注釈（「色が良い」→colorドメイン抽出→保存）が軽量フィードバックとして記録され、数値スコアカード（range入力等）は不存在",
  );

  expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
});

test("restart resume: SIGTERM→再起→episode状態と承認済みが保持", async ({ page }) => {
  test.setTimeout(120_000);
  const consoleErrors = trackConsoleErrors(page, [404]);

  await restartBackend();

  await page.goto(`/episodes/${episodeId}`);
  // 受入a（codex指摘反映）: restart後も語彙と接尾辞が揃って表示され続ける。
  await expect(page.getByTestId("episode-status")).toContainText("PREVIEW_READY", {
    timeout: 20_000,
  });
  await expect(page.getByTestId("episode-status")).toContainText(
    "試し編集完了（全体の完了ではありません）",
  );
  await expect(page.getByTestId("current-stage")).toHaveText("preview");
  await expect(page.getByTestId("preview-player")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("decided-item")).toHaveCount(4);
  await expect(page.getByTestId("approvals-empty")).toBeVisible();
  await expect(page.getByTestId("error-notice")).toHaveCount(0);

  const manualFinalizationDir = path.join(
    EPISODES_ROOT,
    episodeId,
    "manual-finalization",
  );
  expect(
    fs.existsSync(manualFinalizationDir),
    "flow must not hide direct Resolve manipulation",
  ).toBe(false);

  record(
    "transient-restart-resumes",
    "pass",
    "バックエンドuvicornをSIGTERM→同一workspaceで再起→reload後も episode status PREVIEW_READY / stage preview / preview / 承認済み4件が保持（持続state: StateStore + 承認台帳 + workspaceファイル）",
  );
  record(
    "direct-resolve-recorded-as-manual",
    "pass",
    "フロー中に直接Resolve操作は発生せず（manual-finalization記録なし）。発生した場合はManual Finalizationとして記録される経路がT49実装済み",
  );

  expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
});

test("Gate V43-4a checklist artifact書き出し（全項目pass）", async () => {
  record(
    "cockpit-only-completion",
    "pass",
    `作成→レビュー→修正→部分rebuild→承認→注釈→再起復帰の全オペレータステップが ${episodeId} のブラウザUIのみで完結（各testはpage操作のみで駆動）`,
  );
  record(
    "no-cli-json-or-direct-resolve-required",
    "pass",
    "オペレータフローにshell/JSON編集/直接Resolveステップ0件（fixture seedingはharnessのbackstage準備で、オペレータ操作には非公開）。本e2e自体が証拠",
  );

  const outPath = writeChecklist(REPO_ROOT);
  const artifact = JSON.parse(fs.readFileSync(outPath, "utf8")) as {
    summary: { total: number; pass: number; fail: number };
  };
  expect(artifact.summary).toEqual({ total: 10, pass: 10, fail: 0 });
});
