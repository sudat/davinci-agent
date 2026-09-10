import { describe, expect, it } from "vitest";
import { currentStage, EPISODE_STEPS, type StageHint } from "@/lib/stage-steps";

const BASE: StageHint = {
  hasConsultation: false,
  adopted: false,
  hasPublishedSample: false,
  fullAuthorized: false,
};

describe("stage-steps", () => {
  it("4段階の語彙は固定順", () => {
    expect([...EPISODE_STEPS]).toEqual(["素材", "方向", "試し動画", "全編確認"]);
  });

  it("相談なし・非対象段階は素材", () => {
    expect(currentStage(null, false)).toBe("素材");
    expect(currentStage(BASE, false)).toBe("素材");
  });

  it("相談対象・相談あり・採用済み未試写は方向", () => {
    expect(currentStage(null, true)).toBe("方向");
    expect(currentStage({ ...BASE, hasConsultation: true }, false)).toBe("方向");
    expect(currentStage({ ...BASE, adopted: true }, false)).toBe("方向");
  });

  it("採用＋公開済み試し動画は試し動画", () => {
    expect(
      currentStage(
        { ...BASE, hasConsultation: true, adopted: true, hasPublishedSample: true },
        true,
      ),
    ).toBe("試し動画");
  });

  it("全編許可は全編確認（他フラグに優先）", () => {
    expect(
      currentStage(
        {
          hasConsultation: true,
          adopted: true,
          hasPublishedSample: true,
          fullAuthorized: true,
        },
        true,
      ),
    ).toBe("全編確認");
  });
});
