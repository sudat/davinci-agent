import { describe, expect, it } from "vitest";
import { deriveFlowState, type FlowStateInput } from "@/lib/flow-state";

function input(overrides: Partial<FlowStateInput>): FlowStateInput {
  return {
    loaded: true,
    hasConsultation: false,
    hasProposal: false,
    adopted: false,
    hasPublishedSample: false,
    fullAuthorized: false,
    ...overrides,
  };
}

describe("deriveFlowState（5画面の現在地判定）", () => {
  it("未読込ではcompose（希望入力が主操作）", () => {
    expect(deriveFlowState(input({ loaded: false }))).toBe("compose");
  });

  it("何も送っていなければcompose", () => {
    expect(deriveFlowState(input({}))).toBe("compose");
  });

  it("送ったが応答なしではwaiting-proposals", () => {
    expect(deriveFlowState(input({ hasConsultation: true }))).toBe(
      "waiting-proposals",
    );
  });

  it("応答あり・未採用ではchoose", () => {
    expect(
      deriveFlowState(input({ hasConsultation: true, hasProposal: true })),
    ).toBe("choose");
  });

  it("採用済み・試し動画なしではwaiting-trial", () => {
    expect(
      deriveFlowState(
        input({ hasConsultation: true, hasProposal: true, adopted: true }),
      ),
    ).toBe("waiting-trial");
  });

  it("試し動画ありではreview", () => {
    expect(
      deriveFlowState(
        input({
          hasConsultation: true,
          hasProposal: true,
          adopted: true,
          hasPublishedSample: true,
        }),
      ),
    ).toBe("review");
  });

  it("全編許可ではreview（試し動画の有無によらない）", () => {
    expect(deriveFlowState(input({ fullAuthorized: true }))).toBe("review");
  });
});
