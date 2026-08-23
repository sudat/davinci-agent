import { expect, test } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

/**
 * LIVE (non-seeded) real-chain E2E (task 10, Tier C).
 *
 * Skipped unless `LIVE_V44_E2E=1`: the run drives the REAL pipeline
 * (detached episode_runner → ingest → normalize → REAL whisper ASR →
 * heuristic-director selection → plan → compile → preview render) against
 * the committed Japanese-speech fixture, entirely through the cockpit UI.
 * NOTHING is seeded — no pipeline-state planting, no sqlite writes, no
 * preview or review-store fixtures (the back-stage seeding pattern of
 * acceptance.spec.ts stays there; this file must never grow it). Bounded
 * polls dump the episode's runner.log on timeout so a hung chain fails
 * with evidence.
 *
 * The backend webServer is booted (playwright.config.ts, live hook only)
 * with EDITORIAL_RUNTIME_CONFIG → {"mode":"heuristic_diagnostic"} so the
 * chain runs in diagnostic editorial mode with no credentials.
 */

test.describe("live real-chain episode", () => {
  test.skip(
    !process.env.LIVE_V44_E2E,
    "LIVE_V44_E2E=1 required — live real-chain E2E",
  );

  test.describe.configure({ mode: "serial" });

  const API_BASE = process.env.COCKPIT_API ?? "http://127.0.0.1:8765";
  // __dirname is cockpit/tests/e2e; the playwright config state root is
  // cockpit/.e2e/state (episode layout: <id>/{runner.log,previews/preview.mp4}).
  const EPISODES_ROOT = path.resolve(__dirname, "..", "..", ".e2e", "state", "episodes");
  const FIXTURE_CLIP = path.join(__dirname, "fixtures", "live-e2e-ja-speech.mp4");
  const REAL_STAGE_NAMES = [
    "intake",
    "ingest",
    "normalize",
    "analyze",
    "selection",
    "plan",
    "preview",
  ] as const;
  const CHAIN_TIMEOUT_MS = 600_000; // real analyzers included; ~10 min bound
  const REBUILD_TIMEOUT_MS = 300_000;

  let episodeId = "";

  function runnerLogPath(id: string): string {
    return path.join(EPISODES_ROOT, id, "runner.log");
  }

  function dumpRunnerLog(id: string, tailLines = 80): string {
    const logPath = runnerLogPath(id);
    if (!fs.existsSync(logPath)) return `<no runner.log at ${logPath}>`;
    const lines = fs.readFileSync(logPath, "utf8").split("\n").filter((l) => l !== "");
    return `<runner.log tail (${logPath})>\n${lines.slice(-tailLines).join("\n")}`;
  }

  type StageRun = {
    stage_name: string;
    status: string;
    retry_count: number;
    last_error_code: string | null;
  };

  type EpisodeStatusPayload = {
    episode_id: string;
    status: string;
    current_stage: string;
    stage_runs: StageRun[];
  };

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

  test("intake via UI → REAL stages → PREVIEW_READY → preview player", async ({
    page,
    request,
  }) => {
    test.setTimeout(CHAIN_TIMEOUT_MS + 120_000);
    const consoleErrors = trackConsoleErrors(page, [404]);

    // Fresh source folder per run holding the committed fixture clip
    // (unique folder → deterministic-but-unique episode id, no 409 on reruns).
    const sourceDir = fs.mkdtempSync(path.join(os.tmpdir(), "cockpit-live-src-"));
    fs.copyFileSync(FIXTURE_CLIP, path.join(sourceDir, "camera-001.mp4"));

    await page.goto("/");
    await expect(page).toHaveURL(/\/new-episode$/);
    await page.locator("#source-folder").fill(sourceDir);
    await page.locator("#brief-text").fill("live E2E: 実チェーンでプレビューまで");
    await page.getByTestId("create-button").click();
    await page.waitForURL(/\/episodes\/ep-[0-9a-f]+$/, { timeout: 20_000 });
    episodeId = page.url().split("/").pop() as string;

    // Bounded poll through the REAL stages; fail fast on a blocked stage
    // and dump runner.log on any failure (hung-command adversarial class).
    const deadline = Date.now() + CHAIN_TIMEOUT_MS;
    let last: EpisodeStatusPayload | null = null;
    for (;;) {
      const response = await request.get(`${API_BASE}/episodes/${episodeId}`);
      expect(response.ok(), `GET episode status failed: ${response.status()}`).toBeTruthy();
      last = (await response.json()) as EpisodeStatusPayload;
      const blocked = last.stage_runs.find(
        (run) => run.status === "failed_blocked",
      );
      if (blocked !== undefined) {
        throw new Error(
          `chain blocked at ${blocked.stage_name} (${blocked.last_error_code})\n${dumpRunnerLog(episodeId)}`,
        );
      }
      if (last.status === "PREVIEW_READY") break;
      if (Date.now() > deadline) {
        throw new Error(
          `chain did not reach PREVIEW_READY within ${CHAIN_TIMEOUT_MS}ms (last: ${last.status}/${last.current_stage})\n${dumpRunnerLog(episodeId)}`,
        );
      }
      await page.waitForTimeout(2_000);
    }

    // Honest evidence, not just the status string: real stage NAMES
    // succeeded, the preview FILE exists, and the player renders.
    const succeeded = new Set(
      last.stage_runs.filter((run) => run.status === "succeeded").map((run) => run.stage_name),
    );
    for (const stage of REAL_STAGE_NAMES) {
      expect(
        succeeded.has(stage),
        `stage ${stage} never succeeded (runs: ${JSON.stringify(last.stage_runs)})`,
      ).toBe(true);
    }
    const previewFile = path.join(EPISODES_ROOT, episodeId, "previews", "preview.mp4");
    expect(fs.existsSync(previewFile), `preview file missing: ${previewFile}`).toBe(true);

    // The page's own poll converges on the same truth.
    await expect(page.getByTestId("episode-status")).toHaveText("PREVIEW_READY", {
      timeout: 15_000,
    });
    await expect(page.getByTestId("preview-player")).toBeVisible({ timeout: 15_000 });

    expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
  });

  test("NL修正（明示タイムスタンプ）→ 適用 → 部分rebuild → 更新プレビュー", async ({
    page,
  }) => {
    test.setTimeout(REBUILD_TIMEOUT_MS + 120_000);
    const consoleErrors = trackConsoleErrors(page, [404]);

    await page.goto(`/episodes/${episodeId}`);
    await expect(page.getByTestId("episode-status")).toHaveText("PREVIEW_READY", {
      timeout: 15_000,
    });
    await expect(page.getByTestId("preview-player")).toBeVisible({ timeout: 15_000 });

    // Regex path (deterministic interpreter): explicit 0:02 timestamp +
    // remove_section phrasing → fully determined draft, no LLM involved.
    await page.getByLabel("修正指示（自然言語）").fill("0:02の区間を削除して");
    await page.getByTestId("review-chat-send").click();

    await expect(page.getByTestId("review-draft")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("review-draft-kind")).toHaveText("区間を削除");
    await expect(page.getByTestId("review-draft-target")).toHaveText("2s");
    await expect(page.getByTestId("review-draft-needs-confirmation")).toHaveCount(0);

    await page.getByTestId("review-apply-button").click();

    // T9 indicator states: 予約済み → 実行中 → 完了 (poll-driven).
    await expect(page.getByTestId("rebuild-indicator")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId("rebuild-phase")).toHaveText(
      /再build(予約済み|実行中|完了)/,
      { timeout: 20_000 },
    );
    await expect(page.getByTestId("rebuild-phase")).toHaveText("再build完了", {
      timeout: REBUILD_TIMEOUT_MS,
    });

    // Updated preview: the player is still served from a (re-rendered)
    // preview file and the job is back at PREVIEW_READY.
    const previewFile = path.join(EPISODES_ROOT, episodeId, "previews", "preview.mp4");
    expect(fs.existsSync(previewFile), `preview file missing after rebuild`).toBe(true);
    const metricsFile = path.join(EPISODES_ROOT, episodeId, "rebuild-metrics.jsonl");
    expect(fs.existsSync(metricsFile), `rebuild metrics missing: ${metricsFile}`).toBe(true);
    const metricLines = fs
      .readFileSync(metricsFile, "utf8")
      .split("\n")
      .filter((line) => line !== "");
    expect(metricLines.length).toBeGreaterThanOrEqual(1);

    await expect(page.getByTestId("episode-status")).toHaveText("PREVIEW_READY", {
      timeout: 15_000,
    });
    await expect(page.getByTestId("preview-player")).toBeVisible({ timeout: 15_000 });

    expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
  });
});
