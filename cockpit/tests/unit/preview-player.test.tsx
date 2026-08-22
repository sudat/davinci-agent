import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { createRef } from "react";
import PreviewPlayer from "@/components/PreviewPlayer";

describe("PreviewPlayer", () => {
  it("availableでは video 要素（preview URL・controls付き）を表示する", () => {
    const videoRef = createRef<HTMLVideoElement>();
    const { container } = render(
      <PreviewPlayer episodeId="ep-abc" state="available" videoRef={videoRef} />,
    );
    const video = screen.getByTestId("preview-player") as HTMLVideoElement;
    expect(video.tagName).toBe("VIDEO");
    expect(video.getAttribute("src")).toBe("/cockpit-api/episodes/ep-abc/preview");
    expect(video.hasAttribute("controls")).toBe(true);
    expect(videoRef.current).toBe(video);
    expect(container.querySelector('[data-testid="preview-pending"]')).toBeNull();
  });

  it("not_generatedでは構造化された「未生成」状態を表示（videoなし・エラー扱いしない）", () => {
    const { container } = render(
      <PreviewPlayer episodeId="ep-abc" state="not_generated" videoRef={createRef()} />,
    );
    const pending = screen.getByTestId("preview-pending");
    expect(pending).toBeVisible();
    expect(pending.textContent).toContain("プレビューはまだ生成されていません");
    expect(container.querySelector("video")).toBeNull();
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it("checkingでは確認中の表示（videoなし）", () => {
    const { container } = render(
      <PreviewPlayer episodeId="ep-abc" state="checking" videoRef={createRef()} />,
    );
    expect(screen.getByTestId("preview-checking")).toBeVisible();
    expect(container.querySelector("video")).toBeNull();
  });
});
