# AI動画編集パイプライン 方針・実行設計 v4.2

> **文書の権威関係（正本宣言）**: 本書は参照用の統合コピーである。**正本（binding spec）は `docs/prd/PRD_v4.1.md`** であり、本書と v4.1 が矛盾する場合は常に v4.1 を優先する（CLAUDE.md 第10節と同一の運用）。本書は v4.1 の中核設計を変更せず、実行計画（`.omo/plans/foundation-video-pipeline.md`、OMO監査済み）で確定した実装仕様と検証体制を v4.1 への参照資料として整理したものだが、この「どう作り、どう証明するか」の記述自体は正本ではない。**本書は正本を名乗らない。**
>
> 判定：GO（基幹システム実装済み、リリース検証中）
>
> v4.1 が「何を作るか」を定義したのに対し、v4.2 は「どう作り、どう証明するか」——Gate凍結・証拠・人間承認・再現性検証——までを参照資料としてまとめる。

- 文書バージョン：4.2
- 基準日：2026-08-21
- 前版：`docs/prd/PRD_v4.1.md`（2026-08-15）
- 対象：DaVinci Resolve Studio 21を使ったYouTube動画制作
- 最上位KPI：撮影後のActive Human Time
- 実装状態：Phase 0A〜3 実装完了・各Gate通過済み。残作業はリリース候補生成と最終独立検証（F1〜F4）。
- 実装基盤：Python 3.12 モジュラーモノリス／uv管理／Pydantic v2 strict契約＋生成JSON Schema／SQLite＝Runtime State／DuckDB＝検索Index／pytest・hypothesis・Ruff・basedpyright・Semgrep

## 0. 本書の読み方とV4.1からの変更点

### 0.1 このシステムは何か

カメラ素材を入力すると、Ingest → 解析 → AI編集提案 → 低解像度Preview MP4 → 人間が自然言語で修正指示 → 確定Edit Plan → DaVinci Resolveでの自動Build → Render → QC → 最終承認、までを**Versioned Artifactの連鎖**として再構築可能に実行する制作パイプラインである。

人間の作業は「撮影」「方向性の決定」「Preview確認と修正指示」「要確認箇所の確認」「公開判断」に限定することを目指す。評価指標は自動化率ではなく、素材投入後に人間が制作へ使うActive Human Timeである。

本書が正本仕様である。`.omo/plans/foundation-video-pipeline.md` は「それをどう実行したか」の成果物であり、`AGENTS.md` は開発エージェント向けの要約である。

### 0.2 V4.1→V4.2の主な変更一覧

| # | 項目 | V4.1 | V4.2 |
|---|---|---|---|
| 1 | Control Plane Baseline Gate | Control Planeはアーキテクチャ要素（5.2）のみ | Phase 0CとPhase 1の間に独立Gate `control-plane-baseline` を挿入。不変Artifactストア・Registry・SQLite状態・CAS/Lease・Policy/Audit・TTY承認入口を通過しないとPhase 1へ進めない |
| 2 | 人間承認の機構化 | `EDITORIAL_APPROVED` が必須Gateという概念 | ローカルTTY専用CLI（`checkpoint show/record/export`）による**purpose束縛Operation Record**として実装。Editorial/Presentation/Privacy/Rights/Final/Publication/Manual Freeze の7目的を分離し、AI・非対話ランナーは呼出不可 |
| 3 | PRESENTATION_APPROVED | Review GateはEditorial/Finalの2段階 | Preview↔Finalの見た目一致を保証する第3の承認レコードを追加。Manifest/Asset変更で自動失効 |
| 4 | Gate凍結プロトコル | Capability Matrixを実機検証で管理（18章） | 各PhaseのGate Policyをcanonical JSON（キーソート・float禁止・SHA-256）として凍結。親Gate結果hashチェーン、同一バージョン再凍結禁止、観測後の期待値書き換え禁止 |
| 5 | 証拠（Evidence Ledger） | Observabilityへの言及のみ | fsync前投入・previous-hashチェーン付きAttempt台帳を実装。Gate合否は報告値ではなく生バイトから再計算。検出限界も明記 |
| 6 | ツールチェーン固定 | 「Version Pinする」（27章） | uv 0.8.11／CPython 3.12.10／FFmpeg 7.1.1（configure引数まで凍結）／whisper.cpp pinned commit＋モデルSHA-256／CMake 3.31.8／Semgrep 1.120.0 をPhase別Toolchain Lockで継承管理 |
| 7 | Idempotency Key | `stage_name + input_artifact_hashes + runner_version` | **`+ code_snapshot_id`** を追加。コード変更時の成功再利用誤りを防止 |
| 8 | フィクスチャマトリクス | 「CFR、29.97、59.94、VFR…」という列挙 | `p0a-cfr30-fixed`、`p0b-vfr-2-3-cadence`、`cp-crash-before-rename`、`p1-ref-01〜05`、`p2-stale-capability`、`p3-brand-a/b` 等を正確なIDで命名固定。代替作成を禁止 |
| 9 | Gate閾値 | 定性的なExit Criteria | Item Conformance 100%・frame delta 0・同期ずれ≤1 frame・曖昧命令自動適用0・構造化Command Coverage ≥80% 等に数値化 |
| 10 | リリース検証 | 成功条件31.4「再Buildできる」（製品の性質） | manifest-v1リリース候補（candidate_id＝manifestバイト列のSHA-256）、クリーンルーム再生、F1〜F4外部検証レーンによる同一SHA独立承認 |
| 11 | 後続Phase機能の扱い | 本文に機能例示が混在（`get_review_examples`等） | Phase 4以降の機能（Review Learning取得API、自動Privacy検出、Visual Judge等）は**未実装であることをテストで保証** |

### 0.3 変えなかったもの

以下はv4.1から一切変更していない。

- Active Human Time最上位KPI（中央値30分目標・P90・Coverage Ratio併記）
- Supported Episode Contract（talking-head-mvp-v1）
- 7つの設計原則（4.1〜4.7）
- Data Plane / Control Plane分離
- 三つの正準時間座標と半開区間 `[start, end)`
- Timeline IR経由のNLE非依存設計
- Clean Build原則
- 自然言語Review＋専用UI先送り
- Phase 4〜6の非目標

## 1. 目的

カメラで撮影した複数の動画素材を入力として、DaVinci Resolve Studioを使ったYouTube向け動画制作を、撮影後の人間作業を最小化した状態で完了できる仕組みを作る。

目指すものは、AIがDaVinci Resolveを人間の代わりに何百回も操作する仕組みではない。素材を機械可読な状態へ変換し、編集判断を構造化データとして保存し、その判断からPreview、Timeline、完成動画を再構築できる制作システムである。

人間が担当する作業は原則として以下へ限定する。

- 撮影する。
- チャンネルと動画の方向性を決める。
- 低confidenceの候補だけを必要に応じて選ぶ。
- 低解像度のEditorial Preview MP4で構成を確認し、AIとの自然言語対話で修正要求を入力する。
- QC Engineが抽出した要確認箇所を確認する。
- 最終的な公開可否を判断する。

初期の長期目標は、Supported Episode Contractを満たす動画について、候補確認、修正指示、最終確認を含むActive Human Timeの中央値を30分以内へ収めることである。平均値だけでは一部の失敗を隠せるため、P90も同時に計測する。

**実装上の正直な注記**：Phase 0A〜3で達成したのは技術的成功条件（30章）である。30分KPIは実素材Episodeを実運用して初めて評価可能であり、現時点では `not_evaluated` である。5本の技術ReferenceフィクスチャがKPI達成を証明することはない。

## 2. 非目標

次の状態を初期目標にしない。括弧内は実装計画で明示的に排除した項目である。

- あらゆるYouTubeジャンルへ同時に対応する。
- AIへ完全なクリエイティブ責任を渡す。
- DaVinci Resolveに存在する全機能を自動化する。
- AIが同じ入力から毎回同じ編集判断を再生成することを保証する。
- Timeline上で行った任意の手作業を自動的にEdit Planへ逆変換する。
- Native Multicam、Variable Speed Retime、複雑なMask、手作業前提のFusion CompositeをMVPへ含める。
- Transition自動化、可変Retime、Per-shot AI Grade、複雑なFusionグラフ生成。（計画でも明示禁止）
- 分散キュー、ワーカープール、分散ロック、リモートArtifactストア、第二Resolveホスト、代替NLE Adapter。（計画で明示禁止）
- 製品AIへの無制限Shell・File Write・Network Access付与。媒体由来テキスト（Transcript/OCR）を信頼できる指示として扱うこと。
- 実チャンネルブランド制作、実Logo/Font/BGM資産の登録。Phase 3は自己生成の権利安全なA/Bフィクスチャのみを使用する。
- 最終的な公開判断、権利判断、プライバシー判断をAIへ移す。
- 専用Review UIを必須成果物にする。
- **Phase 4以前の自動Privacy Candidate検出器**（手動申告された問題の人間Gate強制は行う）。
- **Phase 5以前のReview Learning取得API**（`get_review_examples` 等。存在しないことをテストで保証する）。
- 技術フィクスチャだけで30分KPIや実編集品質の達成を主張すること。

