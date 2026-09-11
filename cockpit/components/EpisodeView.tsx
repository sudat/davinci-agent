"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import Link from "next/link";
import {
  apiFailure,
  getEpisodeFlags,
  getEpisodeOutputs,
  getEpisodeStatus,
  outputLabel,
  probeEpisodePreview,
  registerOutput,
  CockpitApiError,
  type EpisodeStatus,
  type FlagsPayload,
  type OutputId,
} from "@/lib/api";
import ErrorNotice from "@/components/ErrorNotice";
import EpisodeProgress from "@/components/EpisodeProgress";
import { hasMultipleOutputs, useOutputScope } from "@/components/OutputScope";
import PreviewPlayer, { type PreviewAvailability } from "@/components/PreviewPlayer";
import {
  derivePreviewBinding,
  previewAllowsCompletion,
  previewBindingLine,
  type PreviewProbeObservation,
} from "@/lib/previewBinding";
import FlagList from "@/components/FlagList";
import BeforeAfterSummary from "@/components/BeforeAfterSummary";
import FinishingDomainPanel from "@/components/FinishingDomainPanel";
import ConsultationPanel from "@/components/ConsultationPanel";
import ReviewChatPanel from "@/components/ReviewChatPanel";
import SelfCheckSection from "@/components/SelfCheckSection";
import { useNow } from "@/components/useNow";
import { jobStatusSuffix, formatElapsed, isConsultationStage } from "@/lib/stageGroups";
import { currentStage, type EpisodeStep, type StageHint } from "@/lib/stage-steps";

const POLL_INTERVAL_MS = 2000;
const STALE_AFTER_MS = 15000;

type EpisodeViewProps = {
  episodeId: string;
};

function clockOfEpoch(ms: number): string {
  const at = new Date(ms);
  return [at.getHours(), at.getMinutes(), at.getSeconds()]
    .map((part) => String(part).padStart(2, "0"))
    .join(":");
}

/** 工程3: one honest line for the style pinned at episode start. Absent
 *  on old data → null (render nothing, never 不明 noise). */
function appliedStyleLineOf(status: EpisodeStatus | null): string | null {
  if (status === null) return null;
  const applied: unknown = status.applied_style;
  if (typeof applied !== "object" || applied === null) return null;
  const record = applied as { channel?: unknown; version?: unknown };
  if (typeof record.channel !== "string" || record.channel === "") return null;
  if (typeof record.version !== "number") return null;
  return `使ったスタイル: チャンネル${record.channel}・版${record.version}`;
}

/**
 * Episode status view: polling progress (ETA only when measured), preview
 * player, flagged review items with timestamp jump, and the before/after
 * summary when the payload carries one (task 46).
 *
 * 工程2P poll hardening: every successful status fetch records `fetchedAt`
 * (the 状態取得時刻); failures keep polling but never refresh it. 15s
 * without a successful fetch shows the dedicated 途切れ banner (distinct
 * from the error notice, cleared on recovery) with the 最終取得時刻
 * (unknown-honest when no fetch ever succeeded) and 再照会. The aux
 * fetches (flags / preview probe) are fire-and-track: they must never
 * delay the next poll schedule (codex条件6). visibilitychange/focus
 * trigger an immediate requery.
 *
 * Preview binding: the probe result (or its failure, kept distinct) and the
 * polled status derive ONE binding line under the player — 試し編集が今回の
 * 実行/対象版のものかを推測なしで名乗る。previewOkはその三値写像で、
 * currentのときだけ完了判定に使える。
 */
