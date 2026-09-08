import { describe, expect, it } from "vitest";
import {
  derivePreviewBinding,
  previewAllowsCompletion,
  previewBindingLine,
  type PreviewProbeObservation,
} from "@/lib/previewBinding";
import type { EpisodePreviewProbe, EpisodeStatus } from "@/lib/episode-api";

/** previewBinding純粋導出の固定順契約（不明は不明・推測しない）:
 *  - 文字列一致のみ・順序推測なし
 *  - 材料が1つでも欠ければ unknown
 *  - probe失敗は not_generated に偽装しない */

const probeBase: EpisodePreviewProbe = {
  available: true,
  run_id: "run-1",
  target_version: "v2",
  content_hash: "hash-1",
  output_arrived_at: "2026-09-09T00:00:00+00:00",
};

const probed = (overrides: Partial<EpisodePreviewProbe>): PreviewProbeObservation => ({
  kind: "probed",
  probe: { ...probeBase, ...overrides },
});

const failed: PreviewProbeObservation = { kind: "failed" };

const statusBase: EpisodeStatus = {
  episode_id: "ep-1",
  job_id: "ep-1",
  status: "PREVIEW_READY",
  current_stage: "preview",
  created_at_seq: 1,
  updated_at_seq: 1,
  stage_runs: [],
  current_run: "run-1",
  current_target_version: "v2",
};

const statusOf = (overrides: Partial<EpisodeStatus>): EpisodeStatus => ({
  ...statusBase,
  ...overrides,
});

describe("derivePreviewBinding（固定順）", () => {
  it("probe か status が null → checking（片方だけでも）", () => {
    expect(derivePreviewBinding(null, null)).toEqual({ kind: "checking" });
    expect(derivePreviewBinding(probed({}), null)).toEqual({ kind: "checking" });
    expect(derivePreviewBinding(null, statusOf({}))).toEqual({ kind: "checking" });
  });

  it("probe失敗 → probe_failed（not_generated に偽装しない）", () => {
    expect(derivePreviewBinding(failed, statusOf({}))).toEqual({ kind: "probe_failed" });
  });

  it("available=false → not_generated", () => {
    expect(derivePreviewBinding(probed({ available: false }), statusOf({}))).toEqual({
      kind: "not_generated",
    });
  });

  it("run一致・版一致 → current", () => {
    expect(derivePreviewBinding(probed({}), statusOf({}))).toEqual({
      kind: "current",
      targetVersion: "v2",
    });
  });

  it("版は同じでrun違い → stale（run_only）。版の新旧は推測しない", () => {
    expect(
      derivePreviewBinding(probed({ run_id: "run-0" }), statusOf({})),
    ).toEqual({
      kind: "stale",
      reason: { case: "run_only" },
    });
  });

  it("runは同じで版違い → stale（version_changed・両方の版を名乗る）", () => {
    expect(
      derivePreviewBinding(probed({ target_version: "v1" }), statusOf({})),
    ).toEqual({
      kind: "stale",
      reason: { case: "version_changed", probeVersion: "v1", currentVersion: "v2" },
    });
  });

  it("runも版も違う → stale（version_changedが表示の根拠）", () => {
    expect(
      derivePreviewBinding(
        probed({ run_id: "run-0", target_version: "v1" }),
        statusOf({}),
      ),
    ).toEqual({
      kind: "stale",
      reason: { case: "version_changed", probeVersion: "v1", currentVersion: "v2" },
    });
  });

  it("材料が1つでも欠ければ unknown（probe側: run_id / target_version / content_hash）", () => {
    expect(derivePreviewBinding(probed({ run_id: null }), statusOf({}))).toEqual({
      kind: "unknown",
    });
    expect(derivePreviewBinding(probed({ target_version: null }), statusOf({}))).toEqual({
      kind: "unknown",
    });
    expect(derivePreviewBinding(probed({ content_hash: null }), statusOf({}))).toEqual({
      kind: "unknown",
    });
  });

  it("材料が1つでも欠ければ unknown（status側: current_run / current_target_version、欠如も同値）", () => {
    expect(derivePreviewBinding(probed({}), statusOf({ current_run: null }))).toEqual({
      kind: "unknown",
    });
    expect(
      derivePreviewBinding(probed({}), statusOf({ current_target_version: null })),
    ).toEqual({ kind: "unknown" });
    const legacy: EpisodeStatus = { ...statusBase };
    delete legacy.current_run;
    delete legacy.current_target_version;
    expect(derivePreviewBinding(probed({}), legacy)).toEqual({ kind: "unknown" });
  });

  it("output_arrived_at だけ欠けても unknown にはしない（binding材料に数えない）", () => {
    expect(derivePreviewBinding(probed({ output_arrived_at: null }), statusOf({}))).toEqual(
      { kind: "current", targetVersion: "v2" },
    );
  });
});

describe("previewAllowsCompletion（previewOk三値）", () => {
  it("current のときだけ true。stale / not_generated は false。checking / unknown / probe_failed は null", () => {
    expect(previewAllowsCompletion({ kind: "current", targetVersion: "v2" })).toBe(true);
    expect(
      previewAllowsCompletion({
        kind: "stale",
        reason: { case: "version_changed", probeVersion: "v1", currentVersion: "v2" },
      }),
    ).toBe(false);
    expect(previewAllowsCompletion({ kind: "stale", reason: { case: "run_only" } })).toBe(
      false,
    );
    expect(previewAllowsCompletion({ kind: "not_generated" })).toBe(false);
    expect(previewAllowsCompletion({ kind: "checking" })).toBeNull();
    expect(previewAllowsCompletion({ kind: "unknown" })).toBeNull();
    expect(previewAllowsCompletion({ kind: "probe_failed" })).toBeNull();
  });
});

describe("previewBindingLine（表示文）", () => {
  it("exact strings", () => {
    expect(previewBindingLine({ kind: "checking" })).toBe(
      "今回の実行との対応を確認しています…",
    );
    expect(previewBindingLine({ kind: "probe_failed" })).toBe(
      "確認できません（試し編集の情報を取得できません）",
    );
    expect(previewBindingLine({ kind: "not_generated" })).toBe(
      "試し編集はまだ生成されていません",
    );
    expect(previewBindingLine({ kind: "unknown" })).toBe(
      "不明（対象となる実行を特定できません）",
    );
    expect(previewBindingLine({ kind: "current", targetVersion: "v7" })).toBe(
      "今回の実行の試し編集です（対象版 v7）",
    );
    expect(
      previewBindingLine({
        kind: "stale",
        reason: { case: "version_changed", probeVersion: "v6", currentVersion: "v7" },
      }),
    ).toBe("前回の実行のものです（再生成待ち。表示中 v6／今回 v7）");
    expect(previewBindingLine({ kind: "stale", reason: { case: "run_only" } })).toBe(
      "前回の実行のものです（今回の実行分を再生成待ち）",
    );
  });
});