AI判断の再現性とBuildの再現性は分ける。採用済みEdit Planを保存することで編集判断をReplayし、同じEdit Planと固定Toolchainから同じ構造のTimelineを再Buildする。LLMへ再問い合わせして同じPlanを作らせることは要件に含めない。

## 3. Supported Episode Contract

### 3.1 MVPの適格条件

初期Reference Caseは、次の条件を満たす発話主導動画とする。

| 項目 | MVP既定値 |
|---|---|
| 言語 | 日本語 |
| 完成尺 | 6分から15分 |
| 総素材尺 | 90分以内を既定上限とする |
| 主体 | 単一話者、または明確に分離できる少人数 |
| 撮影形態 | 単一の主映像、連続する複数take、任意のB-roll |
| 音声 | 48kHz推奨。DialogueとAmbientを判別できること |
| 編集 | Hard Cut中心。一定速再生。複雑なMulticamなし |
| Presentation | 字幕、少数テロップ、Intro、Outro、基本BGM |
| Color | Project TemplateとPresetで処理できる範囲 |
| Privacy | Critical候補を人間が確認できること |

既定値はChannel Profileで変更できる。ただし契約を広げた場合は同じ30分KPIを自動的に引き継がない。

### 3.2 適格性の判定

Ingest後にEpisode Eligibilityを機械判定する（実装済み：`services/ingest` 配下のeligibility判定、Assisted Modeへの振り分け判定まで）。

```yaml
eligibility:
  status: eligible
  contract: talking-head-mvp-v1
  reasons: []
  measured:
    source_duration_sec: 4260
    detected_video_rates:
      - 30000/1001
    speaker_count_estimate: 1
    hdr_sources: 0
    multicam_required: false
```

契約外のEpisodeは失敗として扱わずAssisted Modeへ送る。ただしAssisted Mode自体の実装はPhase 4であり、現状は適格性判定とKPI分母からの分離記録までを行う。適格Episodeの割合はCoverage Ratioとして測る。

## 4. 設計原則

### 4.1 Artifact-first

各工程は、前工程の暗黙状態ではなくVersioned Artifactを入力として実行する。チャット履歴、エージェントの記憶、開いたままのTimelineを正本にしない。

### 4.2 ProposalとCommitを分離する

LLMはSelection Plan、Edit Plan、QC Issue、修正案をProposalとして出力する。Schema Validation、Semantic Validation、Lock Validation、Capability Validationを通過したProposalだけを新VersionとしてCommitする。

### 4.3 決定論的処理をコードへ寄せる

時刻変換、尺計算、Constraint solving、Track割当、配置、字幕分割、Build、再Build、検証、Retryはコードで実行する。LLMは意味判断、候補評価、構成、例外原因の仮説生成を担当する。座標・尺計算・制約解決・最終字幕分割・Track割当・Build成功・Retry実行・QC pass・Privacy/Rights却下・Final/Publication承認をAIが所有することはない。

### 4.4 単一Writer

Resolve ProjectとJob Stateへの書き込みは一系統に限定する。実装ではState更新・Plan/Event Commit・Resolve Builderの3つのmutation laneを直列化し、CAS（compare-and-set）と期限付きLeaseで守る。

### 4.5 公開された安定面を優先する

Production Pathは、Resolveの公開Scripting API、公開Interchange Format、事前生成したTemplate、通常のMedia Fileを優先する。内部Project Databaseや非公開ファイル形式の直接編集は行わない。

### 4.6 不確実性を早く見せる

重いResolve Buildの前に低解像度のEditorial Preview MP4を作る。構成や採否が間違っていれば、PresentationとFinal Renderへ進む前に止める。`EDITORIAL_APPROVED` はFinal Buildの必須Gateである。

### 4.7 機能追加は実素材の失敗から行う

想定される全機能を先に作らない。Reference Episodeで人間が修正した内容を記録し、頻出するStructured CorrectionからSchema、Profile、Template、Adapterを広げる。

### 4.8 Gateは凍結されてから通る（V4.2で追加）

各PhaseのGate Policyは、そのPhaseのToolchain Lock・Fixture・Golden値が**実装から独立に**存在することを確認した後にだけ、canonical JSON（UTF-8・キーソート・float/NaN禁止・compact serialization・`sha256(canonical_bytes)` 保存）として凍結する。観測結果に合わせて期待値を書き換えること、同一バージョンの再凍結を禁止する。変更は新しいGateバージョンと影響範囲の再実行を要求する。

### 4.9 人間の承認は目的束縛の操作レコードで残す（V4.2で追加）

`EDITORIAL_APPROVED` 等の人間判断は、ローカルTTY専用CLIが表示した対象hashを操作者が確認した上で記録されるpurpose束縛のOperation Recordとして保存する。製品AIや非対話ランナーはこの経路を呼び出せない。本機構は単一利用者ホスト向けの監査証跡であり、暗号学的な本人認証・否認防止ではない——この限界を仕様として明記する。

### 4.10 証拠は再計算可能でなければならない（V4.2で追加）

Gateの合否は、報告者の書いた `passed` 値ではなく、生のArtifact・Readback・Renderバイトから再計算して判定する。実行証跡はfsync前投入とprevious-hashチェーンを持つAttempt Ledgerとして残す。ただし未記録の実行、台帳全体の差し替え、ワークスペース所有者/root権限による改ざんは検出できない。

## 5. 全体アーキテクチャ

制作データを流すData Planeと、Jobの状態を管理するControl Planeを分ける。

### 5.1 Data Plane

```text
Camera Originals
↓
Ingest / Normalize
↓
Source Manifest + Conform Map
↓
Analyzers
↓
Queryable Media Store
↓
Editorial Director
↓
Selection Plan Proposal
↓
Validator / Committer
↓
Selection Plan
↓
Duration / Constraint Planner
↓
Edit Plan Proposal
↓
Validator / Committer
↓
Edit Plan
↓
Timeline Compiler
↓
Timeline IR
├─→ Preview Renderer
│   ↓
│   Editorial Preview
│   ↓
│   Review Interface
│   （MVP: Video Player + AI Chat / 長期: Dedicated Review UI）
│   ↓
│   Review Commands
│
└─→ Resolve Adapter
    ↓
    Staging Timeline
    ↓
    Build Conformance
    ↓
    Final Render
    ↓
    Deterministic QC
    ↓
    Final Review
```

### 5.2 Control Plane

```text
Job State Machine
+
Artifact Registry
+
Capability Matrix
+
Resolved Configuration
+
Observability / Evidence Ledger
+
Budget / Policy Guard
+
Approval Ingress（TTY専用）
```

Control Planeは、どのArtifactを入力にどのStageを実行したか、Retryしてよいか、人間確認が必要か、どのVersionが承認済みかを管理する。

**V4.2での確定事項**：Control PlaneはPhase 0CまでのSpikeハーネスとは別に、独立したGate `control-plane-baseline` として検証される。不変ファイルArtifactストア（atomic publish・crash復帰・orphan reconciliation）、lineage Registry、SQLite Runtime State（番号付きmigration）、CAS/Lease/直列化authority、設定解決とPolicy/Audit、TTY承認入口が揃い、Baseline Gateを通過するまでPhase 1以降の実装へ進めない。Spike期（0A〜0C）はこの最小サブセットのみを使用する。

### 5.3 実装スタック

- Python 3.12 モジュラーモノリス（`video-pipeline/services/` 配下に約40モジュール）
- 契約はPydantic v2 strict（`extra='forbid'`）で定義し、JSON Schemaを生成してdrift検査する
- Runtime State はSQLite（番号付きmigration）、検索Index はDuckDB（再生成可能）。**正本は常にファイルArtifact**
- 品質コマンド：`uv sync --frozen && uv run ruff check . && uv run basedpyright && uv run pytest -q`
- テストマーカー：`resolve_live`（実機Resolve必須）、`cloud_fixture`（合成データのCloud呼び出し）

## 6. Job State Machine

長時間のLLM tool-call chainをJob Orchestratorにしない。各Stageは明示的な入力、出力、成功条件を持ち、Idempotentな処理とする。

```text
CREATED → INGESTED → NORMALIZED → ANALYZED → PLAN_PROPOSED
→ PLAN_COMMITTED → PREVIEW_READY → EDITORIAL_APPROVED
→ RESOLVE_BUILT → QC_PASSED → FINAL_APPROVED → FROZEN
```

各Stageは以下の補助状態を持てる。

