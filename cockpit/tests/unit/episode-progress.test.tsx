import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import EpisodeProgress from "@/components/EpisodeProgress";
import type { EpisodeStatus } from "@/lib/api";

function status(overrides: Partial<EpisodeStatus>): EpisodeStatus {
  return {
    episode_id: "ep-test01",
    job_id: "ep-test01",
    status: "ANALYZED",
    current_stage: "analyze",
    created_at_seq: 1,
    updated_at_seq: 3,
    stage_runs: [
      { stage_name: "ingest", status: "succeeded", retry_count: 0, last_error_code: null },
      { stage_name: "normalize", status: "succeeded", retry_count: 0, last_error_code: null },
      { stage_name: "analyze", status: "running", retry_count: 1, last_error_code: null },
    ],
    ...overrides,
  };
}

describe("EpisodeProgress — ETAは計測裏付けの時のみ表示（PRD 13.2）", () => {
  it("eta_minutes がないpayloadではETAを一切表示しない", () => {
    const { container } = render(<EpisodeProgress status={status({})} />);
    expect(screen.queryByTestId("eta")).toBeNull();
    // stage/progress は表示する（捏造した精度ではなく実在するデータのみ）
    expect(screen.getByText("analyze")).toBeVisible();
    expect(container.textContent).not.toContain("ETA");
    expect(container.textContent).not.toContain("分");
  });

  it("eta_minutes（実測裏付け）があるpayloadではETAを表示する", () => {
    render(<EpisodeProgress status={status({ eta_minutes: 12 })} />);
    const eta = screen.getByTestId("eta");
    expect(eta).toBeVisible();
    expect(eta.textContent).toContain("12");
  });

  it("eta_minutes が負や非数なら捏造扱いで表示しない", () => {
    const { rerender } = render(<EpisodeProgress status={status({ eta_minutes: -5 })} />);
    expect(screen.queryByTestId("eta")).toBeNull();
    rerender(<EpisodeProgress status={status({ eta_minutes: Number.NaN })} />);
    expect(screen.queryByTestId("eta")).toBeNull();
  });
});

describe("EpisodeProgress — 作業単位の完了/残", () => {
  it("work_units がある場合は 完了/残 を表示する", () => {
    render(<EpisodeProgress status={status({ work_units: { completed: 4, remaining: 2 } })} />);
    const units = screen.getByTestId("work-units");
    expect(units.textContent).toContain("4");
    expect(units.textContent).toContain("2");
  });

  it("work_units がない場合はstage_runsの実数のみ（残の捏造なし）", () => {
    render(<EpisodeProgress status={status({})} />);
    const units = screen.getByTestId("work-units");
    expect(units.textContent).toContain("2"); // succeeded stage 数
    expect(units.textContent).not.toContain("残");
  });
});
