# 工程2.5 slice2 P1修正 — 事前提示設計表（codex指摘対応）

対象: HEAD=b469a1a の slice2 実装に対する独立レビュー指摘（P1-1〜P1-4、安全2件）の修正契約。
由来: Oracle設計コンサルテーション（2026-09-09）、実装・検証は未実施。

## 概要表

| Finding | 要求されるbackend契約 | 受入証拠 |
|---|---|---|
| P1-1 冪等性 | 最新の同一有効採用は既存の judgment_id・再build・outcome・確定版を再利用する | 同一POST再送で journal行・spawn・model呼出・plan版の追加がすべて0 |
| P1-2 予約の固定 | 予約は判断・採用範囲・方針hash・基準版・基準plan hashを固定。director前とcommit直前に鮮度検査 | A→B交錯でB現行後にAが確定しない |
| P1-3 予算 | 提案呼出と選択再build呼出を含む単一の累積エピソード予算。内部予約台帳が有料呼出・壁時間・見本30秒を保護 | 予算枯渇でdirector・rendererに接触しない。失敗も課金記録され可視 |
| P1-4 正直なoutcome | 新規結果は connected または failed のみ。接続証拠・構造検証・未対応・未確認は別フィールド | 証拠なしに「方針を実現した/反映した」を出さない |
| 安全-1 権限 | 再buildは継承した runner.lock の所有を証明し、frozen・非PREVIEW_READYを拒否し、予約した基準版に対してのみcommit | lock欠落・frozen・基準版ずれで event/version 0件 |
| 安全-2 部分書込 | policy commitは読込前と書込失敗後に既存の orphan recovery を起動し、回復済みまたは部分記録の版を正直に報告 | event append後の失敗で v2 を回復または v2 記録済みと報告。「反映されていません」の一律表示は廃止 |

現状の根拠: APIは毎回新UUIDを生成してからschedule判断 (`api_consultation.py:398`)。`_run_stage` は予約ではなく最新policyを読む (`episode_runner_rebuild.py:493`)。`commit_policy` は常に `head.version + 1` を計算し `idempotent=False` を返す (`policy_commit.py:86-127`)。

## P1-1: 同一採用の冪等性

### 採用の同一性

有効な採用 = `(consultation_id, proposal_id, decision, scope.composition, scope.appearance, scope.audio, note)` の完全一致（正規化なし・`""`↔`None` 変換なし・履歴検索なし）。比較対象は「最新のjudgment行」のみ（policy抽出器も最終行を権威とするため、`consultation_store.py:316-350`）。

重複排除を適用する条件: `decision ∈ {adopt, revise}` かつ `proposal_id is not None` かつ scopeフラグ1つ以上true。

検証順序は維持: (1) request-schema失敗は422 (2) 不明consultationは404 `consultation-not-found` (3) 不明proposalは422 `consultation-proposal-not-found` (4) その後に最新judgmentの指紋比較。

`append_effective_judgment_once(...) -> tuple[ConsultationJudgmentV1, bool]` を追加。`appended=True` のときだけ judgment_id / created_at を生成。完全再送は既存オブジェクトを書込ゼロで返す。

### HTTP結果

完全再送は `record_consultation_rebuild` を呼ばない。viewは元の judgment_id を返し、HTTPは既存rebuild状態から決定: `requested|running` → 202、`none|succeeded|failed` → 200。重複経路が予約追記・spawn・予算予約・outcome追記・plan版を作ってはならない。

spawnが予約書込後に失敗した場合、`reserves_sequence` + `failure_code` + 日本語detailを持つ終端rebuild行を追記する（未spawn予約が `requested` と誤読される現状 `consultation_store.py:486-507` の是正）。

### commitの冪等性

policy payload parserを拡張し `judgment_id / proposal_id / decision / plan` を露出（fieldsは全 `policy_applied` payloadに既存、`events.py:281-298`。現parserはplanのみ返す `events.py:301-325`）。