```text
RUNNING / FAILED_RETRYABLE / FAILED_BLOCKING / NEEDS_HUMAN / CANCELLED / SUPERSEDED
```

### 6.1 Idempotency Key（V4.2で拡張）

```text
stage_name + sorted(input_artifact_hashes) + runner_version + code_snapshot_id
```

同じKeyの成功Artifactがある場合は再利用する。**V4.2で `code_snapshot_id` を追加**した。コード変更後も古いKeyで成功を再利用してしまう誤りを防ぐ。

### 6.2 直列化とLease（V4.2で確定）

- 状態遷移はcompare-and-set（期待状態＋親hash）で行い、競合した側は `SUPERSEDED` になる。
- 単一のローカルJob State authorityのみが書き込む。分散トランザクションや並列authorityは存在しない。
- 期限付きLeaseがBuilder等の排他を保証し、失効時は回復手順が動く。
- 承認なしの `PREVIEW_READY → RESOLVE_BUILT` 遷移は禁止され、テストで保証される。

### 6.3 Retry分類

Timeoutや一時的なResolve接続断は回数制限付きで自動Retryできる。Schema違反、Capability不足、元素材欠落は自動Retryせず、入力修正または人間確認へ送る。crash後の復旧は最後にCommitされたArtifactから再開する。

## 7. Source of TruthとArtifact分類

### 7.1 Authoritative Artifact（長期保存）

Camera Original／Source Manifest／Conform Map／Job Manifest／Resolved Configuration Snapshot／Asset Registry Snapshot／Committed Selection Plan／Committed Edit Plan／Review Events／Profile Change Approval／Final Timeline IR／Resolve Build Report／Final QC Report／Final Render／Manual Finalization Package

### 7.2 Rebuildable Artifact（期限付き保存）

Proxy／Edit Mezzanine低解像度版／Analysis Frame／Contact Sheet／Embedding／Query Index／Editorial Preview／Resolve Package／Intermediate Timeline／Temporary Audio Analysis

### 7.3 Runtime State（正本にしない）

Job State、Lock、Lease、Retry Count、進捗、Cache Index。SQLite/DuckDBへ保存するが、編集判断の正本にはしない。Committed Planの本体をDB行として保存しない。

### 7.4 Manual Finalizationの例外

Schemaが表現できない手作業を行いAutomationをFreezeした場合のみ、Timelineが例外的に正本になる。Manual Finalization Package（手作業後のDRT/DRP Export、Final Render、Timeline Fingerprint、Manual Change Log、対応Edit Plan Version、Freeze理由）を正本として保存する。Freeze後のBuilder mutationは禁止する。

## 8. Artifact EnvelopeとJob Manifest

すべての主要Artifactは共通Envelopeを持つ。

```json
{
  "artifact_id": "art_01J...",
  "artifact_type": "edit_plan",
  "schema_version": "4.0.0",
  "content_hash": "sha256:...",
  "created_at": "2026-08-15T12:30:00+09:00",
  "producer": {
    "type": "service",
    "name": "edit-plan-committer",
    "version": "git:5e9342a"
  },
  "inputs": [
    "sha256:selection-plan...",
    "sha256:resolved-config..."
  ]
}
```

Job Manifestは実行環境と採用Artifactを固定する。Toolchain（OS、FFmpeg、ASR、Resolve version/build、adapter/builder git commit）、Models（provider、model_id、snapshot、prompt_bundle_hash、output_schema_version、request_id）を記録する。モデル名やSemantic Versionだけでは挙動を固定できないため、Model Snapshot、Prompt Bundle Hash、Git Commit、Template/Asset Content Hashを可能な限り残す。

## 9. Ingestと時間座標

### 9.1 元素材とEdit Sourceを分ける

Camera Originalは一切変更しない。CFRで座標整合が確認できた素材はOriginalをEdit Sourceに使える。VFR・壊れたTimestamp・複雑なRotation・Codec相性がある素材はCFRのEdit Mezzanineを生成する。MVPではVFR Originalへの直接Frame指定Buildを必須にせず、OriginalへのOnline RelinkはGolden Fixture確認後に追加する。

### 9.2 三つの正準座標

- **Original Timestamp**：PTSとtime_base
- **Edit Video Position**：Edit Source上の整数Frameと正確なFrame Rate
- **Edit Audio Position**：Edit Source上の整数SampleとSample Rate
- **Record Position**：Timeline上の整数Frame

すべての区間は半開区間 `[start, end)`。float秒を正準保存に使わない。変換は有理数・整数演算のみで行い、丸め政策を明示する（実装：`services/conform` の座標変換、hypothesisプロパティテストで単調性・境界保持・決定的丸めを検証済み）。

Subtitle/Transcript Tokenは原則Audio Sampleに結び付け、表示時にCompilerがTimeline Frameへ変換する。

### 9.3 Conform Map

OriginalとEdit Sourceの対応をVersioned Artifactとして記録する。PTS→frameテーブル、sample affine/explicit mapping、正規化metadata、source identityを含み、Analyzerの秒・ffprobeのPTS・ResolveのSource Frameが一致すると仮定しない。

### 9.4 Source Manifestの項目

v4.1で定義した追加項目（start_time、stream_index、pix_fmt、color三要素、HDR Metadata、Audio Channel Layout、Encoder Delay、Timestamp Monotonicity、VFR判定根拠、Edit Source生成Recipe/Hash）を実装済み。Color Metadata/HDR欠落は契約外として止めるか明示的なColor Management Recipeを選ぶ。

## 10. Queryable Media StoreとAnalyzer

Media Storeの一次保存先はファイルArtifactが正本で、DuckDBは再生成可能な検索Index、SQLiteはRuntime Stateを担う。巨大なJSONをLLMへ一括投入しない。

### 10.1 実装済みAnalyzer（Phase 1）

ffprobe Stream Metadata／Audio Decode Validation／Transcript＋Word Timestamp（ローカル日本語ASR）／Silence/Pause／Filler候補／False Start候補／Audio Peak・Loudness・Clipping候補／Scene Change最低限検出／Blur・Black・Exposure最低限検出／Contact Sheet

Motion、Shot Clustering、Visual Similarity、Face Tracking、Plate TrackingはPhase 4（Travel/POV）で追加する。**自動Privacy Candidate検出器はPhase 4まで実装しない**（import自体をscope checkが拒否する）。

### 10.2 ローカル日本語ASR（V4.2で明記）

- `ggml-org/whisper.cpp` をcommit pinned（`1fe009ca…`）でソースビルドし、モデル `ggml-large-v3-turbo.bin`（revision・SHA-256固定）を使用する
- 入力は凍結されたFFmpeg argv（mono PCM-s16 16kHz）でのみ変換する
- Cloud送信なし。word/token timingはGolden Fixture検証済みのexperimental契約として扱い、事実と仮定を分けない

### 10.3 Cache Key

```text
edit_source_content_hash + analyzer_name + analyzer_version + parameter_hash (+ model/prompt hash)
```

親hashが変わったAnalyzer出力は `SUPERSEDED` としてmarkし、採用しない。

### 10.4 Query Interface

Editorial Directorへは読み取り専用の検索Interfaceを公開する（実装：`services/media_query`、pagination/range/budget制限付き、結果にSource ID・Span・Analyzer Version・Confidence・Evidence hashを付与）。

```text
get_episode_summary()          get_source_summary(source_id)
get_transcript(source_id, audio_span)   find_transcript(query, filters)
find_silence_ranges(filters)   find_low_quality_ranges(filters)
get_contact_sheet(source_id, range)     get_candidate_frames(source_span, density)
get_range_statistics(source_span)
```

**`get_review_examples(context)` は意図的に含まない。** v4.1では例示していたが、これはReview Learning（Phase 5）に属する機能であり、Phase 0A–3では実装を禁止し、テストで「APIが存在しないこと」を保証する。Editorial DirectorはEvidenceのない映像内容をPlanへ書かない。モデルへはraw DB・任意SQL・Shell・無制限frame dumpを渡さない。

## 11. モデルの役割と境界

Architecture上の役割名と、現在採用するモデル名を分ける。Roleが契約であり、Modelは交換可能な実装である。

| ロール | モデル候補 | 担当 | 実装状態 |
|---|---|---|---|
| Editorial Director | GPT-5.6 Sol | 素材検索方針、候補評価、構成、Selection/Edit Plan Proposal、テロップ・BGM意味選択、修正Proposal | **実装済み**（`services/editorial`）。境界テスト済み：証拠なしSpan生成、ID採番、Resolve操作、Commit、Retryはすべて拒否される |
| Review Translator | Editorial Directorと同一契約 | 自然言語修正指示→構造化Review Command Proposal | **実装済み**（`services/review_command/translator`）。strict JSON Schema出力、refusal/truncation/error処理、request/model hash記録、replay transport |
| Visual Judge | — | 視覚候補比較（動作開始終了、決定的瞬間、構図差、連続性） | **未実装**。Phase 4（Travel/POV）で導入 |
| Diagnostic Assistant | GLM-5.2 | Build Report確認、Capability Matrix参照、Retry可能性判定案、Builder/Input修正案 | **ロール定義のみ**。Job Runner側のfailure taxonomy（transient/blocking/human分岐）は実装済み。モデル診断統合は未実装 |

