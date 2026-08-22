import { describe, expect, it } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import AdvancedSection from "@/components/AdvancedSection";

describe("AdvancedSection（初期折りたたみ）", () => {
  it("既定では折りたたまれており、高度な項目は非表示", () => {
    render(<AdvancedSection />);
    const details = screen.getByTestId("advanced-section") as HTMLDetailsElement;
    expect(details.tagName).toBe("DETAILS");
    expect(details.open).toBe(false);
    // 折りたたみ状態では必須シーン等の項目が見えない
    expect(screen.queryByLabelText("必須シーン（must-include）")).not.toBeNull();
    expect(
      (screen.getByLabelText("必須シーン（must-include）") as HTMLElement)
        .offsetParent,
    ).toBeNull();
  });

  it("summaryクリックで開き、項目が表示される", () => {
    render(<AdvancedSection />);
    const details = screen.getByTestId("advanced-section") as HTMLDetailsElement;
    fireEvent.click(screen.getByText("高度な設定（任意）"));
    expect(details.open).toBe(true);
    expect(screen.getByLabelText("公開制約")).toBeTruthy();
    expect(screen.getByLabelText("対象視聴者")).toBeTruthy();
    expect(
      screen.getByLabelText("提示強度") as HTMLSelectElement,
    ).toHaveProperty("value", "standard");
  });

  it("高度な項目（提示強度）の既定値は標準", () => {
    render(<AdvancedSection />);
    fireEvent.click(screen.getByText("高度な設定（任意）"));
    const intensity = screen.getByLabelText(
      "提示強度",
    ) as HTMLSelectElement;
    expect(intensity.value).toBe("standard");
  });
});