`commit_policy(..., expected_base_version, expected_base_plan_sha256)` は:
1. `recover_orphan` を実行
2. headを読み込み検証
3. 同一 `judgment_id` の既存 `policy_applied` を検索
4. linkageと正準plan SHAが同一なら indexの版を検証し元の `CommitOutcome` を `idempotent=True` で返却
5. 同一judgmentで proposal/decision/plan SHAが異なるなら書込前に `policy-idempotency-conflict`
6. 存在しなければ期待基準版・hashを強制してから新eventを構築

`stage_selection` は予算予約・director呼出の前にこの既存event検索を行う（crash-resumeが再課金・再生成しない）。

outcome追記も冪等化: キー `(judgment_id, reservation_sequence, status, commit_event_id, failure_code)`。最新outcomeが同一キーなら追記せず返す。

## P1-2: 予約の固定と陳腐化拒否

`RebuildRequestEntry` に後方互換の任意フィールドを追加:

```text
policy_scope: ["composition"|"appearance"|"audio", ...] | null
policy_sha256: sha256 | null
base_plan_version: "vN" | null
base_plan_sha256: sha256 | null
failure_code: string | null
detail: string | null
```

新規相談予約は書込境界で4ピン全部必須。旧形式行はparse可だが実行は `consultation-reservation-unpinned` で失敗。`policy_sha256` は正準 `AdoptedPolicyV1` バイトのSHA-256。`base_plan_sha256` は現在の版entry記録の `plan_sha256`（新規計算ではない）。

spawnコマンドに `--reservation-sequence <N>` を含め、子はその正確な予約を読む（`consultation-{judgment_id}` や後のspawn行から推測しない）。

`policy_for_judgment(episode_dir, judgment_id)` を追加: IDで正確に1つのjudgmentを見つけ、新しさと無関係に提案と結合。予約hashは後の提案再生成による本文変化を拒否する。

### 予約時検査（予約追記前）

1. 指定judgmentが有効な採用policyに解決する
2. `latest_adopted_policy().judgment_id` が指定judgmentと一致
3. job状態が正確に `PREVIEW_READY`（`FROZEN` は `job-frozen`、他は `episode-not-preview-ready`）
4. review-store headを読み版とplan hashを記録
5. 正準scope順 `composition, appearance, audio` を記録
6. 同一judgmentの予約が既存なら追加行・spawnなしで既存状態を返す

### 実行時検査

同じ `assert_reservation_fresh` を (1) director呼出直前 (2) model待ち・plan導出後のcommit直前 で実行。検証内容:

```text
予約が存在し完全にピンされている / 指定judgmentが解決する / policy SHAとscopeが予約と一致 /
最新採用judgmentが予約judgmentのまま / review-store headの版とSHAが予約基準と一致 /
jobがPREVIEW_READYかつFROZENでない / runnerがepisode lockを保持 / 壁時間deadline未超過
```

基準版検査は「同一policy eventが既存で冪等返却される場合」のみ回避可。

失敗コードと表示:

| コード | 表示 |
|---|---|
| `consultation-reservation-not-found` | 作り直しの予約記録が見つからないため、編集を始めませんでした。 |
| `consultation-reservation-unpinned` | 古い予約には対象の判断と編集版の記録が足りないため、安全に再開できません。 |
| `reserved-policy-changed` | 予約した判断が変わりました。古い判断の編集は確定していません。 |
| `policy-base-version-changed` | 予約後に編集の版が変わりました。古い版を上書きしていません。 |
| `job-frozen` | この動画は確定済みのため、作り直しを行いませんでした。 |

失敗時にfailed outcomeを1件追記してselection stageを停止。commitせず、Bへ自動切替せず、最新policyを黙って使用しない。

A→B競合は実在: 既存APIはAのrebuild実行中にBを記録するがBをscheduleしない (`test_consultation_policy_api.py:197-235`)。よってAは「BがA開始前に来た場合」と「Aのmodel呼出中に来た場合」の両方で失敗しなければならない。

## P1-3: 累積選択予算

既定値は6呼出・3区間・600秒 (`config/consultation.json:1`)。既存予算はresetなしのepisode累積 (`consultation_store.py:561-577`) だが、rebuildは相談予算を消費しないと宣言している (`episode_files.py:739-749`)。