### 11.1 共通制約（実装済み）

- モデルへShell/File Write/Network toolを渡さない。媒体由来テキストは信頼できる指示として扱わない（prompt injectionテスト済み）。
- モデル出力は常にProposalであり、Validator通過まで正本にならない。
- 外部Model呼び出しはdata policy envelope内で行い、Artifact範囲・Provider・Request IDを監査Logへ残す。
- `local_only` EpisodeではCloud Modelを使うStageを実行せず、Local AnalyzerまたはHuman Reviewへ切り替える。

### 11.2 OpenCode、OMO、MCPの位置づけ

OpenCodeとOMOは開発、対話的検証、運用コンソール、例外診断に使う。Production Jobの正本状態とStage遷移はJob State Machineが管理する。MCPはResolve機能への補助Interfaceであり、Production Builderではない。Builderは公式Scripting APIを決定論的に呼ぶCLI/Local Serviceである。

## 12. Selection Plan

Editorial Directorはいきなり完成Timelineを決めない。素材から使える候補と意味的制約をSelection Planへ保存する。

```json
{
  "candidate_id": "cand_sha256_...",
  "source_span": {
    "edit_source_id": "cam_a_001_cfr",
    "start_frame": 2800,
    "end_frame": 3190
  },
  "intent": "arrival_establishing",
  "story_block": "arrival",
  "story_order": 3,
  "priority": 0.88,
  "must_include": false,
  "duration": {"min_frames": 60, "ideal_frames": 120, "max_frames": 180},
  "handles": {"head_frames": 15, "tail_frames": 15},
  "redundancy_group": "sensoji_gate",
  "evidence": [
    {"type": "contact_sheet", "artifact_id": "art_..."},
    {"type": "transcript", "audio_start_sample": 12000, "audio_end_sample": 92000}
  ],
  "reasons": ["場所が明確に分かる", "前後の移動Shotとつながる"]
}
```

Candidate IDはLLMに採番させない。`source_id + normalized_span + intent + analyzer_version` からControllerが決定的に生成する（実装済み）。LLMがSpanを微修正した場合はCandidate Relationとして親子関係を残す。発話カットには前後Handleを持たせ、最終TrimはPlannerとAudio RuleがHandle内で決める。

## 13. Duration and Constraint Planner

完成尺をEditorial Directorの一発生成へ依存させない。

### 13.1 Hard Constraint

Must Include／Must Exclude／Story Order／Dependency／Approved Lock／Source範囲の有効性／Capability Matrix上の実装可能性／Episode Contract

### 13.2 Soft Constraint

Target Duration／Redundancy削減／Pacing／Shot Diversity／Channel固有の好み／過去Reviewとの整合／Confidence

PlannerはHard Constraintを黙って破らない。解が存在しない場合はInfeasibility Reportを返す。同じScoreの解が複数ある場合はCandidate IDによる決定的Tie-breakを使う。モデルは意味・重みを提案できるが、制約解決や強制的な結果作りはできない（実装済み：`services/plan`、プロパティテストでhard constraint保持・決定的tie-breakを検証）。

## 14. Edit Plan

Edit Planは採用された編集判断を表す。Resolve固有の命令列を書かない。

- Video ItemとAudio Itemは別に持ち、`link_group_id` で関連付ける。J Cut、L Cut、B-rollを作るときだけSource SpanとPlacementを分ける（J/L Cut自体の活用はPhase 4〜）。
- Decision IDはControllerが採番し、Plan再生成時はSource・Intent・Story Block・近接Span・親Candidateから既存DecisionとReconcileする。LLMへUUID維持を期待しない。
- LockはField単位（selection／source_span／order／text／presentation／audio）。付与者・時刻・根拠・Base Plan Versionを保存し、競合ProposalはCommitせずConflictとしてReview Interfaceへ出す。

## 15. Timeline IR

Edit PlanとResolve固有形式の間に置くNLE非依存の中間表現。

```json
{
  "timeline": {
    "rate": {"num": 30000, "den": 1001},
    "width": 3840,
    "height": 2160,
    "audio_sample_rate": 48000
  },
  "items": [
    {
      "decision_id": "dec_01J...",
      "kind": "video",
      "source_id": "cam_a_001_cfr",
      "source_start_frame": 2800,
      "source_end_frame": 3190,
      "record_start_frame": 0,
      "record_end_frame": 390,
      "logical_track": "primary_video"
    }
  ]
}
```

Resolve固有の `track_index`、Media Type番号、API Method、Template適用手順はResolve Packageで初めて登場する。Preview Rendererと将来の別NLE Adapterは同じTimeline IRを使える。

Timeline CompilerはDuration変換、Anchor解決、Gap検出、Audio Sample→Record Frame変換、Subtitle Cue生成、Logical Track整合を一か所で行う（実装済み：`services/compile`。Golden配置・全IR QC rule・決定的hash/rebuildを検証済み）。

## 16. Editorial Preview

Resolve Buildの前に、Timeline IRから低解像度Preview MP4をFFmpegで生成する（実装済み：`services/preview`）。Hard Cut、基本Audio、仮字幕、簡易Overlay、BGMを再現し、Record Range↔Decision ID対応manifestを添付する。Resolve不要であり、最終画質の証明でもない。

短期ターゲットでは、Previewは通常の動画プレイヤーで再生し、修正はAIとの自然言語対話で伝える。

```text
人間：3:12〜3:35は削除。
人間：5:40は切るのが早いので、一文前から残す。
人間：7:20の字幕を「OpenClaw」に修正。
人間：それ以外はOK。
```

AIはこれを `remove_segment`、`adjust_source_span`、`correct_subtitle`、`approve_remaining` 等の構造化Review Commandへ変換する。曖昧な指示だけを人間へ確認し、解釈結果はReview Eventとして保存する。

Previewの目的は次の判断を先に終えることである：Shot採否、発話削除、構成順序、尺とPacing、Must Include充足、Subtitle内容の大きな誤り、BGM方向性。

`EDITORIAL_APPROVED` をFinal Buildの必須Gateとする。Previewを通さずResolve Buildまで進むと、編集判断の問題とResolve自動化の問題が混ざる。

## 17. Resolve AdapterとBuilder

### 17.1 Build手順

```text
Timeline IR → Capability Validation → Resolve Package
→ Staging Project / Staging Timeline → Base Media Placement
→ Subtitle / Overlay / Audio / Preset適用
→ Item-level Conformance Check → Final Render
```

既存TimelineをBladeし続ける方式は取らない。公開APIのtrim/move/blade制約のため、構造変更はClean Rebuildが既定である。

### 17.2 Adapter Strategy

機能ごとに `direct → interchange → template → external → manual → unsupported` の優先順位で退避経路を持つ。内部DRP/DRT/Project Database直接書き換えはProduction必須経路にしない。

### 17.3 Build Mode

- `clean`：空のStaging Timelineへ全体を再構成。構造変更の既定値。
- `patch`：Capability Matrixで検証済みの低リスク変更のみ（字幕文字修正等）。失敗時はClean Buildへ戻る。
- `frozen`：Manual Finalization後。自動Buildしない。

### 17.4 実装上の確定事項（V4.2）

- Builder（`services/build/clean_builder.py`）は単一WriterとしてResolve Leaseを取得し、versioned Staging project/timelineへ新規構築する。人間のTimelineを漸進的に書き換えない。BuilderはJob Stateを直接書かない。
- Resolve接続はloopback限定のBridge（`services/resolve_bridge`）経由で、host-probed公式Scripting APIのみをロードする。MCP依存・リモート接続・内部DB accessはない。
- Phase 0AでBase Cut／固定字幕／Intro-Outro／基本Audio Preset／Render Preset／Item-level Readback／Build Reportを実機検証済み。

## 18. Capability Matrix

DaVinci Resolve Studioに機能が存在することと、公開APIから安全に自動化できることは別である。

```json
{
  "feature": "fusion_title_placement",
  "resolve_version": "21.x",
  "resolve_build": "...",
  "adapter_commit": "git:...",
  "status": "partial",
  "strategy": "external",
  "live_verified": true,
  "fixture": "fixtures/title-track-placement-v2",
  "known_limitations": [
    "native title insertion cannot reliably target an arbitrary destination track"
  ],
  "fallback": "render transparent overlay asset and append as media-backed clip"
}
```

