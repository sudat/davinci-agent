import { execFileSync } from "node:child_process";
import { expect, test } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const API_BASE = process.env.COCKPIT_API ?? "http://127.0.0.1:8765";
// __dirname is cockpit/tests/e2e — the playwright config's state root is
// cockpit/.e2e (two levels up), NOT cockpit/tests/.e2e.
const EPISODES_ROOT = path.resolve(__dirname, "..", "..", ".e2e", "state", "episodes");
const VIDEO_PIPELINE_DIR = path.resolve(__dirname, "..", "..", "..", "video-pipeline");

/**
 * Episode view e2e (task 46).
 *
 * Fixtures are REAL wherever the API allows it:
 * - preview material: task 60's preview-v2 artifacts do not exist yet, so a
 *   tiny synthetic clip (ffmpeg lavfi testsrc) is dropped into the episode
 *   workspace preview path the backend actually serves.
 * - flags: a real review_command store (plan + sealed event log with one
 *   proposal_recorded event) is seeded under the episode workspace so the
 *   backend's not_yet_generated → with-flags path is exercised for real.
 *
 * The seek test mocks ONLY the flags GET payload (documented component/
 * network-level mocking): the task-44 flags payload carries no timestamp
 * field yet, and the seek wiring is verified against the real page, real
 * preview file and real <video> element.
 */

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

async function createEpisode(
  request: import("@playwright/test").APIRequestContext,
): Promise<string> {
  const sourceDir = fs.mkdtempSync(path.join(os.tmpdir(), "cockpit-epview-src-"));
  const response = await request.post(`${API_BASE}/episodes`, {
    data: {
      source_folder: sourceDir,
      brief_text: "e2eテスト: episode view 表示確認用",
    },
  });
  expect(response.status()).toBe(200);
  const body = (await response.json()) as { episode_id: string };
  return body.episode_id;
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
    artifact_id="edit-plan-e2e-seed",
    artifact_type="edit_plan_0c",
    schema_version="edit-plan-0c-v1",
    content_hash="0" * 64,
    producer=Producer(name="cockpit-e2e", version="1"),
    inputs=(),
    frame_rate=rate,
    plan=EditPlanBody0C(
        plan_version="v1",
        edit_source=EditSourceRef0C(source_id="src-e2e", total_frames=60),
        items=(
            EditPlanItem0C(
                item_id="v1",
                kind="video",
                source_id="src-e2e",
                span=SourceFrameSpan(start_frame=0, end_frame=60, rate=rate),
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
    proposal_id="prop-e2e-1",
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
print("seeded")
`;

function seedReviewStore(episodeId: string): void {
  execFileSync(
    "uv",
    ["run", "python", "-c", SEED_REVIEW_STORE, path.join(EPISODES_ROOT, episodeId)],
    { cwd: VIDEO_PIPELINE_DIR, stdio: "ignore" },
  );
}

test("episode view: 未生成→実fixtureでplayer/flagsが表示され、pollが状態を反映する（ETA捏造なし）", async ({
  page,
  request,
}) => {
  const consoleErrors = trackConsoleErrors(page, [404]);
  const episodeId = await createEpisode(request);

  await page.goto(`/episodes/${episodeId}`);

  await expect(page.getByTestId("episode-status")).toHaveText("CREATED", {
    timeout: 15_000,
  });
  await expect(page.getByTestId("current-stage")).toHaveText("intake");

  // 生成前の正直な状態: preview未生成（構造化された待機状態）・flags未生成
  await expect(page.getByTestId("preview-pending")).toBeVisible();
  await expect(page.getByTestId("flags-empty")).toContainText("まだ生成");
  // 計測裏付けのないETAは出さない
  await expect(page.getByTestId("eta")).toHaveCount(0);
  await expect(page.getByTestId("before-after")).toHaveCount(0);

  // 実fixture投入: preview素材 + review store（バックエンドが読む実ファイル）
  seedPreviewClip(episodeId);
  seedReviewStore(episodeId);

  // stale_state probe: 2秒pollが状態遷移を反映する
  await expect(page.getByTestId("preview-player")).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.getByTestId("flag-item")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("flag-item")).toContainText("proposal_recorded");
  await expect(page.getByTestId("preview-pending")).toHaveCount(0);
  // 実payloadにeta_minutesはない → ETAは今後も表示しない
  await expect(page.getByTestId("eta")).toHaveCount(0);

  await page.screenshot({
    path: path.join("test-results", "episode-view.png"),
    fullPage: true,
  });

  expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
});

test("flag click → player seek（タイムスタンプ付きflagsは文書化されたモック）", async ({
  page,
  request,
}) => {
  const consoleErrors = trackConsoleErrors(page);
  const episodeId = await createEpisode(request);
  seedPreviewClip(episodeId);

  // task-44のflags payloadはtimestampを持たないため、at_seconds付きの
  //前方形状だけをネットワークで差し替える（seek先は実page・実preview・実video）
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

  const player = page.getByTestId("preview-player");
  await page.waitForFunction(() => {
    const video = document.querySelector<HTMLVideoElement>(
      '[data-testid="preview-player"]',
    );
    return video !== null && video.readyState >= 1;
  });

  const jump = page.getByTestId("flag-jump");
  await expect(jump).toBeEnabled();
  await jump.click();

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
  const currentTime = await player.evaluate(
    (element) => (element as HTMLVideoElement).currentTime,
  );
  expect(Math.abs(currentTime - FLAG_TS)).toBeLessThan(0.4);

  expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
});

test("unknown episode → 構造化error表示（malformed input）", async ({ page }) => {
  const consoleErrors = trackConsoleErrors(page, [404]);

  await page.goto("/episodes/ep-doesnotexist9999");

  const notice = page.getByTestId("error-notice");
  await expect(notice).toBeVisible({ timeout: 15_000 });
  await expect(notice).toContainText("episode-not-found");
  await expect(page.getByText("このエピソードは見つかりません。")).toBeVisible();
  await expect(page.getByTestId("preview-pending").or(page.getByTestId("preview-checking"))).toBeVisible();

  expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
});