`ConsultationBudgetLimits` とconfigに追加的に拡張:

```json
{ "schema_version": "cockpit-consultation-config-v1",
  "llm_calls_limit": 6, "intervals_limit": 3, "wall_seconds_limit": 600.0,
  "preview_sample_seconds_limit": 30.0 }
```

第二の独立した呼出・壁時間上限を追加しない。提案生成と選択再buildは同じepisode合計を消費する（「同一相談」最低要件より厳しい）。

内部runtime journal `consultation/selection-budget.jsonl`（authoritative artifactではない）。実装は `consultation_selection_budget.py`・250 pure LOC以下。entry:

```text
attempt_id = "selection-{reservation_sequence}" / consultation_id / judgment_id / reservation_sequence /
phase = director_reserved | director_settled | preview_reserved | preview_settled /
llm_calls_reserved/used / wall_seconds_reserved/used / preview_seconds_reserved/used /
result = succeeded|failed|uncertain|null / failure_code / created_at
```

foldは未一致の予約を保守的に「まだ予約中」と扱う（crash後の再実行が有料試行を無料とみなさない）。

### 検査・消費点

1. **director前**: pin/lock/状態検査を先に。呼出1回分の残りと、既存のdirector全力 allowance（live transportは120秒有界 `live_editorial.py:38-44, 211-234`）以上の壁時間を要求
2. **接触前予約**: director呼出前に呼出1回と残り壁時間許容量で `director_reserved` を追記
3. **finallyで精算**: 成功・失敗に関係なく `director_settled`（llm_calls_used=1・実経過時間）
4. **commit/preview予約前**: planに束ねられた見本尺をframe数とframe rateから正確に計算。累積が30秒を超えるならcommit・renderer開始前に拒否
5. **renderer前**: `preview_reserved` 追記、鮮度と残り壁時間を再検査し、残りdeadlineをpreview rendererと全ての有界ffmpeg/ffprobe呼出に伝播（renderer既定はffmpeg 600秒・probe 120秒で、consultation全体deadlineを強制しない `preview/tools.py:25-27`, `preview/render.py:205-236`）
6. **preview精算**: render開始後は失敗しても予定見本尺を課金し実壁時間を記録
7. **resume時の開放予約**: 正確なpolicy commitが無い場合、有料呼出を繰り返さない。`consultation-selection-attempt-uncertain` を出す

失敗コードと表示:

| コード | 表示 |
|---|---|
| `consultation-selection-budget-exhausted` | この相談で使えるAI回数または処理時間の上限に達したため、作り直しを始めませんでした。 |
| `consultation-preview-budget-exhausted` | 見本映像は合計30秒の上限を超えるため、映像生成を始めませんでした。 |
| `consultation-selection-deadline-exceeded` | この相談の処理時間上限に達したため、続きを確定していません。 |
| `consultation-selection-attempt-uncertain` | 前回のAI処理が完了したか確認できないため、重複利用を避けて再実行を止めました。 |

現在のpreviewはplan全体に束ねられ完全timeline coverageを検証する (`preview/models.py:169-188`, `preview/render.py:192-235`)。よって尺超過時は**fail-closed**（bundleを全plan束縛のまま黙って切り詰めない）。

## P1-4: 接続と実現の分離

現状のoutcome語彙は `honored|failed` のみ (`consultation_store.py:184-205`)で、`stage_selection` は planner/compile/store再読込後に `honored` を書く (`episode_runner_rebuild.py:221-263`)。これらは構造検証であって方針の意味的遵守の証拠ではない。

status unionを拡張: `honored`（旧読取専用・新writerは発行しない）/ `connected` / `failed`。任意フィールド追加:

```text
reservation_sequence / run_id / commit_event_id / failure_code /
director_connection: confirmed|not_started|unknown|null /
director_request_hash / policy_prompt_sha256 /
connected_fields: string[] / realized_checks: string[] / unaddressed: string[] / unconfirmed: string[]
```

