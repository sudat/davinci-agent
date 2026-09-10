"use client";

import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  apiFailure,
  CockpitApiError,
  getSelfCheck,
  postSelfCheck,
  type EpisodeStatus,
  type SelfCheckAnswer,
} from "@/lib/api";
import type { FetchLike } from "@/lib/http";

type SelfCheckSectionProps = {
  episodeId: string;
  status: EpisodeStatus | null;
  fetchImpl?: FetchLike;
  /** 2問目（前より良くなった）の下に置く修正前後の要約。 */
  beforeAfter?: ReactNode;
};

type Answers = {
  q_instruction_transmitted: SelfCheckAnswer;
  q_better_than_before: SelfCheckAnswer;
  q_want_to_publish: SelfCheckAnswer;
};

const EMPTY_ANSWERS: Answers = {
  q_instruction_transmitted: null,
  q_better_than_before: null,
  q_want_to_publish: null,
};

const QUESTIONS: { key: keyof Answers; label: string; testId: string }[] = [
  {
    key: "q_instruction_transmitted",
    label: "①思った通りに伝わった",
    testId: "self-check-q1",
  },
  {
    key: "q_better_than_before",
    label: "②前より良くなった",
    testId: "self-check-q2",
  },
  {
    key: "q_want_to_publish",
    label: "③このまま公開したい",
    testId: "self-check-q3",
  },
];

function answerLabel(answer: SelfCheckAnswer): string {
  if (answer === true) return "はい";
  if (answer === false) return "いいえ";
  return "未回答";
}

export default function SelfCheckSection({
  episodeId,
  status,
  fetchImpl,
  beforeAfter = null,
}: SelfCheckSectionProps) {
  const [supported, setSupported] = useState<boolean | null>(null);
  const [answers, setAnswers] = useState<Answers>(EMPTY_ANSWERS);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<{ code: string; detail: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    const fetchFn = fetchImpl ?? fetch;
    void getSelfCheck(episodeId, fetchFn)
      .then((payload) => {
        if (cancelled) return;
        setSupported(true);
        if (payload.self_check !== null) {
          setAnswers({
            q_instruction_transmitted: payload.self_check.q_instruction_transmitted,
            q_better_than_before: payload.self_check.q_better_than_before,
            q_want_to_publish: payload.self_check.q_want_to_publish,
          });
          setNote(payload.self_check.note);
        }
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        if (cause instanceof CockpitApiError && cause.status === 404) {
          setSupported(false);
          return;
        }
        setSupported(true);
      });
    return () => {
      cancelled = true;
    };
  }, [episodeId, fetchImpl]);

  useEffect(() => {
    const saved = status?.self_check;
    if (saved === undefined || saved === null || sent) return;
    setAnswers({
      q_instruction_transmitted: saved.q_instruction_transmitted,
      q_better_than_before: saved.q_better_than_before,
      q_want_to_publish: saved.q_want_to_publish,
    });
    setNote(saved.note);
  }, [status, sent]);

  if (supported !== true) return null;

  const choose = (key: keyof Answers, value: SelfCheckAnswer) => {
    setAnswers((prev) => ({ ...prev, [key]: value }));
  };

  const submit = () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    void postSelfCheck(
      episodeId,
      {
        q_instruction_transmitted: answers.q_instruction_transmitted,
        q_better_than_before: answers.q_better_than_before,
        q_want_to_publish: answers.q_want_to_publish,
        note,
      },
      fetchImpl ?? fetch,
    )
      .then(() => {
        setSent(true);
      })
      .catch((cause: unknown) => {
        setError(apiFailure(cause));
      })
      .finally(() => {
        setBusy(false);
      });
  };

  return (
    <section className="card" data-testid="self-check-section">
      <details>
        <summary>最後の確認</summary>
        <p className="field-hint">
          分からない質問は「未回答のまま」でかまいません。未回答は未確認として残り、成功扱いにしません。
        </p>
        {QUESTIONS.map((question) => (
          <div key={question.key} data-testid={question.testId} role="group" aria-label={question.label}>
            <p>{question.label}</p>
            <div className="actions">
              {(
                [
                  { value: true, label: "はい" },
                  { value: false, label: "いいえ" },
                  { value: null, label: "未回答のまま" },
                ] as { value: SelfCheckAnswer; label: string }[]
              ).map((option) => (
                <button
                  key={option.label}
                  type="button"
                  className={answers[question.key] === option.value ? "btn-primary" : "btn-small"}
                  aria-pressed={answers[question.key] === option.value}
                  data-testid={`${question.testId}-${option.label === "はい" ? "yes" : option.label === "いいえ" ? "no" : "unanswered"}`}
                  onClick={() => choose(question.key, option.value)}
                >
                  {option.label}
                </button>
              ))}
            </div>
            <p className="field-hint" data-testid={`${question.testId}-current`}>
              現在の回答: {answerLabel(answers[question.key])}
            </p>
            {question.key === "q_better_than_before" && beforeAfter !== null ? (
              <div data-testid="self-check-before-after">{beforeAfter}</div>
            ) : null}
          </div>
        ))}
        <label className="field" htmlFor="self-check-note">
          メモ（自由記入）
          <input
            id="self-check-note"
            type="text"
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="気づいたことがあれば書きます"
            data-testid="self-check-note"
          />
        </label>
        <div className="actions">
          <button
            type="button"
            className="btn-small"
            onClick={submit}
            disabled={busy}
            data-testid="self-check-submit"
          >
            {busy ? "送信中…" : "送信"}
          </button>
        </div>
        {sent ? (
          <p className="field-hint" data-testid="self-check-sent">
            送信しました
          </p>
        ) : null}
        {error !== null ? (
          <p className="field-error" role="alert" data-testid="self-check-error">
            {error.code}: {error.detail}
          </p>
        ) : null}
      </details>
    </section>
  );
}