export default function EpisodeView({ episodeId }: EpisodeViewProps) {
  const [status, setStatus] = useState<EpisodeStatus | null>(null);
  const [flags, setFlags] = useState<FlagsPayload | null>(null);
  /** 動画の機械的な再生可否（player描画とcanSeekの根拠）。probe失敗では
   *  直前の状態を保持する（最後の再生可能videoを消さない）。 */
  const [playerState, setPlayerState] = useState<PreviewAvailability>("checking");
  /** probe観測（成功/失敗を区別して保持）。binding行とpreviewOkの根拠。 */
  const [probeObservation, setProbeObservation] = useState<PreviewProbeObservation | null>(
    null,
  );
  /** 最後にprobe成功で得た再生可能なcontent hash。成功観測がnullなら
   *  nullも保存する（null表示から失敗時に古いhashへ戻さない）。probe失敗
   *  時はこのhashを playerへ渡し続ける — 失敗だけが再生identityを変えない。
   *  playerのkey/srcを変えずvideo要素を作り直さない（再生位置消失を防ぐ）。
   *  今回の対応claim（binding行/previewOk）はprobeObservationのまま即座に
   *  落とす。hashは捏造しない。 */
  const [playableHash, setPlayableHash] = useState<string | null>(null);
  const [error, setError] = useState<{ code: string; detail: string } | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [fetchedAt, setFetchedAt] = useState<number | null>(null);
  /** 工程5: registered outputs (shared with ApprovalSessions via scope).
   *  null = unknown (not yet fetched, or an old backend without the
   *  route) → landscape-only UI with no output noise. The UI never
   *  auto-registers: vertical appears only via the explicit 追加 action. */
  const outputScope = useOutputScope();
  const selectedOutput: OutputId = outputScope.selected;
  const multiOutput = hasMultipleOutputs(outputScope.outputs);
  const verticalRegistered =
    outputScope.outputs?.some((output) => output.output_id === "vertical") ?? false;
  const [addBusy, setAddBusy] = useState(false);
  const [addResult, setAddResult] = useState<string | null>(null);
  const now = useNow(true);
  const [stageHint, setStageHint] = useState<StageHint | null>(null);
  const handleStageHint = useCallback((hint: StageHint) => {
    setStageHint((prev) => {
      if (
        prev !== null &&
        !hint.hasConsultation &&
        !hint.adopted &&
        !hint.hasPublishedSample &&
        !hint.fullAuthorized
      ) {
        return prev;
      }
      return hint;
    });
  }, []);
  useEffect(() => {
    setStageHint(null);
  }, [episodeId]);
  const openedAtRef = useRef<number | null>(null);
  if (openedAtRef.current === null) openedAtRef.current = Date.now();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const requeryRef = useRef<(() => void) | null>(null);

  const seekTo = useCallback((seconds: number) => {
    const video = videoRef.current;
    if (video !== null) video.currentTime = seconds;
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const refreshAux = () => {
      void Promise.allSettled([
        getEpisodeFlags(episodeId, fetch, selectedOutput),
        probeEpisodePreview(episodeId, fetch, selectedOutput),
      ]).then(([flagsResult, previewResult]) => {
        if (cancelled) return;
        if (flagsResult.status === "fulfilled") setFlags(flagsResult.value);
        if (previewResult.status === "fulfilled") {
          setProbeObservation({ kind: "probed", probe: previewResult.value });
          setPlayerState(previewResult.value.available ? "available" : "not_generated");
          if (previewResult.value.available) {
            setPlayableHash(previewResult.value.content_hash);
          }
        } else {
          // probe失敗は専用状態（確認できません）。直前の再生可能videoは
          // 消さず、binding行とpreviewOkだけ即座に今回claimを落とす。
          setProbeObservation({ kind: "failed" });
        }
      });
    };

    const poll = async () => {
      try {
        const next = await getEpisodeStatus(episodeId);
        if (cancelled) return;
        setStatus(next);
        setError(null);
        setFetchedAt(Date.now());
        timer = setTimeout(() => void poll(), POLL_INTERVAL_MS);
        refreshAux();
      } catch (cause) {
        if (cancelled) return;
        setError(apiFailure(cause));
        if (cause instanceof CockpitApiError && cause.status === 404) {
          setNotFound(true);
          return; // stop polling — the episode does not exist
        }
        timer = setTimeout(() => void poll(), POLL_INTERVAL_MS);
      }
    };

    const requery = () => {
      if (cancelled) return;
      if (timer !== undefined) clearTimeout(timer);
      void poll();
    };
    requeryRef.current = requery;

    void poll();
    const onVisibility = () => {
      if (document.visibilityState === "visible") requery();
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", requery);
    return () => {
      cancelled = true;
      requeryRef.current = null;
      if (timer !== undefined) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("focus", requery);
    };
  }, [episodeId, selectedOutput]);

  // 工程5: the outputs list is episode-level metadata — fetched once per
  // episode (never on the poll cadence, never with an output dimension).
  useEffect(() => {
    let cancelled = false;
    void getEpisodeOutputs(episodeId)
      .then((payload) => {
        if (cancelled) return;
        outputScope.setOutputs(payload?.outputs ?? null);
      })
      .catch(() => {
        if (cancelled) return;
        outputScope.setOutputs(null);
      });
    return () => {
      cancelled = true;
    };
    // setOutputs is scope-stable (provider state setter or local fallback).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [episodeId]);

  // 工程5: switching outputs drops the previous output's probe binding —
  // the binding line must never show 横版's claim under 縦版's player.
  useEffect(() => {
    setProbeObservation(null);
    setPlayerState("checking");
    setPlayableHash(null);
    setFlags(null);
  }, [episodeId, selectedOutput]);

  const addVerticalOutput = () => {
    if (addBusy) return;
    setAddBusy(true);
    setAddResult(null);
    void registerOutput(episodeId, "vertical")
      .then((result) => {
        if (result.outputs.length > 0) outputScope.setOutputs(result.outputs);
        setAddResult(
          result.idempotent === true ? "縦版は既に追加済みです" : "縦版を追加しました",
        );
      })
      .catch((cause: unknown) => {
        const failure = apiFailure(cause);
        setAddResult(`縦版を追加できませんでした（${failure.code}）`);
      })
      .finally(() => {
        setAddBusy(false);
      });
  };

  const stale =
    !notFound && now - (fetchedAt ?? openedAtRef.current ?? now) > STALE_AFTER_MS;

  const binding = derivePreviewBinding(probeObservation, status);
  const previewOk = previewAllowsCompletion(binding);
  const appliedStyleLine = appliedStyleLineOf(status);
  // 再生hashは最後の成功観測を保持する: 成功時は今回のhash（nullも保存）、
  // 失敗時は保存済みhash — playerのkey/srcを変えずvideo要素を作り直さない。
  // 未取得の失敗時はnull（固定URL）のまま。
  const probedHash =
    probeObservation?.kind === "probed" ? probeObservation.probe.content_hash : playableHash;

  const eligible = status !== null && isConsultationStage(status.current_stage);
  const step: EpisodeStep = currentStage(stageHint, eligible);
  const showFlagsNormally =
    flags !== null && !flags.not_yet_generated && flags.flags.length > 0;
  const flagsStateLine =
    flags === null
      ? "確認中"
      : flags.not_yet_generated
        ? "まだ生成されていません"
        : `${flags.flags.length}件`;

  const outputControls = multiOutput ? (
    <div data-testid="output-select" role="group" aria-label="出力の切り替え">
      {(outputScope.outputs ?? []).map((output) => (
        <button
          key={output.output_id}
          type="button"
          className={output.output_id === selectedOutput ? "btn-primary" : "btn-small"}
          aria-pressed={output.output_id === selectedOutput}
          data-testid={`output-option-${output.output_id}`}
          onClick={() =>
            output.output_id === "landscape" || output.output_id === "vertical"
              ? outputScope.select(output.output_id)
              : undefined
          }
        >
          {outputLabel(output.output_id)}
        </button>
      ))}
      <p className="field-hint" data-testid="output-independence-note">
        横版と縦版の承認は別々です。表示中のプレビュー・再構築は選択中の版にだけ適用されます。
      </p>
    </div>
  ) : null;

  const footageSlot = (hideUndeterminedBinding: boolean) => (
    <>
      <PreviewPlayer
        episodeId={episodeId}
        state={playerState}
        contentHash={probedHash}
        videoRef={videoRef}
        output={selectedOutput}
      />
      {outputControls}
      {outputScope.outputs !== null && !verticalRegistered ? (
        <div className="actions">
          <button
            type="button"
            className="p2-quiet-action"
            data-testid="output-add"
            onClick={addVerticalOutput}
            disabled={addBusy}
          >
            {addBusy ? "追加中…" : "縦版を追加する"}
          </button>
        </div>
      ) : null}
      {addResult !== null ? (
        <p className="field-hint" aria-live="polite" data-testid="output-add-result">
          {addResult}
        </p>
      ) : null}
      {hideUndeterminedBinding && binding.kind === "unknown" ? null : (
        <p className="field-hint" aria-live="polite" data-testid="preview-binding">
          {previewBindingLine(binding)}
        </p>
      )}
    </>
  );

  const previewSection = (heading: string) => (
    <section className="card">
      <h2 className="card-title">{heading}</h2>
      {multiOutput ? (
        <div data-testid="output-select" role="group" aria-label="出力の切り替え">
          {(outputScope.outputs ?? []).map((output) => (
            <button
              key={output.output_id}
              type="button"
              className={output.output_id === selectedOutput ? "btn-primary" : "btn-small"}
              aria-pressed={output.output_id === selectedOutput}
              data-testid={`output-option-${output.output_id}`}
              onClick={() =>
                output.output_id === "landscape" || output.output_id === "vertical"
                  ? outputScope.select(output.output_id)
                  : undefined
              }
            >
              {outputLabel(output.output_id)}
            </button>
          ))}
          <p className="field-hint" data-testid="output-independence-note">
            横版と縦版の承認は別々です。表示中のプレビュー・再構築は選択中の版にだけ適用されます。
          </p>
        </div>
      ) : null}
      {outputScope.outputs !== null && !verticalRegistered ? (
        <div className="actions">
          <button
            type="button"
            className="btn-small"
            data-testid="output-add"
            onClick={addVerticalOutput}
            disabled={addBusy}
          >
            {addBusy ? "追加中…" : "縦版を追加する"}
          </button>
        </div>
      ) : null}
      {addResult !== null ? (
        <p className="field-hint" aria-live="polite" data-testid="output-add-result">
          {addResult}
        </p>
      ) : null}
      <PreviewPlayer
        episodeId={episodeId}
        state={playerState}
        contentHash={probedHash}
        videoRef={videoRef}
        output={selectedOutput}
      />
      <p className="field-hint" aria-live="polite" data-testid="preview-binding">
        {previewBindingLine(binding)}
      </p>
    </section>
  );

  const detailsBlock = (withStageIds: boolean, withFinishing: boolean, extra?: ReactNode) => (
    <details id="details" className="episode-details" data-testid="episode-details">
      <summary>詳しい記録</summary>
      {extra}
      <dl className="status-list">
        <div>
          <dt>エピソード</dt>
          <dd className="mono">{episodeId}</dd>
        </div>
        <div>
          <dt>ステータス</dt>
          {withStageIds ? (
            <dd data-testid="episode-status">
              {status !== null
                ? `${status.status}${
                    jobStatusSuffix(status.status) !== null
                      ? `（${jobStatusSuffix(status.status)}）`
                      : ""
                  }`
                : "…"}
            </dd>
          ) : (
            <dd>
              {status !== null
                ? `${status.status}${
                    jobStatusSuffix(status.status) !== null
                      ? `（${jobStatusSuffix(status.status)}）`
                      : ""
                  }`
                : "…"}
            </dd>
          )}
        </div>
        <div>
          <dt>現在のステージ</dt>
          {withStageIds ? (
            <dd data-testid="current-stage">
              {status !== null ? status.current_stage : "…"}
            </dd>
          ) : (
            <dd>{status !== null ? status.current_stage : "…"}</dd>
          )}
        </div>
        <div>
          <dt>プレビューの対応</dt>
          <dd>{previewBindingLine(binding)}</dd>
        </div>
        <div>
          <dt>レビューflag</dt>
          <dd data-testid="episode-flags-state">{flagsStateLine}</dd>
        </div>
        {appliedStyleLine !== null ? (
          <div>
            <dt>使ったスタイル</dt>
            <dd>{appliedStyleLine}</dd>
          </div>
        ) : null}
      </dl>
      {withFinishing ? <FinishingDomainPanel episodeId={episodeId} /> : null}
    </details>
  );

  const topNotices = (
    <>
      <Link href="/new-episode" className="top-link">
        ← 新しいエピソード
      </Link>
      {error !== null ? <ErrorNotice code={error.code} detail={error.detail} /> : null}
      {stale ? (
        <div className="card" data-testid="stale-banner">
          <p>状態の更新が途切れています（再接続中）</p>
          <p className="field-hint" data-testid="stale-last-fetch">
            {fetchedAt !== null
              ? `最終取得 ${clockOfEpoch(fetchedAt)}（${formatElapsed(now - fetchedAt)}前）`
              : "最終取得 不明（一度も成功していません）"}
          </p>
          <p className="field-hint">安全な中止は未実装です。</p>
          <button
            type="button"
            className="btn-small"
            data-testid="stale-requery"
            onClick={() => requeryRef.current?.()}
          >
            再照会
          </button>
        </div>
      ) : null}
    </>
  );

  // P3 状況行の正本はここ（全step分岐より上）で求める。方向分岐で先に
  // return されても、止まっている実行の行は方向画面の先頭に出せる。
  const lastFailedRun =
    status !== null
      ? [...status.stage_runs].reverse().find((run) => run.status.startsWith("failed"))
      : undefined;

  /** P3 ブロック行の唯一の正本 — 方向分岐の先頭カードと fallback の
   *  カードで共有する（文言・role・testid を一か所で保つ）。 */
   const STAGE_PLAIN_NAMES: Record<string, string> = {
    analyze: "素材の解析",
    selection: "編集の方向の選定",
    plan: "編集計画",
    compile: "映像の組み立て",
    preview: "プレビュー",
    ingest: "素材の取込み",
    normalize: "素材の正規化",
    intake: "素材の受付",
  };
  const p3BlockedLine =
    lastFailedRun !== undefined ? (
      <p className="p3-blocked-line" role="alert" data-testid="p3-blocked-line">
        {STAGE_PLAIN_NAMES[lastFailedRun.stage_name] ?? "処理"}
        で止まっています。詳しい記録に理由があります。
      </p>
    ) : null;

  if (step === "方向") {
    return (
      <div className="p2-page">
        {topNotices}
        {lastFailedRun !== undefined ? (
          <section className="card p3-stage">
            <header className="p3-stage-header">
              <h2 className="p3-stage-title">処理の状況</h2>
            </header>
            {p3BlockedLine}
          </section>
        ) : null}
        <ConsultationPanel
          episodeId={episodeId}
          status={status}
          footageSlot={footageSlot(false)}
          onStageHint={handleStageHint}
        />
        {showFlagsNormally ? (
          <section className="card p2-flags">
            <h2 className="card-title">レビューflag</h2>
            <FlagList
              flags={flags!.flags}
              notYetGenerated={flags!.not_yet_generated}
              canSeek={playerState === "available"}
              onSeek={seekTo}
            />
          </section>
        ) : null}
        <details className="p2-chat-details">
          <summary>気になるところを伝える</summary>
          <div className="p2-chat">
            <ReviewChatPanel
              episodeId={episodeId}
              getAtSeconds={() => videoRef.current?.currentTime ?? null}
              status={status}
              previewOk={previewOk}
              outputId={selectedOutput}
            />
          </div>
        </details>
        {detailsBlock(true, false)}
      </div>
    );
  }

  if (step === "試し動画") {
    return (
      <div>
        {topNotices}
        <ConsultationPanel
          episodeId={episodeId}
          status={status}
          onStageHint={handleStageHint}
        />
        {detailsBlock(true, false)}
      </div>
    );
  }

  if (step === "全編確認") {
    return (
      <div className="p5-page">
        {topNotices}
        <div className="p5-stage-grid">
          <div className="p5-player">
            <header className="p5-stage-header">
              <h2 className="p5-stage-title">全編の確認用動画</h2>
            </header>
            {footageSlot(true)}
            {showFlagsNormally ? (
              <section className="card p5-flags">
                <h2 className="card-title">レビューflag</h2>
                <FlagList
                  flags={flags!.flags}
                  notYetGenerated={flags!.not_yet_generated}
                  canSeek={playerState === "available"}
                  onSeek={seekTo}
                />
              </section>
            ) : null}
          </div>
          <div className="p5-side">
            <FinishingDomainPanel episodeId={episodeId} summaryOnly />
            <ReviewChatPanel
              episodeId={episodeId}
              getAtSeconds={() => videoRef.current?.currentTime ?? null}
              status={status}
              previewOk={previewOk}
              outputId={selectedOutput}
            />
            <SelfCheckSection
              episodeId={episodeId}
              status={status}
              beforeAfter={<BeforeAfterSummary summary={status?.before_after ?? null} />}
            />
          </div>
        </div>
        <ConsultationPanel
          episodeId={episodeId}
          status={status}
          onStageHint={handleStageHint}
        />
        {detailsBlock(false, true)}
      </div>
    );
  }

  return (
    <div className="p3-page">
      {topNotices}
      <section className="card p3-stage">
        <header className="p3-stage-header">
          <h2 className="p3-stage-title">処理の状況</h2>
        </header>
        {p3BlockedLine}
        <dl className="status-list">
          <div>
            <dt>エピソード</dt>
            <dd className="mono">{episodeId}</dd>
          </div>
          <div>
            <dt>ステータス</dt>
            <dd data-testid="episode-status">
              {status !== null
                ? `${status.status}${
                    jobStatusSuffix(status.status) !== null
                      ? `（${jobStatusSuffix(status.status)}）`
                      : ""
                  }`
                : "…"}
            </dd>
          </div>
          <div>
            <dt>現在のステージ</dt>
            <dd data-testid="current-stage">
              {status !== null ? status.current_stage : "…"}
            </dd>
          </div>
        </dl>
        {appliedStyleLine !== null ? (
          <p className="field-hint" data-testid="applied-style">
            {appliedStyleLine}
          </p>
        ) : null}
        {notFound ? (
          <p className="empty-note">このエピソードは見つかりません。</p>
        ) : status !== null ? (
          <EpisodeProgress status={status} onRequery={() => requeryRef.current?.()} />
        ) : (
          <p className="empty-note">読み込み中…</p>
        )}
      </section>
      <FinishingDomainPanel episodeId={episodeId} summaryOnly />
      <ConsultationPanel episodeId={episodeId} status={status} onStageHint={handleStageHint} />
      <div className="p3-chat">
        <ReviewChatPanel
          episodeId={episodeId}
          getAtSeconds={() => videoRef.current?.currentTime ?? null}
          status={status}
          previewOk={previewOk}
          outputId={selectedOutput}
        />
      </div>
      {detailsBlock(
        false,
        true,
        <>
          {previewSection("全編の確認用動画")}
          <section className="card">
            <h2 className="card-title">レビューflag</h2>
            {flags !== null ? (
              <FlagList
                flags={flags.flags}
                notYetGenerated={flags.not_yet_generated}
                canSeek={playerState === "available"}
                onSeek={seekTo}
              />
            ) : (
              <p className="empty-note">レビューflagの有無を確認しています…</p>
            )}
          </section>
          <BeforeAfterSummary summary={status?.before_after ?? null} />
          <SelfCheckSection episodeId={episodeId} status={status} />
        </>,
      )}
    </div>
  );
}