`director_connection` 規則: `confirmed`=ピンしたpolicyを含むrequestへのlive応答が返った / `not_started`=予算・lock・状態・予約・鮮度検査で呼出前に停止 / `unknown`=transport試行したが配達を証明する応答なし。

`AdoptedPolicySummaryV1` とpromptに `audience_message`, `duration_estimate`, `reference_mapping` を追加（現summaryはこの3つを省略 `consultation_store.py:371-389`, `editorial/models.py:61-79`、promptは構成・場面・字幕・音・テンポ・不用理由・未知・noteのみ `editorial/prompt.py:58-94`）。

成功したv1導出が記録してよい構造検証は正確にこの5つ: `planner_feasibility`, `edit_plan_generation`, `production_compile`, `review_projection`, `review_store_round_trip`（`episode_runner_selection.py:169-235` の solve/generate/compile_ir/project_plan と `episode_runner_rebuild.py:235-243` の再読込に対応）。意味的遵守検証として再ラベルしない。

`unaddressed` は採用scopeから: 構成=構成・候補場面・想定尺・参考対応・テンポが方針の意味どおりか / 見た目=字幕と見た目が実映像で方針どおりか / 音=BGM・音量・音付きテンポが方針どおりか。`unconfirmed` は提案の既存 `unconfirmed` + `試し編集を本人が見て方針どおりか`（順序保存・完全重複除去）。

UI文字列（frontend修正時に適用）:

```text
connected: 採用した方針は編集長への入力に接続されました（対象版 vN）。内容どおりに実現したかは、確認済みの項目だけを表示しています。
failed+confirmed: 方針は編集長に渡されましたが、編集版の確定に失敗しました。
failed+not_started: 方針は編集長に渡していません。理由を確認して相談へ戻れます。
failed+unknown: 方針が編集長へ届いたか確認できません。重複利用を避けて停止しました。
```

現frontendの `反映されました` は `status === "honored"` から直接派生 (`ConsultationEntryList.tsx:35-49`)。frontendが `connected` を受容するまでの防御的fallbackは `不明` 表示であり、それは誤った成功主張より望ましい。

## Story→Moment→Creative 対応表（開示）

PRDはstory理解・moment順位・提示決定の分離のために3passを保持 (`PRD_v4.4.md:866-875`)。sliceは現在 v1 `select_and_reconcile` を1回呼ぶ (`real_director.py:252-284`)。v2は独立A/B/C passを持つが未使用 (`director_v2.py:81-180`)。

| 段階 | 現v1のカバー | v2三passの目標 | 不足 | 実現時期 |
|---|---|---|---|---|
| Story | 方針の構成テキストを単一選択promptへ渡す。出力schemaはsegment・selected/dropped・理由codeのみ (`editorial_model.py:29-43`) | Pass Aがbriefとevidence digestからStoryPlanDraftを返す (`prompt_v2.py:8-13`) | story block・順序・目的・素材対応をこのsliceは生成しない | 相談方針をPass Aに加え、返却StoryPlanを検証・版束縛した後 |
| Moment | v1は宣言segment IDを選択/除外し、決定論的にevidence付き候補へ調整 (`real_director.py:271-279`, `editorial/reconcile.py:1-10`) | Pass BはStoryPlan・候補・EvidenceBundleV2・除去適格性を既知ID/deep-review gate付きで消費 (`director_v2.py:145-166`) | v1がv2のMoment pass・deep-review keep統合・story対応を実行した証明なし | rebuildがPass Bを起動し使用候補・証拠参照を永続化・検証した後 |
| Creative | 下流は決定論的にsolve・generate・compile・project (`episode_runner_selection.py:182-235`)。v1出力は音・字幕・提示intentを表現できない (`editorial_model.py:35-43`) | Pass CがCreativeEditDraftを返し未知targetを拒否 (`director_v2.py:168-180`) | compile成功はplan妥当性の証明であって外観・音の方針実現の証明ではない | Pass CをCreative Plan/IR経路に接続し音付きpreviewと作業者確認を記録した後 |

今回の修正で許される主張は唯一「ピンした方針がv1 director requestに接続され、列挙した構造検証を通過した」。Story・創造的実現・主観品質は未確認のまま。

