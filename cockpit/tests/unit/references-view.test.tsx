import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import ReferencesView from "@/components/ReferencesView";

const REGISTER_OK = {
  source_id: "ref-def456",
  sha256: "b".repeat(64),
  library_version: 2,
  idempotent: false,
};

const EMPTY_LIBRARY = {
  available: false,
  references: [],
};

const POPULATED_LIBRARY = {
  available: true,
  references: [
    {
      source_id: "ref-def456",
      kind: "local_file",
      location: "/tmp/style-ref.mp4",
      sha256: "b".repeat(64),
      created_at: "2026-01-01T00:00:00Z",
    },
  ],
};

function jsonResponse(payload: object, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  sessionStorage.clear();
});

describe("ReferencesView（参照登録と永続化ライブラリ一覧）", () => {
  it("参照を登録するとAPIへPOSTし、セッション一覧と永続化ライブラリに出る", async () => {
    let postSeen = false;
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      expect(url).toBe("/cockpit-api/references");
      if (init?.method === "POST") {
        postSeen = true;
        return jsonResponse(REGISTER_OK);
      }
      return jsonResponse(postSeen ? POPULATED_LIBRARY : EMPTY_LIBRARY);
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

    const calls = (fetchImpl as ReturnType<typeof vi.fn>).mock
      .calls as Array<[string, RequestInit?]>;
    const postCall = calls.find(([, init]) => init?.method === "POST");
    expect(postCall).toBeDefined();
    expect(JSON.parse((postCall![1]!.body as string) ?? "{}")).toEqual({
      path: "/tmp/style-ref.mp4",
    });

    await waitFor(() => {
      expect(screen.getAllByTestId("library-reference-item")).toHaveLength(1);
    });
    expect(screen.getByTestId("library-reference-item").textContent).toContain(
      "/tmp/style-ref.mp4",
    );

    const stored = JSON.parse(
      sessionStorage.getItem("cockpit.references.v1") ?? "[]",
    ) as Array<{ source_id: string }>;
    expect(stored[0].source_id).toBe("ref-def456");
  });

  it("バックエンド構造化エラーはそのまま表示される（存在しないパス）", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        return jsonResponse(
          { error: { code: "reference-unavailable", detail: "Local reference not found" } },
          422,
        );
      }
      return jsonResponse(EMPTY_LIBRARY);
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

  it("永続化ライブラリが空のときは空表示、スコアカードも出ない", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse(EMPTY_LIBRARY));
    render(<ReferencesView fetchImpl={fetchImpl as unknown as typeof fetch} />);

    await waitFor(() => {
      expect(screen.getByTestId("library-reference-empty")).toBeTruthy();
    });
    expect(document.querySelectorAll('input[type="range"]')).toHaveLength(0);
    expect(screen.queryByTestId("scorecard")).toBeNull();
    expect(screen.queryByTestId("pairwise-prompt")).toBeNull();
  });
});