`api_available: true` だけでは不十分である。要求した結果がTimeline上で得られ、ReadbackとRenderで確認できたときに `live_verified: true` とする。Matrixは実機FixtureとBuildごとに更新する。

### 18.1 初期に検証した機能（Phase 0A〜3で実測）

| 機能 | 方針 | 状態 |
|---|---|---|
| Base Cut | direct | live_verified（frame delta 0） |
| Subtitle | direct/interchange/external比較 | 固定方式をlive_verified |
| Fusion Title | template/external | 実機probe結果に応じverified path＋external fallback |
| Intro/Outro | media-backed asset | live_verified（identity/duration/link一致） |
| Voice Isolation / Dialogue Gain | track preset / external | Track単位適用を検証 |
| Fairlight | saved preset | Dialogue/Ambient分離＋Preset適用を検証 |
| Transition | Hard Cut既定 | 自動追加はMVP外 |
| Retime / Native Multicam | unsupported in MVP | 一定速のみ |
| Grade | template/DRX/LUT | Per-shot AI GradeはMVP外 |
| Render | direct | preset/path/codec/job状態/failure検出を検証 |

## 19. 機能別方針

### 19.1 構成編集

発話主導ではTranscriptとAudioを中心に、失敗take、言い直し、重複、Filler、長いPause、本題外の区間、説明順序を判断する。自動削除は語の前後Handle、最小Shot長、Audio Crossfade、映像Jump許容Ruleを通す。Fillerを機械的に全削除しない。Travel/POVの視覚編集はPhase 4であり、Candidate-assisted Modeから始める。

### 19.2 Subtitle

Source TranscriptはAnalyzer ArtifactとしてAudio Sampleに結び付ける。Subtitle CueはEdit Plan確定後にCompilerが生成する：採用Dialogue Span限定 → Edit Boundary分割 → 日本語の句読点・意味境界で整形 → 最低表示時間/行数/文字量検証 → Timeline Cue変換。Editorial Directorが全Cueを一件ずつ生成しない。固有名詞・誤認識・表記統一など意味判断が必要な箇所だけModelへ渡す。

### 19.3 テロップとOverlay

Resolve Native Titleを任意Trackへ安全に置けない場合に備え、高表現Titleは事前作成Template/検証済みFusion Asset、規則的なOverlayは透明背景Media Assetとして外部Renderし通常Clip配置する（実装済み：`services/presentation`）。Overlay Textが事実主張を含む場合はTranscript・Episode Config・確認済みMetadataのいずれかにEvidenceを持つ。AIが数値や固有名詞を補完しない。

### 19.4 Audio

Audio Itemはdialogue／ambient／music／se／mixed／silenceへ分類する。Voice IsolationがTrack単位でしか安定適用できない場合、DialogueとAmbientを同一Trackへ混在させない。Clip単位のGain/Pan/EQ自動化ができない機能は「外部Audio Derivative生成 → Fairlight Track Preset → Project Template → Manual Finalization」の順で処理する。Dialogue処理をAmbientへ一律適用しない。

### 19.5 BGMとSE

BGM/SEは承認済みAsset Registryから選ぶ。AIがInternetから自由に取得しない。Gain/DuckingをResolveで細かく自動化できない場合はPreview/Final共用のMixed Audio Derivativeを外部生成するかTrack Presetで近似する。Channel ProfileはLoudness TargetとTrue Peak Ceilingを持つ。

### 19.6 Color、Crop、Stabilization

MVPではProject Color ManagementとCamera別Presetを固定し、AIによるShotごとのGradeを行わない。Color異常はQC Issueとして検出し人間確認へ送る。Stabilization/Reframe/MaskはCapability MatrixのFixtureを通った機能だけ有効にする。

### 19.7 RetimeとMulticam

一定速以外のRetime、Reverse、Speed Ramp、Native MulticamはMVP外。Interchangeで表現できてもGolden FixtureでFrame Identityまで確認できるまでProductionへ入れない。

## 20. Build VerificationとTimeline Versioning

Builderは「成功しました」だけを返さない。要求と実結果をItem単位で比較する（実装済み：`services/build` のconformance検証）。

```json
{
  "timeline_ir_hash": "sha256:...",
  "resolve_package_hash": "sha256:...",
  "items_requested": 84,
  "items_built": 84,
  "item_checks": [
    {
      "decision_id": "dec_01J...",
      "media_hash_match": true,
      "source_start_delta_frames": 0,
      "record_start_delta_frames": 0,
      "track_match": true,
      "link_group_match": true
    }
  ],
  "timeline_duration_delta_frames": 0,
  "failures": []
}
```

Timeline総尺一致だけでは誤配置を検出できないため、各ItemのMedia Identity、Source Span、Record Span、Track、Link Groupを確認する。Build前にTimeline Fingerprintを比較し、人間の手作業によるDriftがある場合は上書きせず、構造化Override・別Branch・Freezeへ振り分ける。

## 21. QC

### 21.1 Deterministic QC（実装済み）

三段階で実施する。閾値はversioned policyで管理し、Issueにはseverity・Decision ID/Record Span・Evidence・tool/threshold version・input hashを付与する。

- **IR QC**：Gap、Overlap、Track Rule違反、Anchor未解決、Source Span範囲外、Lock違反、Must Include欠落、Episode Contract違反、Unsupported Capability使用
- **Preview QC**：不自然に短いShot、同一映像過剰反復、Subtitle Timing、Audio欠落、Dialogue/Ambient役割衝突、構成上の空白
- **Final QC**：映像（Decode/Render Failure、Duration差、Black Frame、Freeze候補、異常輝度、Frame Drop、解像度/FPS/Color Metadata）、音声（Stream欠落、Clipping、True Peak超過、Integrated Loudness逸脱、異常無音、Channel Layout、Audio Duration差）、字幕（Cue欠落、表示時間不足、Overlap、Safe Area逸脱、文字化け、Style不一致）

### 21.2 AI QC（未実装）

「問題がないことを保証する」役割ではない。人間が見るべき候補をDecision ID付きで抽出する役割であり、Phase後半の導入を意図している。導入後もPlanを直接修正せず、IssueとFix Proposalを作るのみ。**決定論的QCの代替・却下・pass判定は一切できない**。

### 21.3 Privacy QC（手動申告＋人間Gate）

顔、ナンバープレート、住所等のCritical Privacy IssueはReview Budget外とし、解消または人間の明示承認がない限りFinal Approvalへ進めない。**Phase 0A–3では自動検出器を作らず**、人間が申告したPrivacy/Rights issueを受け付けてGateを強制するのみである。自動検出はPhase 4の範囲であり、そのimport自体がscope checkで拒否されることをテストで保証する。

## 22. Review Interface

### 22.1 Editorial Review（実装済み）

低解像度Preview MP4を通常プレイヤーで確認し、自然言語で修正指示を送る。実装面では `services.cli.phase1 run`（Phase 1フロー実行）、`services.cli.review propose|apply`（指示→Proposal変換、検証済み曖昧性なしProposalのCommit→Event適用→IR/Preview再生成）がOperator表面である。曖昧/schema gap/unsupportedな結果は状態を変えない。専用のRemoveボタンやSeek UIは作らない。

### 22.2 Final Review（実装済み）

Final Render、Build Report、QC結果、手動申告されたPrivacy/Rights、修正差分hashからなる不変Bundleを提示する。構造化修正は新しいPlan→Preview→Editorial checkpointへ、一時的失敗はRunnerへ、unsupportedな作業はFreeze Packageへ振り分ける。

### 22.3 PRESENTATION_APPROVED（V4.2で追加）

PreviewとFinalは同じPresentation Manifestを消費し、rasterized overlay/audio derivative hashを共有する。比較はitem/asset/cue/placement、subtitle領域、audio duration/loudness/peak、color metadata/statisticsについて行う（byte一致ではなく宣言tolerance内）。Presentation承認は独立したローカルTTY操作レコード `PRESENTATION_APPROVED` として記録し、Manifest/Profile/Asset変更で失効する。編集意味・timingの変更がある場合は `EDITORIAL_APPROVED` も失効させる。目的の再利用は禁止する。

### 22.4 長期ターゲットの専用Review UI

自然言語Reviewを複数本運用し、繰り返し発生する操作が人間時間のボトルネックになった場合にだけ追加する。Keep/Remove/Trim/Move/Subtitle修正/BGM変更/Approve等の候補機能はv4.1のまま長期リストとして保持する。

### 22.5 Active Human Timeの計測

MVPではPlayer上のSeek操作を完全には計測できないため、Review Session開始、Preview生成時刻、修正指示、Approval時刻をJob Logへ残し、必要に応じて自己申告を併用して概算する（実装済み：`services/metrics`）。計測不能区間は `not_evaluated` として正直に記録する。

```text
Candidate Review + Editorial Preview Review + Correction Instruction
+ QC Review + Final Review = Active Human Time
```

