import { describe, expect, it } from "vitest";
import { confirmationReasonJa } from "@/lib/reviewText";

describe("confirmationReasonJa（確認理由の日本語表示）", () => {
  it("既知の理由は日本語に置き換わる", () => {
    expect(
      confirmationReasonJa(
        "no known command kind matched the message; restate the correction",
      ),
    ).toBe("どの修正にも当てはまりませんでした。言い換えて伝えてください");
    expect(
      confirmationReasonJa(
        "no target timestamp: set the player position or name an explicit time in the message",
      ),
    ).toContain("対象の時刻がわかりませんでした");
    expect(
      confirmationReasonJa(
        "llm-interpretation: no explicit target timestamp for a positional command; confirm the restated correction",
      ),
    ).toContain("対象時刻が明示されていません");
  });

  it("未知の理由は原文のまま（黙って置き換えない）", () => {
    expect(confirmationReasonJa("something new the backend invented")).toBe(
      "something new the backend invented",
    );
  });

  it("null は null", () => {
    expect(confirmationReasonJa(null)).toBeNull();
  });
});
