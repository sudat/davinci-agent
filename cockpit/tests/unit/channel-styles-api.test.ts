import { describe, expect, it, vi } from "vitest";
import {
  getChannels,
  getChannelStyle,
  restoreChannelStyle,
  saveChannelStyle,
  CockpitApiError,
} from "@/lib/api";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function fetchOf(responder: (url: string) => Response): typeof fetch {
  return (async (input: RequestInfo | URL) =>
    responder(String(input))) as typeof fetch;
}

describe("getChannels（GET /channels）", () => {
  it("channel_idの一覧をパースする", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse(200, { channels: [{ channel_id: "ch-a" }, { channel_id: "ch-b" }] }),
    );
    const list = await getChannels(fetchImpl as unknown as typeof fetch);
    expect(list).toEqual([{ channel_id: "ch-a" }, { channel_id: "ch-b" }]);
    expect(fetchImpl.mock.calls[0]?.[0]).toBe("/cockpit-api/channels");
  });

  it("channels不在・非配列は空扱い（欠落許容）", async () => {
    for (const body of [{}, { channels: null }, { channels: "ch-a" }, null]) {
      const fetchImpl = fetchOf(() => jsonResponse(200, body));
      await expect(getChannels(fetchImpl)).resolves.toEqual([]);
    }
  });

  it("channel_idのない要素は読み飛ばす", async () => {
    const fetchImpl = fetchOf(() =>
      jsonResponse(200, { channels: [{ channel_id: "ch-a" }, {}, { channel_id: "" }, null] }),
    );
    await expect(getChannels(fetchImpl)).resolves.toEqual([{ channel_id: "ch-a" }]);
  });

  it("構造化エラーを型付きで投げる", async () => {
    const fetchImpl = fetchOf(() =>
      jsonResponse(503, { error: { code: "channels-unavailable", detail: "down" } }),
    );
    const cause = await getChannels(fetchImpl).catch((error: unknown) => error);
    expect(cause).toBeInstanceOf(CockpitApiError);
    expect((cause as CockpitApiError).code).toBe("channels-unavailable");
  });

  it("接続失敗は network-error（型付き）", async () => {
    const fetchImpl = (async () => {
      throw new TypeError("fetch failed");
    }) as typeof fetch;
    const cause = await getChannels(fetchImpl).catch((error: unknown) => error);
    expect(cause).toBeInstanceOf(CockpitApiError);
    expect((cause as CockpitApiError).code).toBe("network-error");
  });
});

describe("getChannelStyle（GET /channels/{id}/style）", () => {
  const styleBody = {
    channel_id: "ch-a",
    versions: [
      { version: 1, name: "初版", saved_at: "2026-09-01T00:00:00Z" },
      { version: 2, name: "台所回", saved_at: "2026-09-08T00:00:00Z" },
    ],
    current: 2,
  };

  it("版一覧とcurrentをパースする", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(200, styleBody));
    const style = await getChannelStyle("ch-a", { fetchImpl: fetchImpl as unknown as typeof fetch });
    expect(style.channel_id).toBe("ch-a");
    expect(style.current).toBe(2);
    expect(style.versions).toHaveLength(2);
    expect(style.versions[1]).toEqual({
      version: 1 + 1,
      name: "台所回",
      saved_at: "2026-09-08T00:00:00Z",
    });
    expect(fetchImpl.mock.calls[0]?.[0]).toBe("/cockpit-api/channels/ch-a/style");
  });

  it("version指定は ?version=N を付ける", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(200, styleBody));
    await getChannelStyle("ch-a", { version: 1, fetchImpl: fetchImpl as unknown as typeof fetch });
    expect(fetchImpl.mock.calls[0]?.[0]).toBe("/cockpit-api/channels/ch-a/style?version=1");
  });

  it("versions不在・current不在は空・null扱い（旧データ許容）", async () => {
    const fetchImpl = fetchOf(() => jsonResponse(200, { channel_id: "ch-a" }));
    await expect(getChannelStyle("ch-a", { fetchImpl })).resolves.toEqual({
      channel_id: "ch-a",
      versions: [],
      current: null,
    });
  });

  it("versionのない版要素は読み飛ばす", async () => {
    const fetchImpl = fetchOf(() =>
      jsonResponse(200, {
        channel_id: "ch-a",
        versions: [{ name: "名無し" }, { version: 2, name: "二版", saved_at: "s" }],
        current: 2,
      }),
    );
    const style = await getChannelStyle("ch-a", { fetchImpl });
    expect(style.versions).toHaveLength(1);
    expect(style.versions[0]?.version).toBe(2);
  });
});

describe("saveChannelStyle（POST save）", () => {
  it("201は新規版・idempotent=false", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(201, { version: 3 }));
    const result = await saveChannelStyle(
      "ch-a",
      { name: "台所回" },
      fetchImpl as unknown as typeof fetch,
    );
    expect(result).toEqual({ version: 3, idempotent: false });
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/cockpit-api/channels/ch-a/style/save");
    expect(JSON.parse(init.body as string)).toEqual({ name: "台所回" });
  });

  it("200 + idempotent:true は保存済み扱い", async () => {
    const fetchImpl = fetchOf(() => jsonResponse(200, { version: 2, idempotent: true }));
    await expect(
      saveChannelStyle("ch-a", { name: "台所回" }, fetchImpl),
    ).resolves.toEqual({ version: 2, idempotent: true });
  });

  it("版のない応答は unexpected-response（捏造しない）", async () => {
    const fetchImpl = fetchOf(() => jsonResponse(201, {}));
    const cause = await saveChannelStyle("ch-a", { name: "x" }, fetchImpl).catch(
      (error: unknown) => error,
    );
    expect(cause).toBeInstanceOf(CockpitApiError);
    expect((cause as CockpitApiError).code).toBe("unexpected-response");
  });
});

describe("restoreChannelStyle（POST restore）", () => {
  it("target_versionを送り新版を受け取る", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(201, { version: 3 }));
    const result = await restoreChannelStyle(
      "ch-a",
      1,
      fetchImpl as unknown as typeof fetch,
    );
    expect(result).toEqual({ version: 3 });
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/cockpit-api/channels/ch-a/style/restore");
    expect(JSON.parse(init.body as string)).toEqual({ target_version: 1 });
  });

  it("版のない応答は unexpected-response", async () => {
    const fetchImpl = fetchOf(() => jsonResponse(201, { version: null }));
    const cause = await restoreChannelStyle("ch-a", 1, fetchImpl).catch(
      (error: unknown) => error,
    );
    expect(cause).toBeInstanceOf(CockpitApiError);
    expect((cause as CockpitApiError).code).toBe("unexpected-response");
  });
});
