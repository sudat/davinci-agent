import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const pushMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

import IntakeForm from "@/components/IntakeForm";
import { CockpitApiError } from "@/lib/api";

function fill(id: string, value: string) {
  fireEvent.change(screen.getByLabelText(id), {
    target: { value },
  });
}

describe("IntakeForm（Createボタン活性ロジック）", () => {
  beforeEach(() => {
    pushMock.mockClear();
  });

  it("初期状態ではCreate無効（空ソース）", () => {
    render(<IntakeForm />);
    const create = screen.getByTestId("create-button") as HTMLButtonElement;
    expect(create.disabled).toBe(true);
  });

  it("briefだけでは無効（空ソース → malformed input）", () => {
    render(<IntakeForm />);
    fill("この動画は何について？", "テストbrief");
    const create = screen.getByTestId("create-button") as HTMLButtonElement;
    expect(create.disabled).toBe(true);
  });

  it("空白のみのbriefでも無効", () => {
    render(<IntakeForm />);
    fill("ソースフォルダ", "/tmp/a");
    fill("この動画は何について？", "   ");
    const create = screen.getByTestId("create-button") as HTMLButtonElement;
    expect(create.disabled).toBe(true);
  });

  it("ソース+briefで活性し、送信後に /episodes/{id} へ遷移", async () => {
    render(<IntakeForm />);
    fill("ソースフォルダ", "/tmp/a");
    fill("この動画は何について？", "テストbrief");
    const create = screen.getByTestId("create-button") as HTMLButtonElement;
    expect(create.disabled).toBe(false);

    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          episode_id: "ep-ok",
          job_id: "ep-ok",
          status: "CREATED",
          brief_status: "draft",
        }),
        { status: 200 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    fireEvent.click(create);
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/episodes/ep-ok"));
    vi.unstubAllGlobals();
  });

  it("API失敗時は構造化エラーを表示して遷移しない", async () => {
    render(<IntakeForm />);
    fill("ソースフォルダ", "/tmp/missing");
    fill("この動画は何について？", "テストbrief");
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: "source-folder-not-found",
            detail: "source folder does not exist: /tmp/missing",
          },
        }),
        { status: 422 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    fireEvent.click(screen.getByTestId("create-button"));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toBeTruthy(),
    );
    expect(screen.getByRole("alert").textContent).toContain(
      "source-folder-not-found",
    );
    expect(pushMock).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
    expect(fetchMock.mock.calls[0]).toBeDefined();
  });
});

describe("CockpitApiError の表示形式", () => {
  it("code / detail を保持する", () => {
    const error = new CockpitApiError("episode-exists", 409, "重複");
    expect(error.code).toBe("episode-exists");
    expect(error.status).toBe(409);
    expect(error.detail).toBe("重複");
    expect(error.message).toContain("episode-exists");
  });
});
