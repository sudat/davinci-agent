import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import ReviewChatPanel from "@/components/ReviewChatPanel";
import type { EpisodeStatus, FrameMaterial } from "@/lib/api";

// 工程2 display honesty: the 「確認できたもの」 block lists ONLY the flags'
// materials (＋ AI確認済みの静止画, rework round 2 P1-2) and stays DISTINCT
// from the 仮説 block (per-draft). One test per flag/stills/verification
// combination (backend `checked_materials`, review_reactions.py). Stills are
// 静止画（フレーム） — never a 映像/音の確認. frames_verified=false のとき
// 抽出はしたがAI確認はできていない、別行で正直に表示する。
const DRAFT_WITH_HYPOTHESIS = {
  schema_version: "cockpit-review-command-draft-v1",
  command_id: "rcmd-feelings00001",
  command_kind: "mark_boring",
  text: "ここ退屈",
  target_seconds: 6,
  seconds_delta: null,
  scope: "episode",
  needs_confirmation: true,
  confirmation_reason: "this expresses a feeling, not a concrete change",
  investigated: true,
  hypothesis: "同じ説明が続いているのが原因の可能性",
};

function statusOf(): EpisodeStatus {
  return {
    episode_id: "ep-abc",
    job_id: "ep-abc",
    status: "PREVIEW_READY",
    current_stage: "preview",
    created_at_seq: 1,
    updated_at_seq: 1,
    stage_runs: [],
  };
}

