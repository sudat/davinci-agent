import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import ReferenceAnnotator from "@/components/ReferenceAnnotator";

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

type Route = { match: string; body: unknown };

function makeFetch(routes: Route[]) {
  return vi.fn(async (input: RequestInfo | URL): Promise<Response> => {
    const url = String(input);
    const route = routes.find((candidate) => url.includes(candidate.match));
    if (route === undefined) {
      return new Response(
        JSON.stringify({ error: { code: "route-not-found", detail: url } }),
        { status: 404, headers: { "content-type": "application/json" } },
      );
    }
    return jsonResponse(route.body);
  });
}

const COLOR_DRAFT = {
  named_domains: ["color"],
  domains: { color: "like" },
  needs_review: false,
  rationale: "色が良い",
  confidence: 0.85,
  ts_seconds: null,
};

const AMBIGUOUS_DRAFT = {
  named_domains: [],
  domains: {},
  needs_review: true,
  rationale: "全部良い",
  confidence: 0.4,
  ts_seconds: null,
};

const REGISTER_OK = {
  source_id: "ref-abc123",
  sha256: "a".repeat(64),
  library_version: 2,
};

function seedTwoReferences() {
  sessionStorage.setItem(
    "cockpit.references.v1",
    JSON.stringify([
      {
        source_id: "ref-aaa",
        path: "/a.mp4",
        sha256: "a",
        library_version: 2,
        registered_at: "t1",
      },
      {
        source_id: "ref-bbb",
        path: "/b.mp4",
        sha256: "b",
        library_version: 3,
        registered_at: "t2",
      },
    ]),
  );
}

beforeEach(() => {
  sessionStorage.clear();
});

