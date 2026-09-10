import { describe, expect, it } from "vitest";
import { deriveStage, STEPS, type StageSignals } from "@/lib/episode-stage";
import type { EpisodeStatus } from "@/lib/api";
import type { ConsultationPayload, ConsultationSamplesPayload } from "@/lib/api";

function statusOf(overrides: Partial<EpisodeStatus> = {}): EpisodeStatus {
  return {
    episode_id: "ep-stage",
    job_id: "ep-stage",
    status: "CREATED",
    current_stage: "intake",
    created_at_seq: 1,
    updated_at_seq: 1,
    stage_runs: [],
    ...overrides,
  };
}

const EMPTY: StageSignals = { status: null, consultations: null, samples: null };

function consultationsOf(
  overrides: Partial<ConsultationPayload> = {},
): ConsultationPayload {
  return { consultations: [], ...overrides };
}

function samplesOf(count: number, published: boolean): ConsultationSamplesPayload {
  return {
    samples: Array.from({ length: count }, (_, index) => ({
      sample_id: `s-${index}`,
      status: published ? "published" : "stored",
      total_seconds: 30,
      created_at: "2026-01-01T00:00:00Z",
    })),
  };
}

describe("episode-stage（上部4段階の導出）", () => {
  it("STEPS は 素材→方向→試し動画→全編確認 の4段階", () => {
    expect([...STEPS]).toEqual(["素材", "方向", "試し動画", "全編確認"]);
  });

  it("何もなければ素材", () => {
    expect(deriveStage(EMPTY)).toBe("素材");
  });

  it("相談があれば方向", () => {
    expect(
      deriveStage({
        ...EMPTY,
        status: statusOf({ current_stage: "selection" }),
        consultations: consultationsOf({
          consultations: [
            {
              consultation_id: "c-1",
              created_at: "2026-01-01T00:00:00Z",
              message: "相談",
              proposals: [],
              judgments: [],
              budget: {
                llm_calls_used: 0,
                llm_calls_limit: 1,
                intervals_used: 0,
                intervals_limit: 1,
                wall_seconds_used: 0,
                wall_seconds_limit: 1,
                cost_display: "",
              },
            },
          ],
        }),
      }),
    ).toBe("方向");
  });

  it("採用方針があれば試し動画（chain が selection でも上位を取る）", () => {
    expect(
      deriveStage({
        ...EMPTY,
        status: statusOf({ current_stage: "selection" }),
        consultations: consultationsOf({
          policy: {
            adopted: {
              consultation_id: "c-1",
              judgment_id: "j-1",
              proposal_id: "p-1",
              decision: "adopt",
              scope: { composition: true, appearance: true, audio: true },
              audience_message: "",
              structure: "",
              duration_estimate: "",
              candidate_scenes: [],
              subtitle_policy: "",
              audio_policy: "",
              tempo_policy: "",
              reference_mapping: "",
              unused_reasons: "",
              unconfirmed: [],
              note: "",
            },
          },
        }),
      }),
    ).toBe("試し動画");
  });

  it("公開済み試し動画があれば試し動画", () => {
    expect(
      deriveStage({ ...EMPTY, samples: samplesOf(1, true) }),
    ).toBe("試し動画");
  });

  it("full_authorized 判断があれば全編確認", () => {
    expect(
      deriveStage({
        ...EMPTY,
        status: statusOf({ current_stage: "preview" }),
        consultations: consultationsOf({
          consultations: [
            {
              consultation_id: "c-1",
              created_at: "2026-01-01T00:00:00Z",
              message: "相談",
              proposals: [],
              judgments: [
                {
                  judgment_id: "j-1",
                  proposal_id: null,
                  decision: "full_authorized",
                  scope: { composition: true, appearance: true, audio: true },
                  note: null,
                  created_at: "2026-01-01T00:00:00Z",
                },
              ],
              budget: {
                llm_calls_used: 0,
                llm_calls_limit: 1,
                intervals_used: 0,
                intervals_limit: 1,
                wall_seconds_used: 0,
                wall_seconds_limit: 1,
                cost_display: "",
              },
            },
          ],
        }),
      }),
    ).toBe("全編確認");
  });

  it("仕上げ chain 段階なら全編確認", () => {
    expect(
      deriveStage({ ...EMPTY, status: statusOf({ current_stage: "render" }) }),
    ).toBe("全編確認");
  });

  it("本人確認の回答があれば全編確認", () => {
    expect(
      deriveStage({
        ...EMPTY,
        status: statusOf({
          self_check: {
            episode_id: "ep-stage",
            answered_at: "2026-01-01T00:00:00Z",
            q_instruction_transmitted: true,
            q_better_than_before: true,
            q_want_to_publish: true,
            note: "",
          },
        }),
      }),
    ).toBe("全編確認");
  });
});