## 安全-1: 変更権限

episode lockは `<episode>/runner.lock`・非blocking排他flock。descriptorは `pass_fds` で子に継承されexit/crashでkernel解放 (`episode_ops.py:46-78`)。子は今descriptor引数を受けないため権限を主張できない。

全spawn runnerコマンドに `--runner-lock-fd` を追加。`_run_inner` 入口で:
1. `fstat(fd)` と `stat(runner.lock, follow_symlinks=False)` が同一regular file
2. 継承file descriptionに `LOCK_EX | LOCK_NB` を再主張
3. descriptorをrunner exitまで保持
4. 欠落・閉鎖・inode不一致・symlink・unlock不能は `runner-lock-not-held`

直接test呼出は明示的に実lock descriptorを取得して渡す。本番再入はそれ無しではfail-closed。

`FROZEN` は既にterminal状態として存在し遷移は変更を禁止 (`state_models.py:26-39`, `transitions.py:1-10`)。予約時・director前・commit前に状態検査。

`commit_policy` は予約基準版とSHAを受ける。冪等再検出の後、不一致は `policy-base-version-changed`。event・plan・IR・index書込禁止。現writerはoperator-intent検査のみで読み込んだheadに対しcommitする (`policy_commit.py:81-104`)。

## 安全-2: commit途中回復

既存recoveryは `policy_applied` を含み、event stream fold・欠損plan/IR再構築・index書換を行う (`commit.py:208-251`)。reducerもpolicy eventを版生成operator決定として扱う (`reducer.py:247-256`)。不足はpolicy経路からの呼出しのみ。

`commit_policy` の要求動作:
1. 最初の `load_head` 前に `recover_orphan`
2. `append_events` 後の plan・IR・index書込をwrap
3. `OSError` / `ReviewCommitError` 時に `recover_orphan` → head再読込 → 正確なpolicy eventを特定
4. 回復が期待版・hashを生成したなら `CommitOutcome(idempotent=True)` を返す
5. 回復失敗でも封印policy eventが `result_plan_version` を露出するなら、その版を担う `policy-commit-recovery-failed`
6. 封印eventが無ければ「版なし」を報告

現行の一律catchは常に `方針は反映されていません` を書く (`episode_runner_rebuild.py:243-249`)。置換:

```text
封印eventなし: 版の確定前に失敗しました。方針を反映した版はありません。
封印event vN・回復未確認: 編集の記録は vN まで残っていますが、版ファイルの回復を確認できません。自動で「反映されていない」とは判定しません。
```

failed outcomeは非null `plan_version` を持ち得る。`derive_policy_rebuild` はそれを `target_version` として保存（現在は失敗outcomeで常にNone `consultation_store.py:480-485`）。

## 回帰試験（42件）

