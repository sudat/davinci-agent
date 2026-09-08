import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { createRef } from "react";
import PreviewPlayer from "@/components/PreviewPlayer";

describe("PreviewPlayer", () => {
  it("availableでは video 要素（preview URL・controls付き）を表示する。hash不明なら固定URL", () => {
    const videoRef = createRef<HTMLVideoElement>();
    const { container } = render(
      <PreviewPlayer
        episodeId="ep-abc"
        state="available"
        contentHash={null}
        videoRef={videoRef}
      />,
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
      <PreviewPlayer
        episodeId="ep-abc"
        state="not_generated"
        contentHash={null}
        videoRef={createRef()}
      />,
    );
    const pending = screen.getByTestId("preview-pending");
    expect(pending).toBeVisible();
    expect(pending.textContent).toContain("プレビューはまだ生成されていません");
    expect(container.querySelector("video")).toBeNull();
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it("checkingでは確認中の表示（videoなし）", () => {
    const { container } = render(
      <PreviewPlayer
        episodeId="ep-abc"
        state="checking"
        contentHash={null}
        videoRef={createRef()}
      />,
    );
    expect(screen.getByTestId("preview-checking")).toBeVisible();
    expect(container.querySelector("video")).toBeNull();
  });

  it("hash既知ならURLにcontent_hashを付き、hash変更でvideo要素が作り直される（stale再生をしない）", () => {
    const videoRef = createRef<HTMLVideoElement>();
    const view = render(
      <PreviewPlayer
        episodeId="ep-abc"
        state="available"
        contentHash="hash-1"
        videoRef={videoRef}
      />,
    );
    const first = screen.getByTestId("preview-player") as HTMLVideoElement;
    expect(first.getAttribute("src")).toBe(
      "/cockpit-api/episodes/ep-abc/preview?content_hash=hash-1",
    );
    expect(videoRef.current).toBe(first);

    view.rerender(
      <PreviewPlayer
        episodeId="ep-abc"
        state="available"
        contentHash="hash-2"
        videoRef={videoRef}
      />,
    );
    const second = screen.getByTestId("preview-player") as HTMLVideoElement;
    expect(second).not.toBe(first);
    expect(second.getAttribute("src")).toBe(
      "/cockpit-api/episodes/ep-abc/preview?content_hash=hash-2",
    );
    expect(videoRef.current).toBe(second);
  });

  it("hash不変の再描画ではvideo要素は作り直されない（無用な再読込をしない）", () => {
    const view = render(
      <PreviewPlayer
        episodeId="ep-abc"
        state="available"
        contentHash="hash-1"
        videoRef={createRef()}
      />,
    );
    const before = screen.getByTestId("preview-player");
    view.rerender(
      <PreviewPlayer
        episodeId="ep-abc"
        state="available"
        contentHash="hash-1"
        videoRef={createRef()}
      />,
    );
    expect(screen.getByTestId("preview-player")).toBe(before);
  });

  it("hash不明（null）から判明への遷移でも固定URLからhash付きURLへ作り直される", () => {
    const view = render(
      <PreviewPlayer
        episodeId="ep-abc"
        state="available"
        contentHash={null}
        videoRef={createRef()}
      />,
    );
    const before = screen.getByTestId("preview-player") as HTMLVideoElement;
    expect(before.getAttribute("src")).toBe("/cockpit-api/episodes/ep-abc/preview");
    view.rerender(
      <PreviewPlayer
        episodeId="ep-abc"
        state="available"
        contentHash="hash-1"
        videoRef={createRef()}
      />,
    );
    const after = screen.getByTestId("preview-player") as HTMLVideoElement;
    expect(after).not.toBe(before);
    expect(after.getAttribute("src")).toBe(
      "/cockpit-api/episodes/ep-abc/preview?content_hash=hash-1",
    );
  });
});
