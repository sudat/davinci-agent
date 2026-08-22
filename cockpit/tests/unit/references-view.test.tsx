import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import ReferencesView from "@/components/ReferencesView";

const REGISTER_OK = {
  source_id: "ref-def456",
  sha256: "b".repeat(64),
  library_version: 2,
};

beforeEach(() => {
  sessionStorage.clear();
});

describe("ReferencesView（参照登録とセッション一覧）", () => {
  it("参照を登録するとAPIへPOSTし、一覧にsource_idが出る", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL): Promise<Response> => {
      const url = String(input);
      expect(url).toBe("/cockpit-api/references");
      return new Response(JSON.stringify(REGISTER_OK), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    render(<ReferencesView fetchImpl={fetchImpl as unknown as typeof fetch} />);

    const registerButton = screen.getByTestId("register-reference-button");
    expect((registerButton as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("参照ファイルパス"), {
      target: { value: "/tmp/style-ref.mp4" },
    });
    expect((registerButton as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(registerButton);

    await waitFor(() => {
      expect(screen.getAllByTestId("reference-item")).toHaveLength(1);
    });
    const item = screen.getByTestId("reference-item");
    expect(item.textContent).toContain("ref-def456");
    expect(item.textContent).toContain("ライブラリ版 2");

    const [url, init] = (fetchImpl as ReturnType<typeof vi.fn>).mock
      .calls[0] as [string, RequestInit];
    expect(url).toBe("/cockpit-api/references");
    expect(JSON.parse(init.body as string)).toEqual({ path: "/tmp/style-ref.mp4" });

    const stored = JSON.parse(
      sessionStorage.getItem("cockpit.references.v1") ?? "[]",
    ) as Array<{ source_id: string }>;
    expect(stored[0].source_id).toBe("ref-def456");
  });

  it("バックエンド構造化エラーはそのまま表示される（存在しないパス）", async () => {
    const fetchImpl = vi.fn(async (): Promise<Response> => {
      return new Response(
        JSON.stringify({
          error: { code: "reference-unavailable", detail: "Local reference not found" },
        }),
        { status: 422, headers: { "content-type": "application/json" } },
      );
    });
    render(<ReferencesView fetchImpl={fetchImpl as unknown as typeof fetch} />);

    fireEvent.change(screen.getByLabelText("参照ファイルパス"), {
      target: { value: "/nonexistent/ref.mp4" },
    });
    fireEvent.click(screen.getByTestId("register-reference-button"));

    await waitFor(() => {
      expect(screen.getByTestId("error-notice")).toBeTruthy();
    });
    expect(screen.getByTestId("error-notice").textContent).toContain(
      "reference-unavailable",
    );
    expect(screen.queryAllByTestId("reference-item")).toHaveLength(0);
  });

  it("セッション範囲の注記とスコアカード不在を検証する", () => {
    const fetchImpl = vi.fn();
    render(<ReferencesView fetchImpl={fetchImpl as unknown as typeof fetch} />);
    expect(screen.getByTestId("session-scope-note").textContent).toContain(
      "このセッション",
    );
    expect(document.querySelectorAll('input[type="range"]')).toHaveLength(0);
    expect(screen.queryByTestId("scorecard")).toBeNull();
    expect(screen.queryByTestId("pairwise-prompt")).toBeNull();
  });
});
