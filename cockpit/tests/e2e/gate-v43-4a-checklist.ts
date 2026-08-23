import fs from "node:fs";
import path from "node:path";

/**
 * Gate V43-4a UX machine checklist (PRD 4.3 §Gate V43-4, lines 1637-1647).
 *
 * Each PRD UX bullet maps to one checklist item; acceptance.spec.ts
 * records pass/fail FROM THE ACTUAL DRIVEN FLOW, and the final test
 * writes the artifact to .omo/evidence/v43-rebaseline/task-51/. An
 * unrecorded or failed item fails the writer test — the checklist can
 * never claim more than the e2e proved.
 */

export type ChecklistStatus = "pass" | "fail";

export type ChecklistItem = {
  id: string;
  prd_ref: string;
  title: string;
  status: ChecklistStatus;
  evidence: string;
};

type ItemSpec = {
  id: string;
  prd_ref: string;
  title: string;
};

export const CHECKLIST_SPECS: readonly ItemSpec[] = [
  {
    id: "start-from-source-and-brief",
    prd_ref: "PRD_v4.3.md:1639",
    title: "新しいエピソードをソース選択+自然言語brief(+任意参照)で開始できる",
  },
  {
    id: "cockpit-only-completion",
    prd_ref: "PRD_v4.3.md:1640",
    title: "通常のCLI/JSON使用なしでEpisode Cockpitだけで完結できる",
  },
  {
    id: "timestamp-review-jump",
    prd_ref: "PRD_v4.3.md:1641",
    title: "timestamp review jumpが動作する",
  },
  {
    id: "nl-correction-structured-partial-rebuild",
    prd_ref: "PRD_v4.3.md:1642",
    title: "自然言語修正→構造化コマンド→部分rebuildが動作する",
  },
  {
    id: "transient-restart-resumes",
    prd_ref: "PRD_v4.3.md:1643",
    title: "一時的な再起動から復帰する",
  },
  {
    id: "blocking-sessions-le2",
    prd_ref: "PRD_v4.3.md:1644",
    title: "公開前の通常ブロックレビューセッションが2以下である",
  },
  {
    id: "interruption-policy-no-avoidable-prompts",
    prd_ref: "PRD_v4.3.md:1645",
    title: "fallback/retryがInterruption Policyに従い回避可能なプロンプトを出さない",
  },
  {
    id: "publishability-lightweight-feedback",
    prd_ref: "PRD_v4.3.md:1646",
    title: "軽量なPublishability Reviewフィードバックを数値スコアカードなしで記録できる",
  },
  {
    id: "direct-resolve-recorded-as-manual",
    prd_ref: "PRD_v4.3.md:1647",
    title: "直接Resolve操作が隠されずManual Finalizationとして記録される",
  },
  {
    id: "no-cli-json-or-direct-resolve-required",
    prd_ref: "task-51 operational record",
    title: "オペレータフロー全体でCLI/JSON/直接Resolveが不要であった",
  },
] as const;

const recorded = new Map<string, ChecklistItem>();

export function record(
  id: string,
  status: ChecklistStatus,
  evidence: string,
): void {
  const spec = CHECKLIST_SPECS.find((item) => item.id === id);
  if (spec === undefined) {
    throw new Error(`unknown checklist item: ${id}`);
  }
  recorded.set(id, { ...spec, status, evidence });
}

export function buildChecklist(): {
  schema_version: string;
  generated_at: string;
  source: string;
  items: ChecklistItem[];
  summary: { total: number; pass: number; fail: number };
} {
  const items = CHECKLIST_SPECS.map((spec) => {
    const item = recorded.get(spec.id);
    if (item === undefined) {
      throw new Error(`checklist item never recorded by the e2e flow: ${spec.id}`);
    }
    return item;
  });
  return {
    schema_version: "gate-v43-4a-checklist-v1",
    generated_at: new Date().toISOString(),
    source: "cockpit acceptance e2e (tests/e2e/acceptance.spec.ts)",
    items,
    summary: {
      total: items.length,
      pass: items.filter((item) => item.status === "pass").length,
      fail: items.filter((item) => item.status === "fail").length,
    },
  };
}

export function writeChecklist(repoRoot: string): string {
  const checklist = buildChecklist();
  const outDir = path.join(repoRoot, ".omo", "evidence", "v43-rebaseline", "task-51");
  fs.mkdirSync(outDir, { recursive: true });
  const outPath = path.join(outDir, "gate-v43-4a-checklist.json");
  fs.writeFileSync(outPath, `${JSON.stringify(checklist, null, 2)}\n`, "utf8");
  return outPath;
}
