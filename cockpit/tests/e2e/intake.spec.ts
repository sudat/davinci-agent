import { expect, test } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const API_BASE = process.env.COCKPIT_API ?? "http://127.0.0.1:8765";

/**
 * Watch the browser console for real errors (React warnings, fetch
 * failures, hydration errors). Any console.error or pageerror fails.
 * Chromium auto-logs "Failed to load resource" for HTTP error responses;
 * statuses in `allowedResourceStatuses` are expected (this test triggers
 * them deliberately) and are therefore not app errors.
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

test("intake → Create → episode status view (backend receives POST)", async ({
  page,
  request,
}) => {
  // 404 allow-list: the episode view (task 46) probes
  // GET /episodes/{id}/preview while polling — expected 404s until the
  // preview is generated; Chromium logs every failed resource load.
  const consoleErrors = trackConsoleErrors(page, [404]);

  await page.goto("/");
  await expect(page).toHaveURL(/\/new-episode$/);

  const createButton = page.getByTestId("create-button");
  await expect(createButton).toBeDisabled();

  const brief = page.getByLabel("どんな動画にしたいですか？");
  await brief.fill("e2eテスト: コックピットintakeからのbrief");
  await expect(createButton).toBeDisabled();

  const source = page.getByLabel("撮影素材のフォルダを選ぶ");
  await source.fill("   ");
  await expect(createButton).toBeDisabled();

  // Unique existing folder per run → deterministic episode id, no 409 on reruns.
  const sourceDir = fs.mkdtempSync(path.join(os.tmpdir(), "cockpit-e2e-src-"));
  await source.fill(sourceDir);
  await expect(createButton).toBeEnabled();

  await page.screenshot({
    path: path.join("test-results", "intake.png"),
    fullPage: true,
  });

  await createButton.click();
  await page.waitForURL(/\/episodes\/ep-[0-9a-f]+$/, { timeout: 20_000 });

  await expect(page.getByTestId("episode-status")).toHaveText("CREATED");
  await expect(page.getByTestId("current-stage")).toHaveText("intake");

  // The backend really received the POST: verify via the API directly.
  const episodeId = page.url().split("/").pop() as string;
  const response = await request.get(`${API_BASE}/episodes/${episodeId}`);
  expect(response.ok()).toBeTruthy();
  const body = (await response.json()) as {
    episode_id: string;
    current_stage: string;
    status: string;
  };
  expect(body.episode_id).toBe(episodeId);
  expect(body.current_stage).toBe("intake");
  expect(body.status).toBe("CREATED");

  // Stale-state probe: the poll keeps reflecting the created episode.
  await page.waitForTimeout(2500);
  await expect(page.getByTestId("episode-status")).toHaveText("CREATED");
  await expect(page.getByTestId("current-stage")).toHaveText("intake");

  expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
});

test("empty source keeps Create disabled (malformed input)", async ({ page }) => {
  const consoleErrors = trackConsoleErrors(page);

  await page.goto("/new-episode");
  const createButton = page.getByTestId("create-button");
  await expect(createButton).toBeDisabled();

  await page.getByLabel("どんな動画にしたいですか？").fill("briefだけある状態");
  await expect(createButton).toBeDisabled();

  await page.getByLabel("撮影素材のフォルダを選ぶ").fill(" ");
  await expect(createButton).toBeDisabled();

  // 高度な設定は初期折りたたみ
  const advanced = page.getByTestId("advanced-section");
  await expect(advanced).toHaveJSProperty("open", false);

  expect(consoleErrors).toEqual([]);
});

test("backend structured error is displayed on the intake form", async ({ page }) => {
  const consoleErrors = trackConsoleErrors(page, [422]);

  await page.goto("/new-episode");
  await page
    .getByLabel("撮影素材のフォルダを選ぶ")
    .fill(`/nonexistent/cockpit-e2e-${Date.now()}`);
  await page.getByLabel("どんな動画にしたいですか？").fill("存在しないフォルダでの検証");
  await page.getByTestId("create-button").click();

  const alert = page.getByTestId("error-notice");
  await expect(alert).toBeVisible();
  await expect(alert).toContainText("source-folder-not-found");
  await expect(page).toHaveURL(/\/new-episode$/);

  expect(consoleErrors).toEqual([]);
});
