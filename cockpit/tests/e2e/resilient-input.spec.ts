import { execFileSync } from "node:child_process";
import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const STATE_ROOT = path.resolve(process.cwd(), ".e2e", "state");
const EPISODES_ROOT = path.join(STATE_ROOT, "episodes");
const STATE_STORE = path.join(STATE_ROOT, "state.db");
const VIDEO_PIPELINE_DIR = path.resolve(process.cwd(), "..", "video-pipeline");

/**
 * U31 resilient-input coverage (工程6 prep): narrow viewport + keyboard-only
 * operation. Honest scope — pins what the UI actually supports today. Any
 * genuine breakage is a FAILING fact for 工程6 to repair (product code is
 * not touched here, assertions are not weakened to pass).
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

async function overflowingElements(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const viewport = window.innerWidth;
    const offenders: string[] = [];
    for (const element of Array.from(document.querySelectorAll("*"))) {
      const rect = element.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) continue;
      if (rect.right > viewport + 1 || rect.left < -1) {
        const html = element as HTMLElement;
        offenders.push(
          `${element.tagName.toLowerCase()}` +
            `#${html.id || "-"}` +
            `[data-testid=${html.getAttribute("data-testid") ?? "-"}]` +
            `(right=${Math.round(rect.right)})`,
        );
        if (offenders.length >= 10) break;
      }
    }
    return offenders;
  });
}

async function assertNoHorizontalOverflow(page: Page): Promise<void> {
  const scrollWidth = await page.evaluate(
    () => document.documentElement.scrollWidth,
  );
  const viewport = await page.evaluate(() => window.innerWidth);
  expect(
    scrollWidth,
    `page scrolls horizontally (scrollWidth=${scrollWidth}, viewport=${viewport})`,
  ).toBeLessThanOrEqual(viewport + 1);
  const offenders = await overflowingElements(page);
  expect(offenders, "elements overflowing the narrow viewport").toEqual([]);
}

/** Tab from the top of the page, naming each focused stop. */
async function collectTabStops(page: Page, maxTabs: number): Promise<string[]> {
  await page.evaluate(() => {
    (document.activeElement as HTMLElement | null)?.blur?.();
  });
  const stops: string[] = [];
  for (let i = 0; i < maxTabs; i++) {
    await page.keyboard.press("Tab");
    const name = await page.evaluate(() => {
      const element = document.activeElement;
      if (element === null || element === document.body) return "body";
      const html = element as HTMLElement;
      return (
        html.getAttribute("data-testid") ||
        html.id ||
        html.getAttribute("aria-label") ||
        element.tagName.toLowerCase()
      );
    });
    stops.push(name);
  }
  return stops;
}

function assertSubsequence(stops: string[], wanted: string[]): void {
  let cursor = -1;
  for (const name of wanted) {
    const next = stops.indexOf(name, cursor + 1);
    expect(next, `Tab order must reach ${name} (stops: ${stops.join(",")})`).toBeGreaterThanOrEqual(0);
    cursor = next;
  }
}

// Backstage consultation seed (same REAL-store shape as acceptance.spec.ts:
// journal facts only; the judgment itself goes through the UI form).
const SEED_CONSULTATION = `
import sys
from pathlib import Path

from services.episode_cockpit.consultation_store import (
    ConsultationBudgetEventV1,
    ConsultationProposalDetails,
    ConsultationProposalSetV1,
    ConsultationProposalV1,
    ConsultationRecordV1,
    append_budget_event,
    append_consultation,
    append_proposal_set,
    now_stamp,
)

base = Path(sys.argv[1])

def seed(consultation_id: str, message: str, title: str) -> None:
    stamp = now_stamp()
    append_consultation(
        base,
        ConsultationRecordV1(
            consultation_id=consultation_id, created_at=stamp, message=message
        ),
    )
    append_proposal_set(
        base,
        ConsultationProposalSetV1(
            consultation_id=consultation_id,
            created_at=stamp,
            proposals=(
                ConsultationProposalV1(
                    proposal_id="prop-1",
                    title=title,
                    summary="e2e用のお試し提案",
                    details=ConsultationProposalDetails(
                        audience_message="視聴者に工夫を伝える",
                        structure="引き → Before → 改造 → After → まとめ",
                        duration_estimate="45秒前後",
                        candidate_scenes=("冒頭の引き",),
                        subtitle_policy="短く区切って読みやすく",
                        audio_policy="いつもの選曲",
                        tempo_policy="前半は速め、まとめはゆっくり",
                        reference_mapping="いつもの冒頭構成を踏襲する",
                        unused_reasons="主題から離れるカットは使いません",
                        unconfirmed=("Afterの撮影状況は未確認",),
                    ),
                ),
            ),
        ),
    )
    append_budget_event(
        base,
        ConsultationBudgetEventV1(
            consultation_id=consultation_id,
            llm_calls=0,
            intervals=0,
            wall_seconds=0.0,
            created_at=stamp,
        ),
    )

seed("c-kbd-1", "e2e: キーボード操作の相談", "キーボード候補の構成案")
print("consultation-seeded")
`;

