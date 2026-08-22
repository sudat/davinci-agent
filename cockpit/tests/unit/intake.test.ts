import { describe, expect, it } from "vitest";
import {
  canCreateEpisode,
  classifyReference,
  validateIntake,
} from "@/lib/intake";

describe("canCreateEpisode（intake必須チェック）", () => {
  it("両方空なら作成不可", () => {
    expect(canCreateEpisode({ sourceFolder: "", briefText: "" })).toBe(false);
  });

  it("ソースのみ・briefのみなら作成不可", () => {
    expect(canCreateEpisode({ sourceFolder: "/tmp/a", briefText: "" })).toBe(
      false,
    );
    expect(canCreateEpisode({ sourceFolder: "", briefText: "説明" })).toBe(
      false,
    );
  });

  it("空白文字のみは空扱い（malformed input）", () => {
    expect(
      canCreateEpisode({ sourceFolder: "   ", briefText: "\n\t " }),
    ).toBe(false);
    expect(
      canCreateEpisode({ sourceFolder: "  /tmp/a  ", briefText: "  説明  " }),
    ).toBe(true);
  });

  it("両方ありなら作成可", () => {
    expect(
      canCreateEpisode({ sourceFolder: "/tmp/a", briefText: "説明" }),
    ).toBe(true);
  });
});

describe("validateIntake（フィールド単位エラー）", () => {
  it("両方空なら両方のエラーメッセージ", () => {
    const errors = validateIntake({ sourceFolder: "", briefText: "" });
    expect(errors.sourceFolder).toBeDefined();
    expect(errors.briefText).toBeDefined();
  });

  it("briefだけ埋まればsourceFolderのエラーのみ", () => {
    const errors = validateIntake({ sourceFolder: "", briefText: "説明" });
    expect(errors.sourceFolder).toBeDefined();
    expect(errors.briefText).toBeUndefined();
  });
});

describe("classifyReference", () => {
  it("http/httpsはURL、絶対パスと~はローカル、その他は保存済み", () => {
    expect(classifyReference("https://youtu.be/x")).toBe("url");
    expect(classifyReference("/Users/me/ref.mp4")).toBe("local");
    expect(classifyReference("~/movies/ref")).toBe("local");
    expect(classifyReference("ref-2026-08")).toBe("saved");
  });
});