describe("ReferenceAnnotator（注釈→抽出→修正→保存）", () => {
  it("コメントからドメイン抽出プレビューが色付きチップで表示される", async () => {
    const fetchImpl = makeFetch([
      { match: "/references/parse-preview", body: COLOR_DRAFT },
    ]);
    render(<ReferenceAnnotator episodeId="ep-1" fetchImpl={fetchImpl as unknown as typeof fetch} />);

    await fireEvent.change(screen.getByLabelText("注釈コメント（日本語で自由記述）"), {
      target: { value: "色が良い" },
    });
    await fireEvent.click(screen.getByTestId("parse-preview-button"));

    await waitFor(() => {
      expect(screen.getByTestId("domain-chip-color")).toBeTruthy();
    });
    const chip = screen.getByTestId("domain-chip-color");
    expect(chip.className).toContain("chip-like");
    expect(
      (screen.getByTestId("polarity-select-color") as HTMLSelectElement).value,
    ).toBe("like");
    // ドメインスコープの硬化ルールがUI上に明示されている
    expect(screen.getByTestId("domain-scope-rule").textContent).toContain(
      "名前を付けたドメインだけ",
    );
  });

  it("抽出結果を修正すると保存payloadに反映される（修正済みフラグ付き）", async () => {
    const fetchImpl = makeFetch([
      { match: "/references/parse-preview", body: COLOR_DRAFT },
      { match: "/cockpit-api/references", body: REGISTER_OK },
    ]);
    render(<ReferenceAnnotator episodeId="ep-1" fetchImpl={fetchImpl as unknown as typeof fetch} />);

    fireEvent.change(screen.getByLabelText("注釈コメント（日本語で自由記述）"), {
      target: { value: "色が良い" },
    });
    fireEvent.click(screen.getByTestId("parse-preview-button"));
    await waitFor(() => {
      expect(screen.getByTestId("domain-chip-color")).toBeTruthy();
    });

    // 修正: like → dislike
    fireEvent.change(screen.getByTestId("polarity-select-color"), {
      target: { value: "dislike" },
    });
    expect(screen.getByTestId("domain-chip-color").className).toContain("chip-dislike");

    fireEvent.change(screen.getByLabelText("参照動画のパス（ローカルファイル）"), {
      target: { value: "/tmp/ref.mp4" },
    });
    fireEvent.click(screen.getByTestId("save-annotation-button"));

    await waitFor(() => {
      expect(screen.getByTestId("saved-annotation-item")).toBeTruthy();
    });
    const item = screen.getByTestId("saved-annotation-item");
    expect(item.textContent).toContain("色: 嫌い");
    expect(item.textContent).toContain("修正済み");

    const registerCall = (fetchImpl as ReturnType<typeof vi.fn>).mock.calls.find(
      (call) => String(call[0]) === "/cockpit-api/references",
    ) as [string, RequestInit] | undefined;
    expect(registerCall).toBeDefined();
    expect(JSON.parse(registerCall?.[1].body as string)).toEqual({
      path: "/tmp/ref.mp4",
    });

    const stored = JSON.parse(
      sessionStorage.getItem("cockpit.annotations.v1") ?? "[]",
    ) as Array<{ domains: Record<string, string>; corrected: boolean }>;
    expect(stored).toHaveLength(1);
    expect(stored[0].domains).toEqual({ color: "dislike" });
    expect(stored[0].corrected).toBe(true);
  });

  it("A/B比較パネルは既定では出現せず、明示トリガーでのみ表示される", async () => {
    seedTwoReferences();
    const fetchImpl = makeFetch([
      { match: "/references/parse-preview", body: COLOR_DRAFT },
    ]);
    render(<ReferenceAnnotator episodeId="ep-1" fetchImpl={fetchImpl as unknown as typeof fetch} />);

    // 無差別ポップアップ禁止: 通常表示では出ない
    expect(screen.queryByTestId("pairwise-prompt")).toBeNull();

    fireEvent.click(screen.getByTestId("pairwise-trigger"));
    expect(screen.getByTestId("pairwise-prompt")).toBeTruthy();

    // 選択と理由 → 同じ参照API経路のセッションstoreに記録される
    fireEvent.click(screen.getByTestId("pairwise-choice-a"));
    fireEvent.change(screen.getByTestId("pairwise-reason"), {
      target: { value: "Aの方が落ち着いている" },
    });
    fireEvent.click(screen.getByTestId("pairwise-record"));
    await waitFor(() => {
      expect(screen.getByTestId("saved-pairwise-item")).toBeTruthy();
    });
    expect(screen.getByTestId("saved-pairwise-item").textContent).toContain(
      "A を選択",
    );
    const stored = JSON.parse(
      sessionStorage.getItem("cockpit.pairwise.v1") ?? "[]",
    ) as Array<{ choice: string; reference_a_id: string; reference_b_id: string }>;
    expect(stored[0].choice).toBe("a");
    // A = 最新の参照、B = 直前の参照（latest-first）
    expect(stored[0].reference_a_id).toBe("ref-bbb");
    expect(stored[0].reference_b_id).toBe("ref-aaa");
  });

  it("バックエンドの曖昧さヒント（needs_review）でもパネルが出る（無依頼では出ない）", async () => {
    const fetchImpl = makeFetch([
      { match: "/references/parse-preview", body: AMBIGUOUS_DRAFT },
    ]);
    render(<ReferenceAnnotator episodeId="ep-1" fetchImpl={fetchImpl as unknown as typeof fetch} />);

    expect(screen.queryByTestId("pairwise-prompt")).toBeNull();
    fireEvent.change(screen.getByLabelText("注釈コメント（日本語で自由記述）"), {
      target: { value: "全部良い" },
    });
    fireEvent.click(screen.getByTestId("parse-preview-button"));

    await waitFor(() => {
      expect(screen.getByTestId("ambiguity-hint")).toBeTruthy();
    });
    expect(screen.getByTestId("pairwise-prompt")).toBeTruthy();
  });

  it("スコアカード（数値評価入力）は一切存在しない", () => {
    const fetchImpl = makeFetch([]);
    render(<ReferenceAnnotator episodeId="ep-1" fetchImpl={fetchImpl as unknown as typeof fetch} />);
    expect(document.querySelectorAll('input[type="range"]')).toHaveLength(0);
    expect(screen.queryByTestId("scorecard")).toBeNull();
    expect(
      document.querySelectorAll(
        "[aria-label*='評点'], [aria-label*='スコア'], [aria-label*='点数']",
      ),
    ).toHaveLength(0);
  });

  it("空コメントでは抽出ボタンが無効（malformed input）", () => {
    const fetchImpl = makeFetch([]);
    render(<ReferenceAnnotator episodeId="ep-1" fetchImpl={fetchImpl as unknown as typeof fetch} />);
    const parseButton = screen.getByTestId("parse-preview-button") as HTMLButtonElement;
    expect(parseButton.disabled).toBe(true);
    const saveButton = screen.getByTestId("save-annotation-button") as HTMLButtonElement;
    expect(saveButton.disabled).toBe(true);
    expect((fetchImpl as ReturnType<typeof vi.fn>).mock.calls).toHaveLength(0);
  });
});
