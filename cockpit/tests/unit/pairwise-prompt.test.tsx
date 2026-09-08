import { beforeEach, describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import PairwisePrompt from "@/components/PairwisePrompt";

// 工程2: reason OPTIONAL + 「どちらも違う」 choice, with session-storage
// backward compatibility (old records carry string reasons and still parse).
const OLD_RECORD = {
  domain: "color",
  choice: "a",
  reason: "昔の記録には理由が文字列で入っている",
  reference_a_id: "ref-old-a",
  reference_b_id: "ref-old-b",
  saved_at: "2026-01-01T00:00:00.000Z",
};

beforeEach(() => {
  sessionStorage.clear();
});

describe("PairwisePrompt（理由任意・どちらも違う・互換）", () => {
  it("理由なしで記録でき、空の理由はnullとして保存される（捏造しない）", () => {
    render(<PairwisePrompt referenceIds={["ref-aaa", "ref-bbb"]} />);
    expect(screen.getByText("理由（任意）")).toBeTruthy();
    fireEvent.click(screen.getByTestId("pairwise-choice-a"));
    // 理由が空のまま記録ボタンが有効
    expect((screen.getByTestId("pairwise-record") as HTMLButtonElement).disabled).toBe(
      false,
    );
    fireEvent.click(screen.getByTestId("pairwise-record"));

    const stored = JSON.parse(
      sessionStorage.getItem("cockpit.pairwise.v1") ?? "[]",
    ) as Array<{ choice: string; reason: string | null }>;
    expect(stored).toHaveLength(1);
    expect(stored[0].choice).toBe("a");
    expect(stored[0].reason).toBeNull();
    expect(screen.getByTestId("saved-pairwise-item").textContent).toBe(
      "色: A を選択",
    );
  });

  it("「どちらも違う」を選ぶとchoice=neitherで記録される", () => {
    render(<PairwisePrompt referenceIds={["ref-aaa", "ref-bbb"]} />);
    fireEvent.click(screen.getByTestId("pairwise-choice-neither"));
    expect(
      (
        screen.getByTestId("pairwise-choice-neither") as HTMLButtonElement
      ).getAttribute("aria-pressed"),
    ).toBe("true");
    fireEvent.click(screen.getByTestId("pairwise-record"));

    const stored = JSON.parse(
      sessionStorage.getItem("cockpit.pairwise.v1") ?? "[]",
    ) as Array<{ choice: string }>;
    expect(stored).toHaveLength(1);
    expect(stored[0].choice).toBe("neither");
    expect(screen.getByTestId("saved-pairwise-item").textContent).toContain(
      "どちらも違う",
    );
  });

  it("理由が文字列の旧レコードもそのまま表示でき、新しい記録を追記できる", () => {
    sessionStorage.setItem("cockpit.pairwise.v1", JSON.stringify([OLD_RECORD]));
    render(<PairwisePrompt referenceIds={["ref-aaa", "ref-bbb"]} />);

    const items = screen.getAllByTestId("saved-pairwise-item");
    expect(items).toHaveLength(1);
    expect(items[0].textContent).toBe("色: A を選択 — 昔の記録には理由が文字列で入っている");

    // 追記（clear/appendの意味づけは不変）: 新しいnull理由レコードが旧レコードと共存
    fireEvent.click(screen.getByTestId("pairwise-choice-b"));
    fireEvent.click(screen.getByTestId("pairwise-record"));
    const stored = JSON.parse(
      sessionStorage.getItem("cockpit.pairwise.v1") ?? "[]",
    ) as Array<{ choice: string; reason: string | null }>;
    expect(stored).toHaveLength(2);
    expect(stored[0].reason).toBe(OLD_RECORD.reason);
    expect(stored[1]).toMatchObject({ choice: "b", reason: null });
    expect(screen.getAllByTestId("saved-pairwise-item")).toHaveLength(2);
  });
});