## 23. Review CommandsとReview Events

Review入力はDomain Commandとして受け取り、Validatorが新しいPlan Versionへ反映する。Event Logへ直接任意JSONを書かせない。

```json
{
  "event_id": "evt_01J...",
  "episode_id": "tokyo-001",
  "actor": "human:suda",
  "base_edit_plan_version": 7,
  "decision_id": "dec_01J...",
  "command": "adjust_source_span",
  "before": {"start_frame": 812, "end_frame": 1040},
  "after": {"start_frame": 760, "end_frame": 1110},
  "reason_category": "needs_more_breathing_room",
  "reason_text": "景色をもう少し長く見せたい",
  "idempotency_key": "...",
  "created_at": "2026-08-15T14:00:00+09:00"
}
```

`base_edit_plan_version` が現在Versionと一致しない場合はConflictとして止める。決定的ReducerがEventを順に適用し、新しいEdit PlanとTimeline IR、Previewを再生成する（実装済み：純粋関数reducer＋atomic versioned file writer、冪等性・非可換Event順序・無関係Decision不変性をテスト済み）。

Review Eventは三種類へ分ける：Episode Correction（この動画のみ）、Preference Evidence（傾向証拠として蓄積）、Profile Change Approval（人間が上位Profileへの反映を承認した記録）。

## 24. Review LearningとProfile Governance（Phase 5——現状は禁止）

Review Historyから現在の判断に近い例だけを検索して渡す機能、過去Edit Plan取得、Profile Change Proposal生成、Holdout Evaluationは**Phase 5の範囲であり、現状は実装を禁止している**（取得API不在をテストで保証）。

Profile自動更新は常に禁止である。同種の修正が一定数続き、再現可能なRuleへ落とせる場合にのみProfile Change Proposalを作り、人間が承認する。承認後も少数のHoldout Episodeで効果を確認する。

## 25. Genre、Channel、Episodeの設定

設定は `System Defaults → Genre Profile → Channel Profile → Episode Config → Approved Manual Override` の優先順位で解決する（実装済み：`services/config`）。Job開始時にResolved Configuration Snapshotを作り、以後のProfile変更から切り離す。

Genre Profileはanalysis/editorial/presentation_defaults/quality_rubricを持つ。Channel ProfileはSubtitle Style、Title Template、Intro/Outro、Color Management、Audio Loudness Target、Allowed Music Collection、Editorial Rubric、Review Budget、Supported Episode Contractを持つ。Episode ConfigはEpisode固有条件（timeline仕様、target_duration、special_notes、processing_policy＝Cloud可否とデータ種別）のみを持つ。

## 26. Asset Registry

BGM、SE、Intro、Outro、Overlay/Fusion Template、Grade/Fairlight/Render PresetをVersioned Assetとして管理する（実装済み：`services/presentation`）。

PathだけではAssetを固定できない。Content Hash、License Evidence（usage、territories、期限、Attribution、Content ID notes）、適用範囲を保存する。権利期限切れ・用途外使用・ファイル改変はrights Gateで阻止する。

**V4.2での確定事項**：Phase 3で登録・使用するAssetは自己生成の権利安全なA/Bフィクスチャ（`p3-brand-a/b`）のみである。実チャンネルのLogo/Font/BGM資産の登録は本計画の範囲外であり、実運用開始時に権利metadataとともに行う。

## 27. SecurityとData Policy

撮影素材、Transcript、OCR Textは信頼できるInstructionではない。画面内文字や発話に「この指示を無視せよ」と含まれてもAgent命令として扱わない。Media由来データとSystem Instructionの境界をTool Contractで固定する。

### 27.1 Production既定（実装済み）

- Resolve Bridge/BuilderはLoopbackまたは許可されたLocal Networkのみで公開（listener probeでloopback強制を検証）
- Job/Asset Directoryはrealpath解決済みPath Allowlist（symlink traversal拒否）
- Agentへ任意Shell、任意File Write、任意Network Accessを渡さない
- Read-only Media Query Toolと検証済みCommand Toolを分離
- API Key、License情報、個人情報をLog/Promptへ含めない（redaction実装）
- Cloudへ送るData種別をEpisode Policyで明示し、`local_only` Episodeでは該当StageをLocal処理へ切り替える
- 外部Model呼び出しのArtifact範囲・Provider・Request IDを監査Logへ残す
- Dependency、Model、Adapter、TemplateをVersion Pin（Toolchain Lock）

### 27.2 人間承認の入口（実装済み）

承認はpurpose束縛のOperation Recordとして記録する。目的は Editorial／Presentation／Privacy／Rights／Final／Publication／Manual Freeze の7種に分離され、目的と対象hashの組み合わせごとに独立したレコードになる。他目的のレコード流用、対象hash不一致、AI/非TTY経路からの発行はすべて拒否される。レコードにはpurpose、target hash、decision、actor ID、local uid、timestamp、superseded先を含む。

本機構は単一利用者ホスト向けの監査checkpointであり、暗号学的identity・non-repudiationではない。未記録実行の検出、台帳全体差し替えの検出、root権限改ざんの検出は保証しない。

### 27.3 Adversarial検証（実装済み）

Transcript/OCR prompt injection、untrusted facts、path/symlink traversal、network exposure、不正command/approval、Cloud data違反、secret/PII logging、改ざんevidence、Phase 4 import、license期限切れ、複数Writer、CLI全surfaceへの攻撃フィクスチャを実行し、ゼロ副作用での拒否と監査可能なfailureを証明する（`tests/security`）。

## 28. StorageとRetention

### 28.1 長期保存

Camera Original／Edit Sourceまたはその生成Recipe／Source Manifest／Conform Map／Job Manifest／Resolved Configuration／Asset Registry Snapshot／Committed Selection Plan／Committed Edit Plan／Final Timeline IR／Review Events／Build Report／Final QC Report／Final Render／Manual Finalization Package

### 28.2 期限付き保存

Proxy／Analysis Frame／Contact Sheet／Embedding／Preview Render／Resolve Package／Intermediate Timeline／Temporary Audio

再生成可能でも、障害調査中のJobは消さない。RetentionはJobの `FROZEN`／`FAILED`／`ACTIVE` 状態と連動させる（実装済み：`services/retention`）。state-aware policy、investigation hold（ACTIVE/NEEDS_HUMAN/FAILEDは削除ブロック）、dry-run、no-follow、hash/registry reconciliation、削除監査を備える。

## 29. 計測指標

### 29.1 最上位指標

- Active Human Timeの中央値とP90
- Supported Episode Coverage Ratio
- First-pass Editorial / Final Acceptance Rate
- Blocking Defect Rate

30分という単一値だけでは、難しいEpisodeを契約外へ追い出したり、大失敗を平均で隠せたりできる。CoverageとP90を同時に見る。

### 29.2 実装済み計測（V4.2で確定）

append-only Eventから導出する指標を実装した（`services/metrics`）：session/review/correction/QC/final時間、median/P90＋サンプル数、eligibility/Coverage、first-pass率、修正件数、Build/QC統計、コスト/ストレージ、Decision→Source→Review→BuildのEnd-to-End trace。idle・自己申告・log欠落は明示的に記録し、データ不足は `not_evaluated` として報告する。

**現状**：技術フィクスチャのみで計測されており、実Episode KPIは未評価である。合成データの指標を実人間時間と混同しない。

### 29.3 Structured Correction Coverage

```text
DaVinci Resolveを直接操作せず、Edit Plan、Profile、Episode Config、
Template、Asset Registry、Manual Overrideの変更として表現できた修正要求の割合
```

長期目標は90%以上。無理に不自然なSchemaへ押し込まず、Manual Finalization理由の蓄積をSchema拡張候補とする。

## 30. 実装PhaseとGate

Phaseは機能数ではなく不確実性を一つずつ潰す順に並ぶ。**V4.2では実際に運用されたGate体系を正本とする。**

### 30.1 Gate凍結プロトコル

各Phaseについて、(1) 判定基準を定義し、(2) Toolchain Lock・Fixture・Golden値を**実装から独立に**生成し（stdlibのみ・`services` import禁止・AST/import監査付き）、(3) canonical JSON Policy payload（`schema_version`、`record_type=gate_policy`、`purpose=gate_policy_freeze`、gate_id/version、親Gate結果hash群、基準、参照hash）を凍結する。canonical JSONはfloat/NaN禁止・キーソート・compact UTF-8で、`sha256(canonical_bytes)` を保存する。同一バージョンの再凍結は拒否される。凍結直前に必ず `services.toolchain.verify` が該当Lockのsmoke検証を実行する。

### 30.2 Gate体系と現在状態

