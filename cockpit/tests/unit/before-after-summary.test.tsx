import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import BeforeAfterSummary from "@/components/BeforeAfterSummary";
import type { BeforeAfterSummary as BeforeAfterPayload } from "@/lib/api";

describe("BeforeAfterSummary — ペイロードがある時だけ表示", () => {
  it("null（比較データなし）では何も表示しない", () => {
    const { container } = render(<BeforeAfterSummary summary={null} />);
    expect(screen.queryByTestId("before-after")).toBeNull();
    expect(container.textContent).toBe("");
  });

  it("比較payloadがある場合は要約＋各項目の before/after を表示する", () => {
    const payload: BeforeAfterPayload = {
      summary: "指摘3件のうち2件を反映",
      items: [
        { label: "冒頭の無音", before: "12秒", after: "3秒" },
        { label: "テロップ", before: "実写のまま", after: "字幕に置換" },
      ],
    };
    render(<BeforeAfterSummary summary={payload} />);
    const section = screen.getByTestId("before-after");
    expect(section.textContent).toContain("指摘3件のうち2件を反映");
    expect(section.textContent).toContain("冒頭の無音");
    expect(section.textContent).toContain("12秒");
    expect(section.textContent).toContain("字幕に置換");
  });
});
