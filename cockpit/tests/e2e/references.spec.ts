import { expect, test } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const API_BASE = process.env.COCKPIT_API ?? "http://127.0.0.1:8765";
// Spec __dirname resolves to tests/ (not tests/e2e/) under the Playwright
// transform, so anchor on the run cwd — always the cockpit/ config root.
const STATE_ROOT = path.resolve(process.cwd(), ".e2e", "state");

/**
 * Same console discipline as intake.spec.ts. Allowed statuses: 404 — a fresh
 * episode has no preview render yet, so the (task-46) availability HEAD
 * probe legitimately observes 404; it is an expected backend state, not an
 * app error.
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

async function createEpisode(request: import("@playwright/test").APIRequestContext) {
  const sourceDir = fs.mkdtempSync(path.join(os.tmpdir(), "cockpit-ref-e2e-src-"));
  const response = await request.post(`${API_BASE}/episodes`, {
    data: { source_folder: sourceDir, brief_text: "reference annotation e2e" },
  });
  expect(response.ok()).toBeTruthy();
  const body = (await response.json()) as { episode_id: string };
  return body.episode_id;
}

test("注釈 → 抽出プレビュー → 修正 → 保存 → 一覧表示（バックエンド永続化）", async ({
  page,
  request,
}) => {
  test.setTimeout(120_000);
  const consoleErrors = trackConsoleErrors(page, [404]);

  const episodeId = await createEpisode(request);
  const runKey = `${Date.now()}-${process.pid}`;
  const refFile = path.join(os.tmpdir(), `cockpit-ref-e2e-${runKey}.mp4`);
  // source_id is CONTENT-derived (ref-<sha16>): identical bytes across runs
  // would collide in the shared library (duplicate_source → 500), so the
  // payload itself must be unique per run.
  fs.writeFileSync(refFile, `reference-e2e-bytes-${runKey}`);

  await page.goto(`/episodes/${episodeId}`);
  await expect(page.getByTestId("reference-annotator")).toBeVisible();
  await expect(page.getByTestId("pairwise-prompt")).toHaveCount(0);

  await page
    .getByLabel("参照動画のパス（ローカルファイル）")
    .fill(refFile);
  await page
    .getByLabel("注釈コメント（日本語で自由記述）")
    .fill("色が良い");

  const parseButton = page.getByTestId("parse-preview-button");
  await expect(parseButton).toBeEnabled();
  await parseButton.click();

  const chip = page.getByTestId("domain-chip-color");
  await expect(chip).toBeVisible();
  await expect(chip).toHaveClass(/chip-like/);

  await page.getByTestId("polarity-select-color").selectOption("dislike");
  await expect(chip).toHaveClass(/chip-dislike/);

  await page.getByTestId("save-annotation-button").click();
  const savedItem = page.getByTestId("saved-annotation-item");
  await expect(savedItem).toBeVisible();
  await expect(savedItem).toContainText("色: 嫌い");
  await expect(savedItem).toContainText("修正済み");

  await page.goto("/references");
  const annotationItem = page.getByTestId("annotation-item");
  await expect(annotationItem).toBeVisible();
  await expect(annotationItem).toContainText("色: 嫌い");
  await expect(annotationItem).toContainText("ref-");

  // stale_state probe: the annotation's reference really persisted in the
  // backend library — a SECOND registration must see version advance and
  // the on-disk library must hold both sources.
  const secondRef = `${refFile}.b.mp4`;
  fs.writeFileSync(secondRef, `reference-e2e-bytes-b-${runKey}`);
  await page.getByLabel("参照ファイルパス").fill(secondRef);
  await page.getByTestId("register-reference-button").click();
  await expect(page.getByTestId("reference-item")).toContainText(/ライブラリ版 [0-9]+/);

  const libraryPath = path.join(STATE_ROOT, "episodes", "reference-library.json");
  const library = JSON.parse(fs.readFileSync(libraryPath, "utf8")) as {
    version: number;
    sources: Array<{ source_id: string; location: string }>;
  };
  expect(library.version).toBeGreaterThanOrEqual(3);
  const locations = library.sources.map((source) => source.location);
  expect(locations).toContain(refFile);
  expect(locations).toContain(secondRef);

  await page.screenshot({
    path: path.join("test-results", "references.png"),
    fullPage: true,
  });

  expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
});

test("A/B比較は無差別に出ない: 読み込み時なし・トリガー/曖昧さヒントでのみ出現", async ({
  page,
  request,
}) => {
  test.setTimeout(120_000);
  const consoleErrors = trackConsoleErrors(page, [404]);

  const episodeId = await createEpisode(request);

  // 曖昧さヒント経路: 通常読み込みでは出ず、needs_review な抽出結果でのみ出る
  await page.goto(`/episodes/${episodeId}`);
  await expect(page.getByTestId("reference-annotator")).toBeVisible();
  await expect(page.getByTestId("pairwise-prompt")).toHaveCount(0);

  await page
    .getByLabel("注釈コメント（日本語で自由記述）")
    .fill("全部良い");
  await page.getByTestId("parse-preview-button").click();
  await expect(page.getByTestId("ambiguity-hint")).toBeVisible();
  await expect(page.getByTestId("pairwise-prompt")).toBeVisible();

  // 再読み込みすればやはり出ない（無断ポップアップではない）
  await page.reload();
  await expect(page.getByTestId("reference-annotator")).toBeVisible();
  await expect(page.getByTestId("pairwise-prompt")).toHaveCount(0);

  // 明示トリガー経路
  await page.getByTestId("pairwise-trigger").click();
  await expect(page.getByTestId("pairwise-prompt")).toBeVisible();
  await expect(page.getByTestId("pairwise-insufficient")).toBeVisible();

  // 数値スコアカードは存在しない
  await expect(page.locator('input[type="range"]')).toHaveCount(0);

  expect(consoleErrors).toEqual([]);
});

test("空入力はボタン無効（malformed input）", async ({ page, request }) => {
  test.setTimeout(120_000);
  const consoleErrors = trackConsoleErrors(page, [404]);

  const episodeId = await createEpisode(request);
  await page.goto(`/episodes/${episodeId}`);
  await expect(page.getByTestId("reference-annotator")).toBeVisible();

  await expect(page.getByTestId("parse-preview-button")).toBeDisabled();
  await expect(page.getByTestId("save-annotation-button")).toBeDisabled();

  await page
    .getByLabel("注釈コメント（日本語で自由記述）")
    .fill("   ");
  await expect(page.getByTestId("parse-preview-button")).toBeDisabled();

  await page.goto("/references");
  await expect(page.getByTestId("register-reference-button")).toBeDisabled();

  expect(consoleErrors).toEqual([]);
});