| Wave | Gate ID | Policy File | 内容 | 主要閾値 | 状態 |
|---|---|---|---|---|---|
| 0 | — | — | 最小Spikeハーネス（bootstrap、Resolve readiness probe、Gate schema、Evidence Ledger、0A fixture/policy凍結） | uv/CPython hash一致、preflight契約 | ✅ |
| 1 | `phase-0a` | `config/gates/phase-0a-v1.json` | Resolve Capability Spike：Media Import、Base Cut配置/readback、固定字幕、Intro/Outro、Audio Preset、Render、Item-level Build Report | Item Conformance 100%、全delta 0、6回Clean Build（Resolve再起動含む）＋部分失敗からの回復 | ✅ 通過 |
| 2 | `phase-0b` | `config/gates/phase-0b-v1.json` | Coordinate/Conform Spike：CFR24/29.97/59.94/VFR 2-3 cadence/Rotate90/Audio offset1024 | Golden座標100%一致、drop/duplicate全報告、字幕音声同期≤1 timeline frame | ✅ 通過 |
| 3 | `phase-0c` | `config/gates/phase-0c-v1.json` | Preview/自然言語Review Spike：remove/span/subtitle clear、曖昧2-target、lock conflict | 明確命令100%変換適用、曖昧自動適用0、ResolveなしでPlan/IR/Preview再生成 | ✅ 通過 |
| 4 | `control-plane-baseline` | `config/gates/phase-1-control-plane-v1.json` | 不変Artifactストア、lineage Registry、SQLite state/migration、CAS/Lease/直列化、config/policy/audit、TTY承認入口 | atomic publish、crash復帰、orphan reconcile、stale CAS拒否、lease排他、path/symlink拒否 | ✅ 通過 |
| 5–6 | `phase-1-technical` | `config/gates/phase-1-technical-v1.json` | Talking-head Editorial縦貫：5本のReferenceフィクスチャでIngest→ASR→解析→Query→Selection→Planner→Edit Plan→IR→Preview→Review loop | Must-Include/critical欠落0、構造化Command Coverage ≥80%、E2E完走 | ✅ 通過（技術） |
| — | **H1** | — | 人間だけの完了Gate：実素材talking-head Episodeを持ち込み、Preview確認→自然言語修正→TTY `EDITORIAL_APPROVED` 操作レコード記録 | 表示hash一致、TTY実行、実Episode（fixture代替禁止） | ✅ 完了 |
| 7 | `phase-2` | `config/gates/phase-2-v1.json` | Resolve Finalization縦貫：Package compile、Clean Builder、固定Presentation、Item conformance/drift、Render、QC、Final Review/Freeze経路。5障害注入（stale capability、partial build restart、same-duration wrong media、false render complete、blocking QC privacy） | Item Conformance 100%、critical/未分類failure 0、通常経路で手動Resolve UI不要、bounded recovery | ✅ 通過 |
| 8 | `phase-3` | `config/gates/phase-3-v1.json` | Presentation Layer：A/B Brand Profile差し替え、subtitle/overlay/audio/color profile、Preview/Final parity | 同一編集IRで構造drift 0、presentation hash差分宣言通り、rights違反0、Phase-2回帰100%、Builder内channel分岐なし | ✅ 通過 |
| 9 | — | — | 運用強化：retention/GC、KPI計測、Operator CLI/runbook、adversarial security、scope check | security suite全pass、loopback強制 | ✅ |
| 10 | release | manifest-v1 ＋ F1–F4 | クリーンルーム再生＋不変リリース候補生成＋独立検証 | 全レーンAPPROVE、candidate byte一致 | ⬜ **未完了** |

### 30.3 正準フィクスチャマトリクス

実行主体が独自のフィクスチャを発明することはない。以下が正準である。

- **0A**：`p0a-cfr30-fixed`（20秒 CFR30 bars/slate、48kHz pulse、linked A/V cuts、固定字幕/intro/outro/preset）
- **0B**：`p0b-cfr24`、`p0b-ntsc2997`、`p0b-ntsc5994`、`p0b-vfr-2-3-cadence`、`p0b-rotate90`、`p0b-audio-offset1024`
- **0C**：`p0c-remove-clear`、`p0c-span-clear`、`p0c-subtitle-clear`、`p0c-ambiguous-two-targets`、`p0c-locked-conflict`
- **Control Plane**：`cp-atomic-publish`、`cp-crash-before-rename`、`cp-orphan-reconcile`、`cp-stale-cas`、`cp-lease-expiry`、`cp-path-symlink-denial`
- **Phase 1**：`p1-ref-01-clean-ja`、`p1-ref-02-pauses-fillers`、`p1-ref-03-multi-take-must-include`、`p1-ref-04-linked-av-offset`、`p1-ref-05-review-mix`
- **Phase 2**：上記5本に各1つの名前付き障害を注入した `p2-stale-capability`、`p2-partial-build-restart`、`p2-same-duration-wrong-media`、`p2-false-render-complete`、`p2-blocking-qc-privacy`
- **Phase 3**：`p3-brand-a`、`p3-brand-b`（自己生成・権利metadata付き・編集hash同一）

Golden値の導出scriptは `tests/goldens/reference/<phase>/` 配下に置き、stdlibと共通helperのみimportし、`services` の実装出力を読まない。

### 30.4 H1チェックポイント

H1は自動QAでは満たされない人間だけのGateである。Operatorが実素材Episodeを1本供給し、Preview MP4を確認し、自然言語で修正指示を出し、ローカルTTYで対象hashを確認した上で `EDITORIAL_APPROVED` 操作レコードを記録・exportする。checkpoint Artifactにはeligibility、Source/Conform manifest、初回と最終のPlan/IR/Preview hash、Review Proposal/Event/version連鎖、policy/toolchain/model/request hash、表示target hash、TTY操作レコードhashが含まれる。H1未完了はPhase 2開始とリリース候補生成をブロックする。**本チェックポイントは既に完了している。**

### 30.5 Phase 4以降（未実装——現状の境界）

- **Phase 4 Travel/POV Assisted Mode**：Scene Detection、Motion Analysis、Visual Similarity、Shot Clustering、Visual Candidate Ranking、Privacy Candidate、J/L Cut、B-roll。完全自動SelectionではなくCandidate-assistedから始める。
- **Phase 5 Review Learning and Advanced QC**：Review Event/Past Plan Retrieval、Profile Change Proposal、Holdout Evaluation、AI QC Ranking、QC Review Budget、False Negative Sampling、Dedicated Review UI（必要性が実測された場合のみ）。
- **Phase 6 派生動画**：Shorts等。Media Store/Candidateは再利用し、Edit Planは別に作る。

## 31. 成功条件

### 31.1 技術的成功条件（本計画範囲——ほぼ達成）

- 再現性：Committed Edit Planと固定Toolchainから同じ構造のTimeline IRとResolve Timelineを再Buildできる ✅（Phase 0A/2でframe delta 0実証）
- 復旧性：OpenCode、MCP、Resolve、Model Providerのいずれかが停止しても最後にCommitされたArtifactから再開できる ✅（crash/restart/injection試験）
- 追跡可能性：Final Render上の各ItemをDecision ID、Source Span、Review Event、Build Resultへ追跡できる ✅
- 修正容易性：人間の修正の多くをResolve操作ではなくReview Commandと構造化Artifact変更として表現できる ✅（Coverage ≥80%）
- モデル交換可能性：Role契約とProposal/Commit分離により、Model交換時にArtifact連鎖が残る ✅（構造保証）
- NLE交換可能性：Timeline IRまでResolve非依存 ✅（Preview Rendererが同一IRを使用）
- セキュリティ：adversarial suite全pass ✅
- リリース検証：F1–F4独立承認 ⬜ 未完了

### 31.2 製品KPI（実運用後に評価——現状 not_evaluated）

- Active Human Time中央値30分以内への接近とP90低減
- Coverage Ratio（作りたいEpisodeの多くがContractに入ること）
- First-pass Acceptance Rate向上
- Structured Correction Coverage 90%以上

これらは実素材Episode群を運用して初めて測定できる。技術フィクスチャでの達成主張はしない。

## 32. ディレクトリ構成（実装ベース）

```text
video-pipeline/
  pyproject.toml          # uv管理、依存pin
  schemas/                # Pydantic v2契約から生成されるJSON Schema
  config/
    gates/                # phase-0a-v1.json … phase-3-v1.json（凍結済みGate Policy）
    toolchains/           # phase-*-v1.json（Toolchain Lock）
    system-defaults / genres / channels / contracts
  capabilities/
    resolve-21/           # matrix.json、fixtures/、reports/
  services/               # 約40モジュール（下記）
  tests/                  # 単体/プロパティ/E2E/security/release
    fixtures/manifests/<phase>/   # Phase別入力manifest
    goldens/reference/<phase>/    # 独立Golden導出（stdlib限定）
    qa/todo-N.json               # Manual-QA実行マトリクス
  docs/runbooks/          # phase-0a..3、control-plane、ops-metrics、ops-retention、ops-final-approval

jobs/<episode_id>/        # 実行時に生成（v4.1 32章の構成を踏襲）
  episode.yaml, job-manifest.json, source-manifest.json, conform-map.json,
  resolved-config.json, media.duckdb,
  artifacts/{selection-plan,edit-plan,timeline-ir,resolve-package,build-report,qc-report}/,
  review-events.jsonl, sources/, analysis/, contacts/, proxies/, previews/, renders/,
  manual-finalization/
```