1. `test_append_effective_judgment_once_reuses_latest_exact_adoption` — 同一オブジェクト返却・judgment行1行
2. `test_append_effective_judgment_once_appends_when_any_fingerprint_field_differs` — proposal/decision/各scope/noteをパラメータ化
3. `test_duplicate_adoption_post_returns_existing_202_without_side_effects` — 同一judgment ID・journal/spawn/budget行数不変
4. `test_duplicate_completed_adoption_post_returns_existing_200_without_v3` — 完了済v2がhead維持・director/spawnなし
5. `test_duplicate_adoption_still_validates_consultation_and_proposal_first` — 404/422コード維持
6. `test_spawn_failure_records_terminal_rebuild_state` — 失敗spawnはfailed（孤立requestedにならない）
7. `test_commit_policy_same_linkage_and_plan_returns_original_idempotently`
8. `test_commit_policy_same_judgment_with_different_plan_refuses` — `policy-idempotency-conflict`・バイト不変
9. `test_same_reservation_rerun_reuses_commit_without_director` — v2のまま・予算不変
10. `test_consultation_reservation_pins_scope_policy_and_base` — 全ピンfieldと `--reservation-sequence`
11. `test_policy_for_judgment_loads_exact_nonlatest_judgment` — B後に正確なAを再構成
12. `test_reservation_a_then_b_before_director_fails_closed` — director 0回・policy eventなし・日本語陳腐化文言
13. `test_reservation_a_then_b_during_director_fails_before_commit` — director 1回・head不変・`director_connection=confirmed`
14. `test_reject_after_reservation_cancels_stale_run` — `reserved-policy-changed`・commitなし
15. `test_head_change_during_director_refuses_reserved_base` — `policy-base-version-changed`
16. `test_legacy_consultation_reservation_without_pins_fails_closed`
17. `test_selection_budget_exhausted_stops_before_director` — director 0回・plan編集0・failed outcome
18. `test_director_failure_settles_call_and_wall_usage` — 失敗でも1課金呼出・非零壁時間
19. `test_open_director_reservation_blocks_second_paid_attempt` — `consultation-selection-attempt-uncertain`
20. `test_proposal_budget_view_includes_selection_usage` — 合計がepisode累積維持
21. `test_preview_seconds_accumulate_across_rebuilds` — 合計30秒超なし
22. `test_preview_over_remaining_budget_stops_before_commit_and_render` — renderer 0回・新版なし
23. `test_preview_failure_consumes_reserved_sample_seconds`
24. `test_wall_deadline_is_forwarded_to_director_and_preview_subprocesses` — 全timeout ≤ 残り壁時間
25. `test_deadline_expiry_after_director_blocks_commit` — 使用精算・版なし
26. `test_connected_outcome_records_exact_policy_prompt_binding` — request hash・prompt SHA・fields・reservation・event ID
27. `test_connected_outcome_lists_only_structural_realized_checks` — 正確な5検証語彙・意味的遵守文言なし
28. `test_connected_outcome_lists_unaddressed_by_scope` — 構成/見た目/音のパラメータ化
29. `test_failure_before_director_records_not_started`
30. `test_transport_failure_records_unknown_connection`
31. `test_new_writer_never_emits_honored_but_legacy_honored_still_loads`
32. `test_reentry_without_inherited_runner_lock_refuses` — `runner-lock-not-held`・書込0
33. `test_reentry_with_inherited_runner_lock_reaches_selection` — 有効inode/lock受容
34. `test_frozen_job_refuses_before_director_and_commit` — `job-frozen`・呼出/event 0
35. `test_commit_policy_expected_base_mismatch_writes_nothing` — event log・seal・files・indexがバイト同一
36. `test_policy_plan_write_failure_recovers_orphan_and_returns_v2` — `atomic_write` をplan-v2で1回失敗注入
37. `test_policy_ir_write_failure_recovers_without_duplicate_event`
38. `test_policy_index_write_failure_recovers_without_v3`
39. `test_crash_after_policy_event_then_retry_recovers_idempotently`
40. `test_persistent_policy_recovery_failure_reports_recorded_version` — 失敗outcomeがv2を名指し・「反映されていません」なし
41. `test_failed_rebuild_view_preserves_non_null_recorded_target_version`
42. `test_a_then_b_interleaving_preserves_b_as_current_policy` — Aは失敗しBを黙って実行しない

## 実装順序

1. 後方互換の予約・outcomeフィールド追加。正確judgment・append-once helper追加
2. judgment UUID生成をstorage重複排除の背後に移動。HTTPで既存rebuild状態を反映
3. 予約sequenceとlock descriptorをrunnerへ渡す。両鮮度検査を実装
4. 内部選択予算台帳・統合budget fold・30秒検査・壁deadline伝播を追加
5. policy event payload parser・期待基準CAS・冪等event検索・orphan recoveryを追加
6. 新outcomeは `connected`/`failed` のみを規定の証拠フィールド・日本語文字列で発行
7. 42回帰を実装し、対象pytest・全体pytest・ruff・basedpyrightを実行

規模: 大（人間換算3-5日）。確度: 中〜高。安全・冪等契約は高確度。現在のrendererはplan全体束縛previewを作るため、30秒超の正直な短期挙動はfail-closed（実際の切り詰め見本は別設計のplan/IR束縛preview射影が必要）。
