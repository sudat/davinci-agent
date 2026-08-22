import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import FlagList from "@/components/FlagList";
import type { ReviewFlag } from "@/lib/api";

const flag = (overrides: Partial<ReviewFlag>): ReviewFlag => ({
  sequence: 1,
  kind: "proposal_recorded",
  reason: "冒頭の無音を削除",
  ...overrides,
});

describe("FlagList — not_yet_generated / 一覧", () => {
  it("not_yet_generated では構造化された未生成状態を表示", () => {
    render(
      <FlagList flags={[]} notYetGenerated canSeek={false} onSeek={() => {}} />,
    );
    const empty = screen.getByTestId("flags-empty");
    expect(empty.textContent).toContain("レビューflag");
    expect(empty.textContent).toContain("まだ生成");
  });

  it("flagなし（生成済み・空）では「flagはありません」", () => {
    render(<FlagList flags={[]} notYetGenerated={false} canSeek={false} onSeek={() => {}} />);
    expect(screen.getByTestId("flags-empty").textContent).toContain("ありません");
  });

  it("flag項目は kind / reason / sequence を表示する", () => {
    render(
      <FlagList
        flags={[flag({ sequence: 3, reason: "テロップ修正" })]}
        notYetGenerated={false}
        canSeek={false}
        onSeek={() => {}}
      />,
    );
    const item = screen.getByTestId("flag-item");
    expect(item.textContent).toContain("proposal_recorded");
    expect(item.textContent).toContain("テロップ修正");
    expect(item.textContent).toContain("#3");
  });
});

describe("FlagList — timestamp jump", () => {
  it("タイムスタンプ付きflag＋playerあり → クリックで onSeek(ts) が呼ばれる", () => {
    const onSeek = vi.fn();
    render(
      <FlagList
        flags={[flag({ at_seconds: 1.25 })]}
        notYetGenerated={false}
        canSeek
        onSeek={onSeek}
      />,
    );
    const jump = screen.getByTestId("flag-jump");
    expect(jump).toBeEnabled();
    fireEvent.click(jump);
    expect(onSeek).toHaveBeenCalledWith(1.25);
  });

  it("タイムスタンプ付きflag＋playerなし → disabled-seek表示＋説明（クリックしてもseekしない）", () => {
    const onSeek = vi.fn();
    render(
      <FlagList
        flags={[flag({ at_seconds: 1.25 })]}
        notYetGenerated={false}
        canSeek={false}
        onSeek={onSeek}
      />,
    );
    const jump = screen.getByTestId("flag-jump");
    expect(jump).toBeDisabled();
    expect(jump.textContent).toContain("0:01");
    expect(screen.getByTestId("flag-jump-note").textContent).toContain(
      "プレビュー未生成のため",
    );
    fireEvent.click(jump);
    expect(onSeek).not.toHaveBeenCalled();
  });

  it("タイムスタンプなしflag（task-44の現行形状）→ jumpボタンを出さない", () => {
    render(
      <FlagList
        flags={[flag({})]}
        notYetGenerated={false}
        canSeek
        onSeek={() => {}}
      />,
    );
    expect(screen.getByTestId("flag-item")).toBeVisible();
    expect(screen.queryByTestId("flag-jump")).toBeNull();
  });
});