const SET_JOB_STATE = `
import sqlite3
import sys

episode_id = sys.argv[1]
state_store = sys.argv[2]
status = sys.argv[3]
stage = sys.argv[4]
connection = sqlite3.connect(state_store)
connection.execute(
    "UPDATE jobs SET status = ?, current_stage = ? WHERE job_id = ?",
    (status, stage, episode_id),
)
connection.commit()
connection.close()
print("job-state-set")
`;

test("narrow viewport (375x667): intake form renders without horizontal overflow", async ({
  page,
}) => {
  test.setTimeout(60_000);
  const consoleErrors = trackConsoleErrors(page, [404]);

  await page.setViewportSize({ width: 375, height: 667 });
  await page.goto("/");
  await expect(page).toHaveURL(/\/new-episode$/);

  await expect(page.getByLabel("ソースフォルダ")).toBeVisible();
  await expect(page.getByLabel("この動画は何について？")).toBeVisible();
  await expect(page.getByTestId("create-button")).toBeVisible();

  await assertNoHorizontalOverflow(page);

  expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
});

test("keyboard-only: intake Tab order reaches inputs and Enter submits", async ({
  page,
}) => {
  test.setTimeout(90_000);
  const consoleErrors = trackConsoleErrors(page, [404]);

  await page.goto("/");
  await expect(page).toHaveURL(/\/new-episode$/);
  await expect(page.getByTestId("create-button")).toBeDisabled();

  // Empty form: the create button is disabled, and disabled buttons are
  // skipped in the Tab order (platform behavior, not a bug). The inputs
  // themselves must still be Tab-reachable; the disabled skip is recorded
  // here as an honest fact, not asserted away.
  const emptyStops = await collectTabStops(page, 25);
  assertSubsequence(emptyStops, ["source-folder", "brief-text"]);
  expect(
    emptyStops,
    "disabled create button is skipped in the Tab order (honest gate fact)",
  ).not.toContain("create-button");

  // Keyboard-only fill: Tab back to the source input, type, Tab on, type.
  await page.evaluate(() => {
    (document.activeElement as HTMLElement | null)?.blur?.();
  });
  for (let i = 0; i < 25; i++) {
    const focused = await page.evaluate(
      () =>
        (document.activeElement as HTMLElement | null)?.id ??
        document.activeElement?.tagName,
    );
    if (focused === "source-folder") break;
    await page.keyboard.press("Tab");
  }
  const sourceDir = fs.mkdtempSync(path.join(os.tmpdir(), "cockpit-kbd-src-"));
  await page.keyboard.type(sourceDir);
  await expect(page.getByLabel("ソースフォルダ")).toHaveValue(sourceDir);

  await page.keyboard.press("Tab");
  const afterSourceTab = await page.evaluate(
    () => document.activeElement?.id,
  );
  expect(afterSourceTab).toBe("brief-text");
  await page.keyboard.type("キーボードのみでの作成テスト");
  await expect(page.getByTestId("create-button")).toBeEnabled();

  // Filled form: the enabled create button joins the Tab order.
  const filledStops = await collectTabStops(page, 25);
  assertSubsequence(filledStops, ["create-button"]);

  // Tab to the create button and submit with Enter (no mouse).
  for (let i = 0; i < 25; i++) {
    const focused = await page.evaluate(
      () =>
        (document.activeElement as HTMLElement | null)?.getAttribute(
          "data-testid",
        ) ?? (document.activeElement as HTMLElement | null)?.id,
    );
    if (focused === "create-button") break;
    await page.keyboard.press("Tab");
  }
  await page.keyboard.press("Enter");
  await page.waitForURL(/\/episodes\/ep-[0-9a-f]+$/, { timeout: 20_000 });

  await expect(page.getByTestId("episode-status")).toHaveText("CREATED");

  expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
});