function jsonResponse(payload: object, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

async function sendWith(checkedMaterials: {
  transcript: boolean;
  shot: boolean;
  frames?: FrameMaterial[];
  frames_delivery_attempted?: boolean;
  frames_verified?: boolean;
}): Promise<void> {
  const fetchImpl = vi.fn(async () =>
    jsonResponse({
      received: true,
      sequence: 7,
      draft: DRAFT_WITH_HYPOTHESIS,
      investigated: true,
      hypothesis: DRAFT_WITH_HYPOTHESIS.hypothesis,
      investigation_state: "hypothesis-proposed",
      checked_materials: checkedMaterials,
    }),
  );
  render(
    <ReviewChatPanel
      episodeId="ep-abc"
      getAtSeconds={() => 6.0}
      status={statusOf()}
      fetchImpl={fetchImpl as unknown as typeof fetch}
    />,
  );
  fireEvent.change(screen.getByLabelText("気になるところを伝える"), {
    target: { value: "ここ退屈" },
  });
  fireEvent.click(screen.getByTestId("review-chat-send"));
  await waitFor(() => {
    expect(screen.getByTestId("review-draft-investigation")).toBeTruthy();
  });
}

describe("ReviewChatPanel（確認できたものブロック）", () => {
  it("両方確認できた場合は仮説ブロックと別に両方を列挙する", async () => {
    await sendWith({ transcript: true, shot: true });
    expect(screen.getByTestId("review-checked-materials").textContent).toBe(
      "確認できたもの: 周辺の字幕・場面情報",
    );
    // 仮説ブロックは別要素（ドラフトカード内）のまま
    expect(screen.getByTestId("review-draft-investigation").textContent).toContain(
      "AIの仮説",
    );
  });

  it("字幕だけ確認できた場合は字幕だけを列挙する", async () => {
    await sendWith({ transcript: true, shot: false });
    expect(screen.getByTestId("review-checked-materials").textContent).toBe(
      "確認できたもの: 周辺の字幕",
    );
  });

  it("場面情報だけ確認できた場合は場面情報だけを列挙する", async () => {
    await sendWith({ transcript: false, shot: true });
    expect(screen.getByTestId("review-checked-materials").textContent).toBe(
      "確認できたもの: 場面情報",
    );
  });

  it("何も確認できていない場合はブロック自体を出さない", async () => {
    await sendWith({ transcript: false, shot: false });
    expect(screen.queryByTestId("review-checked-materials")).toBeNull();
  });

  it("frames_verifiedがtrueの場合は静止画N枚と位置（M:SS付近）を字幕・場面情報に続けて列挙する", async () => {
    await sendWith({
      transcript: true,
      shot: true,
      frames_verified: true,
      frames: [
        { path: "/ep/review-frames/frame-000.jpg", at_seconds: 42, source: "/src/a.mp4" },
        { path: "/ep/review-frames/frame-001.jpg", at_seconds: 43.4, source: "/src/a.mp4" },
        { path: "/ep/review-frames/frame-002.jpg", at_seconds: 44.9, source: "/src/a.mp4" },
      ],
    });
    expect(screen.getByTestId("review-checked-materials").textContent).toBe(
      "確認できたもの: 周辺の字幕・場面情報・静止画3枚（0:42・0:43・0:44付近）",
    );
  });

  it("flagsよりframesだけでも静止画の列挙を出し、60秒超はM:SSで繰り上げ表示する", async () => {
    await sendWith({
      transcript: false,
      shot: false,
      frames_verified: true,
      frames: [
        { path: "/ep/review-frames/frame-000.jpg", at_seconds: 63.2, source: "/src/a.mp4" },
        { path: "/ep/review-frames/frame-001.jpg", at_seconds: 125.8, source: "/src/a.mp4" },
      ],
    });
    expect(screen.getByTestId("review-checked-materials").textContent).toBe(
      "確認できたもの: 静止画2枚（1:03・2:05付近）",
    );
  });

  it("静止画の表示は決して映像・音の確認と主張しない", async () => {
    await sendWith({
      transcript: false,
      shot: false,
      frames_verified: true,
      frames: [
        { path: "/ep/review-frames/frame-000.jpg", at_seconds: 10, source: "/src/a.mp4" },
      ],
    });
    const shown = screen.getByTestId("review-checked-materials").textContent ?? "";
    expect(shown).toContain("静止画1枚");
    expect(shown).not.toContain("映像");
    expect(shown).not.toContain("音声");
    expect(shown).not.toContain("動画");
    expect(shown).not.toContain("音の確認");
  });

  it("frames_verifiedがfalseの場合は静止画を確認できたものに入れず、抽出のみの正直な行を出す", async () => {
    await sendWith({
      transcript: true,
      shot: false,
      frames_verified: false,
      frames_delivery_attempted: true,
      frames: [
        { path: "/ep/review-frames/frame-000.jpg", at_seconds: 42, source: "/src/a.mp4" },
        { path: "/ep/review-frames/frame-001.jpg", at_seconds: 43.4, source: "/src/a.mp4" },
      ],
    });
    expect(screen.getByTestId("review-checked-materials").textContent).toBe(
      "確認できたもの: 周辺の字幕",
    );
    expect(screen.getByTestId("review-frames-extracted").textContent).toBe(
      "静止画2枚を抽出しましたが、AIでの確認はできていません",
    );
  });

  it("frames_verifiedがない場合も同様に確認できたものから外し、抽出のみの行を出す", async () => {
    await sendWith({
      transcript: false,
      shot: false,
      frames: [
        { path: "/ep/review-frames/frame-000.jpg", at_seconds: 10, source: "/src/a.mp4" },
      ],
    });
    expect(screen.queryByTestId("review-checked-materials")).toBeNull();
    expect(screen.getByTestId("review-frames-extracted").textContent).toBe(
      "静止画1枚を抽出しましたが、AIでの確認はできていません",
    );
  });

  it("抽出のみの行も映像・音の確認と主張しない", async () => {
    await sendWith({
      transcript: false,
      shot: false,
      frames: [
        { path: "/ep/review-frames/frame-000.jpg", at_seconds: 10, source: "/src/a.mp4" },
      ],
    });
    const shown = screen.getByTestId("review-frames-extracted").textContent ?? "";
    expect(shown).toContain("抽出");
    expect(shown).not.toContain("確認できた");
    expect(shown).not.toContain("映像");
    expect(shown).not.toContain("音");
  });
});