主要servicesモジュール：`contracts`（strict型＋Schema生成）、`evidence`（Attempt Ledger）、`gates`（Policy検証）、`toolchain`（Lock検証）、`fixtures`（materialize/freeze）、`spike`（0A–0C Gate runner）、`ingest`／`normalize`／`conform`（座標系）、`analyze`（ASR/候補解析）、`media_query`（DuckDB Index）、`editorial`（Director境界）、`validate`（Selection/Edit Commit）、`plan`（Constraint Planner/Edit Plan）、`compile`（Timeline IR/字幕Cue）、`preview`、`resolve_bridge`／`resolve_adapter`／`build`（Bridge/Package/Clean Builder/Render）、`qc`、`review_command`（Translator/Reducer）、`final_review`／`manual_finalization`、`approvals`（TTY操作レコード）、`job_runner`（State Machine/Stage Runner/Gate runner）、`artifact_store`／`artifact_registry`、`config`／`policy`／`audit`／`security`、`metrics`、`retention`、`release`（replay/verify_candidate/finalize_candidate）、`cli`（operator表面）、`qa`（Todo QA runner）。

## 33. 再現性とリリース（V4.2で新設）

### 33.1 リリース候補（Release Candidate）

- Git full SHAに紐付いた読み取り専用ディレクトリとして生成する
- `manifest-v1`：compact UTF-8 JSON、path-sorted entries `{path, size, sha256}` のみ。絶対パス・`.`/`..`・NUL・backslash・symlink・非regular file・case-fold衝突を拒否。`manifest.json` 自身はentries外
- **candidate_id = manifestバイト列のSHA-256**
- temp書き込み＋fsync＋atomic renameで公開し、全ファイル/ディレクトリを読み取り専用へchmodしてからmanifestを再計算する

### 33.2 クリーンルーム再生

`services.release.replay` が候補抽出物に対して障害注入（partial build、resolve restart、stale state、false success、repeated interruption）とA/B Profile swapを含むフル再生を実行する。チャット履歴や開いたままのTimelineへの依存があれば失敗する。

### 33.3 最終独立検証（Fレーン）

| レーン | 内容 |
|---|---|
| F1 Plan compliance audit | 候補manifest/evidence照合、Todo1–66証拠とreceipt-bound Todo67の検証、identity再計算 |
| F2 Code quality review | 候補抽出物へのfrozen lint/type/offline test実行とidentity再計算 |
| F4 Scope fidelity | controller所有Semgrep ruleでPhase4+シンボル（travel/POV/review-learning/dedicated-UI/shorts）、分散基盤import、内部Resolve DB/複雑Fusion API、AI承認権限、Builder channel分岐を拒否 |
| F3 Real manual QA | F1/F2/F4承認後、実機Resolve lease取得、障害注入再生、render frame anchor検証、review-work 5レーン＋debugging audit、Global Review Report v1（6報告）とFinal TTY受領 |

すべてのレーンが同一full SHA／同一candidate_idで `VERDICT: APPROVE` を出すまで完成とみなさない。レビュー後のソース変更は当該SHAのレビューを無効化する。

### 33.4 Commit規律

TodoごとにConventional Commitでatomic commitし、full SHAを実行台帳へ記録する。Gate failureは後続commit groupを停止させ、閾値緩和による回避を禁止する。H1のprivate media/操作レコードと生成物（jobs/renders）はcommitしない。

## Appendix A. V4.1→V4.2変更対照表

| 項目 | V4.1 | V4.2 | 変更理由 |
|---|---|---|---|
| Control Plane | アーキテクチャ要素 | 独立Gate `control-plane-baseline` として検証 | Spike期の暫定機構からProduction基盤への昇格を検証可能にするため |
| 人間承認 | EDITORIAL_APPROVED概念 | TTY専用CLI＋purpose束縛Operation Record（7目的） | 承認を改ざん・流用しにくい監査証跡へするため |
| PRESENTATION_APPROVED | 存在しない | Preview↔Final parity承認として追加 | 見た目差し替え時の編集承認の妥当性を保つため |
| Idempotency Key | stage＋input hashes＋runner version | ＋code_snapshot_id | コード変更後の誤った成功再利用を防ぐため |
| Gate Policy | 定性的Exit Criteria | canonical JSON凍結プロトコル＋数値閾値 | 観測後の期待値書き換えとGate緩和を構造的に防ぐため |
| Golden値 | 「Golden Valueと一致」 | 実装から独立な導出＋AST/import監査 | 自己成就的なテストを防ぐため |
| Evidence | Observability言及 | fsync前投入・hashチェーン台帳＋限界明記 | 実行証跡を改ざん検知可能にしつつ過剰主張を避けるため |
| Toolchain | Version Pin方針 | バイナリ/モデルhashまでのPhase別Lock | 「同じToolchain」を検証可能な定義にするため |
| フィクスチャ | 特徴の列挙 | 正準IDマトリクス固定 | 実行主体による安易な代替を防ぐため |
| get_review_examples | Query Interface例示 | Phase 5機能として除外（不在をテスト保証） | Review Learning先行実装を防ぐため |
| Privacy QC | AI候補抽出の記載 | 手動申告＋人間Gateのみ（自動検出はPhase 4） | 権利・プライバシー判断の人間責任を早期から保持するため |
| AI QC / Visual Judge / Diagnostic Assistant | ロール定義 | ロール定義維持＋未実装を明記 | 実装済み範囲と将来範囲を混同させないため |
| リリース | 成功条件の一項 | manifest-v1候補＋クリーンルーム再生＋F1–F4 | 「動く」ではなく「同一バイトで再構築・独立検証済み」を完成条件にするため |

## Appendix B. 初期リスク登録簿（更新）

| Risk | 影響 | 対策 | Gate | V4.2時点の評価 |
|---|---|---:|---|---|
| AIのShot採否が人間の好みと合わない | 高 | Preview、Review Event | Phase 1 | 技術検証済み。実素材での精度は運用後に評価 |
| VFR/Mixed FPSでSource位置がずれる | 高 | CFR Mezzanine、Conform Map、Golden Fixture | Phase 0B | 解消（Golden一致・同期≤1 frame実証） |
| Resolve APIの機能不足 | 高 | Capability Matrix、Fallback Ladder | Phase 0A | 実測済み。制約ある機能はexternal/templateへ退避確認 |
| Build失敗原因の不明瞭さ | 高 | Item-level Build Report、Staging Timeline | Phase 0A | 解消（item delta 0・構造化report） |
| 自然言語Reviewが人間時間を食う | 高 | Preview MP4先行運用、頻出操作のみUI昇格 | Phase 1以降 | 構造化Command Coverage ≥80%。実時間は運用後に評価 |
| Profile Learningの過適合 | 中 | Change Proposal、Human Approval、Holdout | Phase 5 | 未着手（意図的） |
| Cloud処理での情報漏えい | 高 | Episode Data Policy、local-only、Audit Log | 全Phase | 対策実装済み・adversarial試験pass |
| Asset権利情報の欠落 | 高 | License Evidence、Content Hash、期限管理 | Phase 3 | 対策実装済み（自己生成fixtureで検証） |
| Manual変更を再Buildで失う | 高 | Drift Detection、Freeze、MF Package | Phase 2 | 対策実装済み |
| 汎用基盤開発の先行 | 高 | Phase Gate、Stop Criteria | 全Phase | 回避できた（scope fidelity検証をF4で予定） |

## References

[R1] Blackmagic Design, DaVinci Resolve Studio 21 support information, 2026.
[R2] DaVinci Resolve Scripting API documentation distributed with Resolve Studio 21.
[R3] samuelgursky/davinci-resolve-mcp — API coverage and limitations（title destination track、trim/move、transition、Fairlight細粒度操作等）.
[R4] FFmpeg Project, FFmpeg/ffprobe documentation — time_base、timestamp handling、CFR/VFR conversion.
[R5] OpenAI, structured outputs / Responses API documentation.
[R6] Z.ai, GLM-5.2 product information.
[R7] ggml-org/whisper.cpp（pinned commit `1fe009ca…`）およびモデル `ggml-large-v3-turbo.bin`（revision `5359861c…`）.
[R8] 本リポジトリ実行計画：`.omo/plans/foundation-video-pipeline.md`（OMO監査済み実行仕様・証跡契約）.