test("narrow viewport (375x667): episode page renders without horizontal overflow", async ({
  page,
}) => {
  test.setTimeout(90_000);
  const consoleErrors = trackConsoleErrors(page, [404]);

  await page.goto("/");
  const sourceDir = fs.mkdtempSync(path.join(os.tmpdir(), "cockpit-narrow-src-"));
  await page.getByLabel("この動画は何について？").fill("狭い画面の確認");
  await page.getByLabel("ソースフォルダ").fill(sourceDir);
  await page.getByTestId("create-button").click();
  await page.waitForURL(/\/episodes\/ep-[0-9a-f]+$/, { timeout: 20_000 });
  const episodeId = page.url().split("/").pop() as string;

  await page.setViewportSize({ width: 375, height: 667 });
  await page.goto(`/episodes/${episodeId}`);
  await expect(page.getByTestId("episode-status")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("current-stage")).toBeVisible();

  await assertNoHorizontalOverflow(page);

  expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
});

test("keyboard-only: consultation judgment reachable and submittable by keyboard", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const consoleErrors = trackConsoleErrors(page, [404]);

  await page.goto("/");
  const sourceDir = fs.mkdtempSync(path.join(os.tmpdir(), "cockpit-kbd-cons-"));
  await page.getByLabel("この動画は何について？").fill("相談のキーボード確認");
  await page.getByLabel("ソースフォルダ").fill(sourceDir);
  await page.getByTestId("create-button").click();
  await page.waitForURL(/\/episodes\/ep-[0-9a-f]+$/, { timeout: 20_000 });
  const episodeId = page.url().split("/").pop() as string;

  // Backstage (harness-only): park the stage where the panel mounts and
  // plant one journal consultation; the judgment goes through the UI form.
  execFileSync(
    "uv",
    ["run", "python", "-c", SET_JOB_STATE, episodeId, STATE_STORE, "PREVIEW_READY", "selection"],
    { cwd: VIDEO_PIPELINE_DIR, stdio: "ignore" },
  );
  execFileSync(
    "uv",
    ["run", "python", "-c", SEED_CONSULTATION, path.join(EPISODES_ROOT, episodeId)],
    { cwd: VIDEO_PIPELINE_DIR, stdio: "ignore" },
  );

  await page.goto(`/episodes/${episodeId}`);
  await expect(page.getByTestId("consultation-panel")).toBeVisible({ timeout: 15_000 });
  const entry = page.locator(
    '[data-testid="consultation-entry"][data-consultation-id="c-kbd-1"]',
  );
  await expect(entry.getByTestId("consultation-entry-message")).toContainText(
    "e2e: キーボード操作の相談",
  );

  // Submit starts disabled (判断しないことは同意にはならない) — a disabled
  // button is skipped in the Tab order, so the decision buttons must come
  // first by keyboard, then the submit joins the order once enabled.
  await expect(entry.getByTestId("consultation-judgment-submit")).toBeDisabled();
  const initialStops = await collectTabStops(page, 80);
  assertSubsequence(initialStops, [
    "consultation-judgment-adopt",
    "consultation-judgment-reject",
  ]);
  expect(
    initialStops,
    "disabled judgment submit is skipped in the Tab order (honest gate fact)",
  ).not.toContain("consultation-judgment-submit");

  // Keyboard-only judgment: Tab to 見送る, choose with Enter, Tab to送信, Enter.
  await page.evaluate(() => {
    (document.activeElement as HTMLElement | null)?.blur?.();
  });
  for (let i = 0; i < 80; i++) {
    const focused = await page.evaluate(
      () =>
        (document.activeElement as HTMLElement | null)?.getAttribute("data-testid"),
    );
    if (focused === "consultation-judgment-reject") break;
    await page.keyboard.press("Tab");
  }
  await page.keyboard.press("Enter");
  await expect(entry.getByTestId("consultation-judgment-submit")).toBeEnabled();

  // Enabled submit joins the Tab order — reachable by keyboard now.
  const enabledStops = await collectTabStops(page, 80);
  assertSubsequence(enabledStops, ["consultation-judgment-submit"]);

  for (let i = 0; i < 80; i++) {
    const focused = await page.evaluate(
      () =>
        (document.activeElement as HTMLElement | null)?.getAttribute("data-testid"),
    );
    if (focused === "consultation-judgment-submit") break;
    await page.keyboard.press("Tab");
  }
  await page.keyboard.press("Enter");

  await expect(page.getByTestId("consultation-announcement")).toHaveText(
    "判断を記録しました",
    { timeout: 15_000 },
  );
  await expect(entry.getByTestId("consultation-judgment-recorded")).toContainText(
    "見送る",
  );

  expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
});
