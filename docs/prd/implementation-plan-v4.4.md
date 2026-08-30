# davinci-agent v4.4 差分改修実装計画

Status: Delta correction plan from the already-implemented v4.3 repository

Repository: `sudat/davinci-agent`

Audited baseline commit: `c37247e9c10fa7c1c2ca50b84f0d1296b50c9b81`

Date: 2026-08-23

Companion specification: `docs/prd/PRD_v4.4.md`

---

## 0. Current Execution Contract

この計画はv4.3を最初から実装し直す計画ではない。

v4.3のコードはすでに大規模に実装済みであり、最新監査時点では型・Lint・テストも健全である。v4.4では既存資産を壊さず、実素材で未検証のクリティカルパスだけを差分修正する。

### 0.1 Baseline

作業開始基準コミット:

`c37247e9c10fa7c1c2ca50b84f0d1296b50c9b81`

直前の品質修正コミットでは full suite `2993 passed / 0 failed`、ruff all-pass、basedpyright 0 errors が記録されている。

したがってv4.4の原則は次のとおり。

```text
working v4.3 capability
        |
        v
keep it
        |
        v
remove from critical path if unnecessary
        |
        v
modify only when real-episode proof identifies a blocker
```

### 0.2 Gate V44-2まではやってよいことを限定する

Gate V44-2 First Publishable Real Episodeまでは、原則として以下だけを実装する。

- 文書正本の修正
- representative real episodeの準備
- Editorial Feasibility Spike
- real evidence provider
- production editorial-model wiring
- Japanese evidence/subtitle quality validation
- Cockpit intakeから実pipelineへの配線
- Taste Seed v0への軽量化
- natural-language reviewの実用化
- real partial rebuild
- real Resolve finishing
- real publishability decision
- それらを阻害する実測された不具合修正

禁止:

- 新しい汎用基盤を先回りで作る
- 新しいgenre frameworkを作る
- pairwise/derived-profile機能をさらに拡張する
- 3フォーマットを揃えるためだけの機能を作る
- Production Kitをカタログ化する
- legacy fallbackを整理するためだけに削除する
- Gateに必要のない新Artifactを増やす

### 0.3 Pre-first-publish新規Artifact上限

新しいauthoritative artifactは原則2種類まで。

1. `EditorialGroundTruthV1`
2. `ProductProofReportV1`

それ以外は既存v4.3 Artifactを再利用するかruntime viewで処理する。

---

# 1. Repository audit: v4.3で実際にできていること

## 1.1 すでに実装済みなので再実装しない

現行treeには以下の主要packageが存在する。

```text
video-pipeline/services/
  media_intelligence/
  editorial_v2/
  reference_learning/
  production_kit/
  creative_plan/
  mcp_client/
  mcp_execution/
  episode_cockpit/
  publish/
  channel_learning/
  qc/
  metrics/
  ...

cockpit/
  Next.js app
  components
  lib
  tests
```

v4.3の主要機能は既にコード化されている。

## 1.2 MCPは実機Fit済み

`video-pipeline/capabilities/v4.3/mcp-fit.json` には Resolve 21.0.4 + `davinci-resolve-mcp` 2.98.3 の実機結果が記録されている。

v4.3実績:

- 22 probes
- 16 accepted
- 6 failed

failed:

- `title-text-plus`
- `transition-path`
- `audio-property-operation`
- `bgm-track-ducking`
- `edit-engine-selects`
- `alternate-shot-similarity`

accepted側には、base placement/readback、Fusion composition、punch-in、voice isolation、DRX grade、render、analysis standard pass、deep-shot analysis等が含まれる。

結論:

MCPを「使えるかどうか」のゼロイチ調査は完了している。

v4.4で必要なのは、実日本語素材に対するanalysis content qualityの検証である。

## 1.3 MCP subtitle acceptedの解釈に注意

現行 `mcp-fit.json` の subtitle probe は `accepted` だが、readbackには synthetic mediaにspeechがなく、実subtitle generation自体はexerciseされていない旨が記録されている。

したがって日本語字幕のproduction acceptanceは未完了と扱う。

---

# 2. Repository audit: v4.4で最優先に直すべき事実

## 2.1 P0: `AGENTS.md` / `CLAUDE.md` がv4.1を正本としている

現行ファイルは `docs/prd/PRD_v4.1.md` をbinding specとして参照し、さらに「基幹システムを先に完成させる」「実動画は後」というv4.1時代の優先順位を残している。

これは現在のv4.3実装状態ともv4.4方針とも矛盾する。

最初のcommitで修正する。

変更:

- binding specを `docs/prd/PRD_v4.4.md` に変更
- `implementation-plan-v4.4.md` を現在のexecution contractとして明記
- Foundation-first記述を削除
- Gate V44-2までPOST-PUBLISH要件を実装しない規律を追加
- Cockpit / real episode / editorial feasibilityを現優先事項へ変更
- v4.3 historical evidenceは保存する旨を追加

対象:

- `AGENTS.md`
- `CLAUDE.md`
- 必要ならREADME/operator docsのversion reference

## 2.2 P0: Production Editorial Directorのdefaultがheuristic

`video-pipeline/services/editorial_v2/director_v2.py`

現状:

```text
llm_call is None
        |
        v
heuristic_planner
```

これはcontract testには有効だが、プロダクトの「AIが面白い場面を判断する」を証明しない。

v4.4変更:

- production runtime用 `EditorialModelProvider` / adapterを既存seamへ接続
- production configでmodel/provider/pinを明示
- product runではheuristic fallbackを暗黙利用しない
- heuristic pathは `test` / `diagnostic` modeとして保持
- provider unavailableなら明示BLOCKEDまたはoperator-approved fallback

新しい大規模frameworkは作らない。既存 `llm_call` seamを使う。

## 2.3 P0: Moment Deep Reviewがplaceholder

`video-pipeline/services/media_intelligence/moment_review.py`

現行docstring自身が、assessmentをdeterministic placeholderと明記している。

現在の主なprovider:

- `SyntheticFrameExtractor`
- `SyntheticTranscriptLookup`
- `SyntheticAudioContext`
- `_heuristic_assessment`

v4.4変更:

- synthetic providerはunit/regression用として残す
- real episode用provider bundleを追加
- real dense frames / short clip contextを取得
- real transcript lookupを接続
- real audio contextを接続
- action/reaction/timing assessmentをactual multimodal providerで作成
- lineageにprovider/model/version/costを記録
- product gateではsynthetic lineageを拒否

候補ファイル構成:

```text
services/media_intelligence/
  moment_review.py              # existing contracts remain
  moment_review_real.py         # production provider adapters
```

既存Artifact schemaをできるだけ維持する。

## 2.4 P0: 現行Real Episode 0はeditorial validationに不適切

`video-pipeline/capabilities/v4.3/runs/probes/episode0-freeze-manifest.json`

記録上、現行real-01はDJI clipでtranscriptがなく、full editorial chainがliveで回せない。

v4.4では新規 `v44-real-01` を用意する。

要求:

- operatorが実際に作りたい次の動画に近い
- Japanese speechを含むなら実speechを含む
- 必要ならB-roll / alternate momentsを含む
- boring / redundant / valuable momentsが実在する
- proper nounを含む

素材本体はprivateに保つ。

推奨:

```text
video-pipeline/private/reference-episodes/v44-real-01/
  episode.json
  sources/
  ground-truth.json
  runs/
```

リポジトリへcommitするのはmanifest/hash/protocolだけでもよい。

## 2.5 P0: Gate V43-1のrecall結果はred flag

`video-pipeline/capabilities/v4.3/runs/gate-v43-1/gate-summary.json`

現在記録:

- screen-product: `miss_rate=1.0`, `signal=degraded`
- speaker-broll: `miss_rate=1.0`, `signal=degraded`
- visual-first: `miss_rate=1.0`, `signal=degraded`

この値はsynthetic placeholder chain由来なので「実systemが100%見逃す」と断定してはいけない。

しかし、このGateをeditorial qualityの成功証拠として扱ってもいけない。

v4.4ではV43-1をhistorical mechanism evidenceとして凍結し、V44-0のreal human ground truthで置き換えて評価する。

## 2.6 P0: `benchmark.py` はmechanism benchmark

`services/media_intelligence/benchmark.py` は明示的にsynthetic providerを使い、real 30/90/180 minute測定は別工程と記載している。

これをproduct gateに流用しない。

保持用途:

- determinism regression
- cache invalidation
- progressive scheduling mechanism

新用途:

- V44-0ではreal providerを使う別harnessを追加

## 2.7 P0: Cockpit intakeはjobを作るだけ

`services/episode_cockpit/episode_ops.py:create_episode`

現状:

- source folder validation
- deterministic episode id
- job row作成
- brief draft保存

まで。

normal operator UXとして必要な「動画を作る」を押した後のreal pipeline launchがここでは完成していない。

v4.4ではCockpit intakeから既存Job Runner / stage machineryへ実workをenqueue/startする。

## 2.8 P0: Cockpit acceptanceはsynthetic fast path

v4.3のacceptance harnessはUI mechanicsをよく検証しているが、`PREVIEW_READY`までStateStore/preview/review-store/ledgerをseedするfast pathを使う。

そのテストは残す。

追加でv4.4 live acceptanceを作る。

条件:

```text
source folder
-> cockpit create
-> actual processing
-> preview
-> NL correction
-> rebuild
-> final preview
```

内部state seed禁止。

## 2.9 P0: Review chatはregex parser

`services/episode_cockpit/review_chat.py`

現状:

- Japanese-first deterministic pattern matching
- known `ReviewCommandKind`
- timestamp parsing
- ambiguity confirmation
- validated commit/rebuild lineage

これは安全な土台なので残す。

不足:

- 自由度の高い自然言語
- transcript/visual descriptionによるtargeting
- 複合表現
- bounded vocabulary外の自然な言い換え

v4.4ではLLM interpretationを既存structured contractの手前に追加する。

LLMはcommand execution authorityを持たない。

---

# 3. Existing module disposition

## 3.1 KEEP AND USE NOW

| Area | Current module | v4.4 treatment |
|---|---|---|
| artifact/state | artifact_registry, artifact_store, job_runner | Keep |
| conform/time | normalize, conform, contracts | Keep |
| MCP transport | mcp_client | Keep |
| MCP execution | mcp_execution | Keep; use live in product gate |
| MI canonical model | media_intelligence/models.py | Keep |
| query | media_query v2 | Keep |
| editorial contracts | editorial_v2 schemas/validation/commit | Keep |
| creative plan | creative_plan | Keep |
| preview | preview v2 | Keep |
| quality report | creative_plan/quality_domains.py | Extend applicability only as needed |
| QC | qc | Keep |
| Cockpit | episode_cockpit + `cockpit/` | Extend real wiring |
| production kit | production_kit | Keep and bootstrap on real footage |
| publish | publish | Keep |
| approvals/security | approvals/policy/security/audit | Keep |

## 3.2 MODIFY NOW

- `editorial_v2/director_v2.py` production-model wiring
- `media_intelligence/moment_review.py` production provider path
- real editorial feasibility harness
- Japanese evidence-quality checks
- `episode_cockpit/episode_ops.py` real pipeline launch
- `episode_cockpit/review_chat.py` LLM interpretation adapter while preserving deterministic validation
- `quality_domains.py` if actual episode requires more explicit applicability for audio/color
- Production Kit preview generation on real Episode 0 footage
- live E2E acceptance
- docs authority

## 3.3 KEEP BUT REMOVE FROM FIRST-PUBLISH CRITICAL PATH

Already implemented but not required to obtain first publishable episode:

- `reference_learning/feature_extract.py`
- `reference_learning/pairwise.py`
- `reference_learning/derive_profile.py`
- broad cross-episode derived-profile workflows
- channel-learning performance optimization
- three-format validation harnesses
- phase-9 KPI/legacy cleanup workflows
- legacy removal decision machinery

Do not delete.

Feature flag or runtime policy may bypass these before V44-2.

---

# 4. Phase 0: v4.4 authority rebaseline

Goal: coding agentが古い優先順位へ戻らない状態を作る。

## Task 0.1 Add documents

Add:

```text
docs/prd/PRD_v4.4.md
docs/prd/implementation-plan-v4.4.md
```

Do not rewrite historical `PRD_v4.3.md` / v4.3 plan.

## Task 0.2 Fix agent instructions

Update `AGENTS.md` and `CLAUDE.md`.

Required wording concept:

```text
Binding product spec: docs/prd/PRD_v4.4.md
Current execution plan: docs/prd/implementation-plan-v4.4.md
Current priority: First Publishable Real Episode
Do not implement POST-PUBLISH requirements before Gate V44-2 unless a measured blocker requires them.
```

## Task 0.3 Freeze baseline evidence

Record:

- baseline commit SHA
- current test summary
- current `mcp-fit.json` hash
- current V43 gate evidence paths
- current config/backend state

No need for a new generalized release framework. Existing release/freeze utilities should be reused.

Exit:

- a coding agent reading only root instructions cannot conclude that v4.1 is binding or that foundation work should precede real footage.

---

# 5. Phase 1: Editorial Feasibility Spike + MCP Evidence Quality Fit

This is the highest-priority v4.4 engineering work.

## 5.1 Prepare `v44-real-01`

Operator supplies real footage.

Create an episode manifest using existing ingest/conform utilities.

Do not require full manual edit.

## 5.2 Add `EditorialGroundTruthV1`

Minimal schema.

Suggested shape:

```json
{
  "schema_version": "editorial-ground-truth-v1",
  "episode_id": "v44-real-01",
  "anchors": [
    {
      "start_frame": 0,
      "end_frame": 300,
      "label": "must_keep",
      "note": "..."
    }
  ],
  "continuation_question": "この選択なら続きを作る価値があるか"
}
```

Labels:

- must_keep
- good_optional
- must_remove
- uncertain

This is measurement ground truth, not an edit plan.

## 5.3 Wire actual editorial model

Reuse `DirectorV2.llm_call` seam.

Implementation requirements:

- typed adapter
- model/provider version pin
- timeout and failure handling
- strict response validation through existing prompt_v2 contracts
- zero direct Resolve/job mutation
- no fallback to heuristic without explicit run mode

Suggested config:

```text
config/editorial-runtime.json
```

Possible modes:

- `production_model`
- `heuristic_diagnostic`

Default for v4.4 product runs: `production_model`.

## 5.4 Implement real Moment Deep Review provider bundle

Use existing `ReviewProviders` contract if possible.

Required adapters:

- actual frame/clip extractor
- actual transcript lookup
- actual audio context
- actual multimodal assessment provider

Do not change `MomentDeepReviewV1` unless real data cannot be represented.

Preferred sequence:

```text
existing review window
-> real frames / short clip
-> transcript context
-> audio context
-> multimodal assessment
-> existing record_review validation
```

## 5.5 Run Experiment A

Input:

- real episode
- approved Episode Brief
- actual coarse analysis
- actual editorial model
- no deep review

Output:

- selected candidates
- story draft
- reasons/evidence refs
- metrics against operator anchors

## 5.6 Run Experiment B

Same inputs plus progressive Moment Deep Review.

Compare:

- Must-Keep recall
- Must-Remove retention
- wrong decisions
- Deep Review lift
- wall clock
- provider cost
- operator continuation verdict

## 5.7 Product-proof report

Add only one combined report artifact:

`ProductProofReportV1`

It should carry:

- run identity / commit / model pins
- experiment A metrics
- experiment B metrics
- evidence-quality metrics
- Japanese transcript metrics when applicable
- operator verdict
- diagnosis state
- pass/fail against predeclared criteria

## 5.8 NO-GO diagnostic arm

If B fails:

1. choose failed high-impact regions,
2. prepare human-rich/corrected evidence,
3. run same editorial model,
4. classify evidence failure vs reasoning failure.

Then allow one targeted correction and one repeat by default.

`v44-real-01`の実測診断は§5.10のとおりである。Arm Cは、§5.10の修正後Arm Bが依然として不合格の場合のみ実行する。

Do not continue building downstream features while V44-0 remains unresolved.

## 5.9 Japanese evidence-quality test

Use a manually corrected sample.

Measure separately:

- transcript CER
- proper noun recall
- omitted utterance count
- duplicated utterance count
- timestamp alignment error

Do not mix with subtitle line-breaking quality.

## 5.10 実測診断と限定修正（v44-real-01 arm-B-r2）

`arm-B-r2`（本物のDirectorV2 three-pass、GPT-5.6 Sol、Moment Deep Review付き、commit `138072c0bf`）は事前合格基準（PRD §6.6）に失敗した。実測値:

- must-keep recall 72/75（0.96、要求1.0）
- catastrophic removal 3件（要求0）
- must-remove残留 2/2（1.0、上限0.25）
- evidence品質: transcript CER 0.104、timestamp誤差 p95 5740ms、発話欠落31件、発話重複37件（固有名詞recallは1.0）

オペレーター継続判定はYESだったが、機械判定は不合格でpublishabilityは未記録であり、Gate V44-0は未達成である。あわせて、現行の最終出力は90度回転という独立blockerを抱えるため、現時点で公開可能とは扱わない（向きの診断・修正は別課題とする）。

分類（PRD §21.1）: evidence生成・解釈の失敗である。欠落・重複・ずれたtimestampにより意思決定層が素材の忠実な写像を受け取れなかった。pipeline再構築の根拠ではなく、editorial reasoningやmodel選択の失敗の証拠でもない。

PRD §6.7の「1回の的を絞った設計修正と1回の再実行」の枠内で、evidence生成に対する次の限定修正のみを行う。役割はPRD §8.5と同じ4つである。

| 役割 | model / surface | 責務 | 境界 |
|---|---|---|---|
| 全編音声映像map/reduce | `gemini-3.7-flash`（Gemini Developer API） | 局所windowの一意和集合がEdit Source全体 `[0, source_duration)` を厳密に覆うmap確認と、episode単位のreduce 1回 | 実装前にlive-probeで保証する。Vertex AI等への黙替えおよびprovider/model fallbackは禁止。利用不可は型付きblocked |
| 的を絞った映像専属 | `glm-5v-turbo`（公式にサポートされる動画転送方式） | 決定論的またはGemini指定の不確実・高価値領域について、音声除去済みの対象clipのみ確認 | 編集判断は行わない。音声は送らない |
| fusion | 同一の`gemini-3.7-flash` pin | 局所/reduce結果と専属観察をprovider中立に融合 | 型付きpayload外の文章は信頼しない |
| 編集判断の唯一の所有者 | GPT-5.6 Sol（DirectorV2） | keep/remove/順序/編集意図の選択はこの役割のみ | 融合evidenceを検証・commitの前に消費する。どのmodelもJob State・Selection Plan・Edit Plan・Resolveに書き込まない |

権威artifactは融合済み`MomentDeepReviewV1`記録のみとし、段階別の来歴は既存記録内で検査可能とする。map/reduce/専属の実行時payloadは再生成可能な実行データであり、第三のauthoritative artifactは作らない（§0.3）。

Arm C（§5.8）は、この修正後のArm Bが依然として不合格の場合のみ実行する。Gate V44-0 → V44-1 → V44-2の順序は変更しない。

## 5.11 実測修正サイクル記録: r3分類とr4診断結果（v44-real-01、2026-08-31）

本節は§5.10の追記記録であり、§5.10のr2記述を書き換えない。サニタイズ済みの正本は
`video-pipeline/capabilities/v4.4/product-proof/v44-0/r3-failure-classification.md` と
`video-pipeline/capabilities/v4.4/product-proof/v44-0/r4-diagnostic-summary.json` に置く
（私有レポートはhash引用のみ、本文にトランスクリプト・素材パスは含めない）。

r3実測（A/B-r3レポートhash同上ファイルに引用）: must-keep recall 68/75、catastrophic 7件、
GT v1のmust-remove 2件が残留（2/2）。9件のエラーをアンカーID/区間でちょうど1回ずつ分類した結果は
5件が知覚・入力経路（末尾発話s96〜s99が候補発見されなかった3アンカーと、`optional`を削除扱いする
kept-span導入に起因する2アンカー）、2件が推論・不適格カット（Bのみで自由記述理由によりkeepから降格、
決定論的証拠なし）、2件が正解表の決定変更（a001/a002、2026-08-30のオペレーター決定でGT v2は77/0）。

§6.7の「1回の的を絞った修正」の枠内で、入力整合性とカット方針の境界のみを修正した
（権威ある全フレーム数に基づく候補完全性の完全一致検査、モデル呼び出し前のfail-closedな
トランスクリプト整列、決定論的な削除適格性）。pipeline再構築・provider/model変更は行っていない。

修正後の診断再実行A-r4/B-r4（GT v2、`operator_corrected_diagnostic`レーン、両アーム同一バインディング）は
93/93候補を提案かつ維持、must-keep 77/77（recall 1.0）、catastrophic 0、削除0、エスカレーション0。
must-remove次元は「測定不能（0アンカー）」と開示する。統合比較の編集差分は全て0。
r3の重要失敗クラスの再発はないため、§6.7 NO-GOは発動せず、Arm Cも不要のため未実行。

r4は診断証拠であり製品証明ではない。製品レーンのsystem-ASR readiness probeは
有料呼び出し前に型付き`asr-alignment-failed`で拒否された
（CER 0.10372340425531915 > 0.10、p95 5740.0ms > 500.0、欠落31 > 5、重複37 > 5。
閾値は実行前に凍結済み）。ASR整列を次の測定済みblockerとして記録する。
Gate V44-2は未達成（`gate_v44_2_passed=false`）のままであり、Resolve構築・公開・学習は開始していない。

## Gate V44-0

Pass only when:

- Must-Keep recall policy passes,
- no catastrophic Must-Keep removal,
- Deep Review value is understood,
- operator says the candidate selection is worth continuing,
- evidence quality is adequate or a known fallback exists,
- any major failure source is classified.

---

# 6. Phase 2: Minimal real vertical slice from Cockpit

Goal:

```text
source folder -> real first preview
```

No prebuilt MI artifact required from operator.

## 6.1 Wire Cockpit intake to orchestration

Current `create_episode` writes state + brief.

Extend the flow so `Create video` starts/enqueues the real stage chain.

Reuse existing Job Runner.

Do not make Next.js responsible for long-running work.

Recommended boundary:

```text
Cockpit HTTP request
-> create episode + enqueue/start job
-> return quickly
-> background/local worker advances stages
-> Cockpit polls existing state
```

If an always-on worker does not exist, add the smallest local runner necessary rather than a new distributed queue.

## 6.2 Build source-to-MI path

Normal path:

```text
source folder
-> source manifest
-> normalize/conform as needed
-> MCP/local analysis
-> reconciliation
-> MediaIntelligenceArtifact
-> index/query
```

No synthetic evidence in product run.

## 6.3 Taste Seed v0 projection

Reuse existing:

- `ReferenceSourceV1`
- `ReferenceAnnotationV1`
- domain-scoping validator
- reference ingestion

Do not call heavy aggregation by default.

Create an ephemeral retrieval/projection function that yields:

```text
operator comment
named domain
polarity
reference time range
small frame/clip context
optional transcript context
```

Feed this directly to Editorial Director prompt context.

Do not create a second reference database.

## 6.4 Generate actual Editorial Preview

Use existing Timeline IR / preview renderer.

First-preview acceptance must be based on the actual planned edit, not a placeholder render.

## Gate V44-1A

- Cockpit intake can reach PREVIEW_READY without manually preparing MI JSON
- model/evidence providers are production ones
- preview opens in Cockpit
- operator never sees required artifact IDs or JSON paths

---

# 7. Phase 3: Natural-language correction and real partial rebuild

## 7.1 Preserve existing deterministic safety shell

Keep:

- `ReviewCommandDraft`
- ambiguity confirmation
- validated commit path
- `AppliedCommand`
- `RebuildPlan`
- stage lineage

These are good v4.3 assets.

## 7.2 Add LLM interpretation ahead of existing validator

New flow:

```text
operator text + player position + nearby transcript/scene context
        |
        v
LLM interpretation proposal
        |
        v
existing typed ReviewCommandDraft
        |
        v
semantic validation
        |
        +-- ambiguous -> confirmation
        |
        v
existing commit/rebuild planning
```

Regex parser remains:

- fast path for simple known commands,
- offline fallback,
- regression oracle.

## 7.3 Targeting improvement

Add targeting sources only as needed by real Episode 0:

- current playback timestamp
- explicit timestamp
- nearby transcript phrase
- source/take identity
- visual description via Media Query

Do not invent a general visual-language agent framework before a real correction requires it.

## 7.4 Real partial rebuild

Current acceptance proves a rebuild intent/stage hint.

v4.4 must execute an actual correction through the affected stages and produce an updated preview.

Measure:

- correction interpretation time
- rebuild wall time
- whether unrelated stages were correctly skipped
- operator confirmation count

## Gate V44-1

Pass when one real operator correction results in an updated real preview through the normal Cockpit path.

---

# 8. Phase 4: Real Resolve finishing and Production Kit bootstrap

Use the already-implemented creative-plan and MCP execution stack.

Do not rebuild it.

## 8.1 Select only needed finishing domains

Determine episode applicability before building.

Example:

```text
editorial_construction: applicable
subtitle: applicable
 audio_finishing: applicable
color_finishing: applicable
framing_motion: applicable
 graphics_presentation: not needed
 delivery_qc: applicable
```

All domains remain explicitly represented, but `intentionally_not_needed` is valid when justified.

## 8.2 Small code change if audio/color applicability requires it

Current `quality_domains.py` already supports intentionally-not-needed for some domains but audio/color are plan-driven and may become blocked if no plan exists.

Only if a real episode demonstrates the need, extend `QualityFactsV1` / builder so truly inapplicable audio/color can be represented without fake plans.

Do not change the schema preemptively if a verified no-op plan is semantically correct.

## 8.3 Production Kit candidates on real footage

Reuse current registry/recipe selection.

For needed domains only:

- choose one or a few representative 10-20s Episode 0 regions,
- render candidate recipes on those regions,
- show A/B or A/B/C in Cockpit,
- operator picks or rejects.

No synthetic title card acceptance.

Record selected recipe via existing kit mechanisms.

## 8.4 Japanese subtitle live path

Run real Japanese speech end to end.

Validate:

- transcript
- proper nouns
- segmentation
- line breaks
- timing
- final render legibility

If MCP-native subtitle path is inadequate, use the existing accepted external/template fallback rather than blocking on MCP feature purity.

## 8.5 Live Resolve executor

Product gate must use live MCP/Resolve path, not the fake executor.

For known failed MCP operations, follow current fallback matrix.

Do not spend v4.4 critical-path time making all six failed MCP capabilities native unless Episode 0 is blocked by one of them.

## 8.6 Technical + editorial QC

Reuse existing QC.

Add real run evidence only where current checks are synthetic/scaffolded.

---

# 9. Phase 5: First Publishable Real Episode

## 9.1 Operator review

Operator watches final preview and chooses:

- publishable
- publishable after fixes
- not publishable

No numeric scorecard required.

Optional comments are captured.

## 9.2 If fixes are needed

Run the same natural-language review loop.

Do not create a separate final-review editing system.

## 9.3 Measure Bootstrap AHT

Record separately:

- ordinary edit review time
- Production Kit bootstrap time
- taste/reference calibration time
- troubleshooting time
- direct Resolve time

Run label: `bootstrap`.

各区分の意味（v44-real-01修正時に固定化、PRD §19.4と同一）:

- 5区分は既存のfinishing time-log契約（`TimeLogLineV1` のphase）として記録する。運用時の通常レビュー・Production Kit準備・テスト/参照キャリブレーション・障害対応・Resolve直接作業を混ぜない。
- 合計bootstrap AHT = 上記5区分の合計。
- direct Resolve時間は合計に1回だけ含めた上で、内訳として別途報告する（二重計上しない）。
- 未記入の区分は推定せずnullのまま残し、Gate V44-2を不合格にする。

Do not compare this raw number directly to steady-state <=30 minute target.

## Gate V44-2

Pass only if:

- real episode final video exists,
- operator says publishable,
- no unresolved technical blocker,
- no routine manual Resolve dependency was required,
- product-proof report identifies actual model/evidence pins,
- applicable quality domains are resolved.

This is the main product gate.

---

# 10. Phase 6: Publication proof

The publish subsystem already exists.

v4.4 work is validation, not redesign.

## 10.1 Use accepted final render

Build existing `PublishPackageV1` from the Gate V44-2 episode.

## 10.2 Operator publication approval

Require existing publication approval semantics.

## 10.3 Real upload/schedule

Validate one idempotent real path.

Can be:

- private,
- unlisted,
- scheduled,
- public only if operator explicitly chooses it.

Record remote video id / result in existing ledger.

## Gate V44-3

One approved real upload/schedule succeeds or a clearly external credential/platform blocker is documented.

---

# 11. Phase 7: Steady-state validation on actual next episodes

No synthetic genre quota.

Use the operator's next actual videos.

## 11.1 Label runs

`steady_state` only after the core Production Kit and reference conventions are reasonably stable.

## 11.2 Measure

Per episode:

- AHT
- TTFRP
- first-preview acceptance category
- correction count
- blocking-session count
- direct Resolve minutes
- Must-Keep misses discovered by operator
- full vs partial rebuild count
- publishability

## 11.3 Target

After enough steady-state episodes to make a median meaningful:

- median AHT <= 30 min
- <=2 normal human stops
- direct Resolve time ~0 normal path
- no recurring catastrophic Must-Keep misses
- first preview generally incremental rather than re-edit level

## Gate V44-4

Steady-state target demonstrated on actual next episodes.

---

# 12. Phase 8: Activate advanced taste/channel learning only after evidence

The code already exists.

The question is not whether it can be implemented. It is whether it improves the product.

## 12.1 Evaluate existing heavy reference-learning components

Candidate activation:

- `feature_extract.py`
- `pairwise.py`
- `derive_profile.py`
- cross-episode retrieval

Activation experiment:

```text
same/similar episode context
A: Taste Seed v0 only
B: derived profile / advanced learning

compare:
first-preview corrections
operator preference
AHT
```

If no meaningful benefit, leave advanced path optional.

## 12.2 Audience outcome remains separate

Do not convert retention/CTR automatically into operator taste.

Performance observations may propose a profile change but must remain distinguishable from operator preference evidence.

## Gate V44-5

Advanced learning is activated only with a measured benefit and explicit governance.

---

# 13. Detailed delta backlog by file/module

## P0 Documentation authority

### `AGENTS.md`

- replace v4.1 binding reference with v4.4
- remove stale "docs only / implementation not started" statements
- current priority = First Publishable Real Episode
- add FIRST-PUBLISH vs POST-PUBLISH discipline
- preserve artifact/single-writer/safety principles

### `CLAUDE.md`

Same authority/priority corrections.

### `docs/prd/`

Add v4.4 files; keep historical files.

---

## P0 Editorial model runtime

### `services/editorial_v2/director_v2.py`

- keep current contract
- make run mode explicit
- reject implicit heuristic in product mode
- inject production model adapter

### `services/editorial_v2/prompt_v2.py`

- only adjust prompts based on Spike failures
- add Taste Seed context in a bounded section
- keep media content as untrusted data

### New small adapter module if needed

`services/editorial_v2/model_provider.py`

Responsibilities only:

- provider call
- pin/version
- timeout
- response extraction
- no orchestration

---

## P0 Real Moment Review

### `services/media_intelligence/moment_review.py`

- preserve models and synthetic test providers
- mark product-vs-synthetic mode clearly

### New

`services/media_intelligence/moment_review_real.py`

- real frame/clip evidence
- real transcript lookup
- audio context
- multimodal assessment

### Tests

- real-provider contract fixture
- no synthetic lineage accepted by V44 product harness
- same source window remains deterministic identity

---

## P0 Product proof harness

Prefer one focused module instead of a new framework.

Suggested:

```text
services/metrics/v44_product_proof.py
services/cli/v44_product_proof.py
```

Responsibilities:

- load ground truth
- run A/B/C experiment arms
- compute editorial metrics
- compute evidence-quality metrics
- record operator verdict fields
- emit ProductProofReportV1
- enforce predeclared pass policy

Do not merge this into the synthetic v4.3 benchmark in a way that obscures its mechanism-only semantics.

---

## P0 Japanese quality

Reuse current ASR/transcript/subtitle components.

Add small evaluation utilities under tests/metrics or quality tooling.

Need:

- CER
- proper noun recall
- timestamp error
- subtitle cue review output

No full NLP evaluation platform.

---

## P0 Cockpit orchestration

### `services/episode_cockpit/episode_ops.py`

- intake returns quickly
- launches/enqueues real work
- persists current job authority

### Existing Job Runner

- reuse stages
- avoid long HTTP request execution

### `cockpit/`

- create flow shows real progress
- real preview appears when ready
- blocked reason actionable

---

## P0 Review interpretation

### `services/episode_cockpit/review_chat.py`

Keep regex + typed contracts.

Add provider path before/alongside `interpret_command`.

Suggested small adapter:

`services/episode_cockpit/review_interpreter.py`

Input:

- text
- current timestamp
- nearby transcript
- optional media-query context

Output:

- existing `ReviewCommandDraft`

No direct mutation.

### Partial rebuild

Ensure `POST /review-chat/apply` / rebuild path schedules and executes the actual affected stage chain in live acceptance.

---

## P1 Production Kit real previews

Use current kit registry.

Add a preview utility that renders recipe candidates over fixed actual Episode 0 snippets.

Do not add dozens of recipes.

---

## P1 Quality domain applicability

Only if real episode requires it:

### `services/creative_plan/quality_domains.py`

Potential additive fields:

- `has_audio`
- explicit assessment/no-op reason

Goal:

truly irrelevant domain can be `intentionally_not_needed` without fake execution evidence.

Preserve seven explicit entries and blocked/manual semantics.

---

## P1 Live Cockpit E2E

Add a marked slow/live test separate from fast seeded Playwright acceptance.

Possible structure:

```text
cockpit/tests/live-real-episode.spec.ts
video-pipeline/tests/live/test_v44_real_episode.py
```

Do not run on every unit-test cycle if expensive.

Record evidence instead.

---

## POST-PUBLISH

Do not modify before V44-2 absent a measured blocker:

- `reference_learning/feature_extract.py`
- `reference_learning/pairwise.py`
- `reference_learning/derive_profile.py`
- channel-learning expansion
- format generalization
- legacy removal

---

# 14. Gate checklist details

## V44-0 Editorial Feasibility + Evidence Quality

Machine-verifiable:

- ground truth loaded
- run uses production model mode
- run uses non-synthetic evidence provider
- A and B use same source/brief/model pin
- Must-Keep recall computed
- Must-Remove retention computed
- B vs A delta computed
- Japanese evidence metrics present if dialogue
- operator verdict field filled

Human-verifiable:

- anchor labels were created before inspecting A/B outcomes
- "continue" verdict is genuine operator judgment

Fail behavior:

- downstream expansion blocked
- run diagnostic C

## V44-1 Real Review Loop

- source folder entered via Cockpit
- no prebuilt MI input from operator
- actual preview generated
- one natural-language command applied
- actual partial rebuild occurs
- updated preview generated
- ambiguous command requires confirmation rather than guessing

## V44-2 First Publishable

- live Resolve path/fallback
- applicable domains resolved
- no technical QC blocker
- no editorial QC blocker
- operator publishable=yes
- Bootstrap AHT recorded
- direct Resolve time recorded

## V44-3 Publication

- final approval
- publication approval
- idempotent upload/schedule proof

## V44-4 Steady State

- actual next episodes
- AHT target evaluated only on steady-state runs

---

# 15. Test strategy

## 15.1 Keep existing fast test suite green

v4.4 does not trade product validation for regression instability.

Required on ordinary commits:

- ruff
- basedpyright
- existing pytest suite
- existing frontend tests

## 15.2 Add three test tiers

### Tier A: contract/unit

- production adapter response validation
- Taste Seed projection
- review interpreter structured output
- ProductProof metric calculations

### Tier B: recorded-real fixtures

Record sanitized outputs from the actual providers so regressions can be tested offline.

Do not pretend recorded fixtures prove the current provider is live.

### Tier C: live product acceptance

Requires:

- actual source media
- actual model provider
- actual analysis path
- actual Resolve host where required
- actual operator verdict

Only Tier C can pass V44 product gates.

---

# 16. Metrics implementation

Reuse current metrics package where possible.

Do not create one file per metric.

`ProductProofReportV1` should be the primary v4.4 experiment report.

Suggested fields:

```text
run
  episode_id
  commit_sha
  run_kind
  model_pin
  analysis_provider_pin

editorial
  must_keep_total
  must_keep_recalled
  must_keep_recall
  must_remove_total
  must_remove_kept
  must_remove_retention
  redundancy_errors

progressive_lift
  A metrics
  B metrics
  deltas

evidence_quality
  transcript_cer
  proper_noun_recall
  timestamp_error
  sampled_factual_errors

operator
  continuation_yes_no
  publishability
  comments

efficiency
  wall_clock
  AHT
  TTFRP
  provider_cost
```

Avoid fake precision. Missing human metrics remain null/pending until operator supplies them.

---

# 17. Taste Seed implementation details

The full reference-learning implementation already exists.

Do not remove it.

For pre-first-publish runtime, add a simple selection policy.

Pseudo-flow:

```text
approved ReferenceAnnotationV1
  where scope in {episode, series/channel if explicitly allowed}
  and named domain relevant to current planning pass

        |
        v
resolve ReferenceSourceV1 + optional time range

        |
        v
extract small bounded context

        |
        v
attach operator rationale + polarity

        |
        v
Editorial Director context
```

Do not invoke `derive_profile()` by default in FIRST-PUBLISH mode.

Pairwise UI may remain visible only if explicitly requested, but it must not block episode creation or first preview.

---

# 18. Production Kit bootstrap details

The kit is already versioned.

v4.4 adds acceptance behavior, not registry architecture.

For each real needed domain:

1. choose actual Episode 0 snippet,
2. render candidate recipe(s),
3. show video previews,
4. operator selects or rejects,
5. record accepted recipe/version,
6. use it in full episode.

Bootstrap time is measured separately.

If existing default looks good enough, the operator may simply accept it and no new variant is generated.

---

# 19. Rollback and safety

## 19.1 Preserve v4.3 historical evidence

Never rewrite `capabilities/v4.3/runs/*` to make v4.4 look successful.

Create separate v4.4 evidence paths.

Suggested:

```text
capabilities/v4.4/
  product-proof/
  live-runs/
```

## 19.2 Preserve heuristic and synthetic modes

They remain useful for testing.

They are not production defaults.

## 19.3 Preserve legacy Resolve fallback

Do not remove legacy direct bridge during v4.4 product proof.

Known MCP failures make fallback valuable.

## 19.4 Feature flags

Use existing backend/config feature flags where possible.

Need explicit runtime distinctions such as:

- production vs diagnostic editorial
- real vs synthetic review providers
- Taste Seed vs advanced taste profile

Avoid hidden automatic mode switches.

---

# 20. What must not happen in v4.4

Do not respond to a bad Episode 0 edit by immediately adding more schemas.

Do not call a Gate successful because models serialized and tests passed.

Do not use `miss_rate=1.0, degraded` synthetic evidence as if recall is healthy.

Do not call an `accepted` MCP subtitle probe production-ready for Japanese speech when speech was not exercised.

Do not allow `llm_call=None` heuristic output to stand in for product editorial intelligence.

Do not use seeded `PREVIEW_READY` Cockpit acceptance as proof of raw-folder-to-preview integration.

Do not require full pairwise/derived taste learning before the first publishable episode.

Do not require all known failed MCP features to be fixed natively before publishing.

Do not require unrelated presentation domains on an episode that does not need them.

Do not count Bootstrap AHT against the steady-state 30-minute promise.

---

# 21. Recommended commit sequence

This is intentionally narrower than v4.3.

```text
1. docs(v4.4): rebaseline authority + agent instructions

2. test(v4.4): add representative real-episode protocol + ground truth schema

3. feat(editorial): wire production model through DirectorV2 seam

4. feat(media-intelligence): real Moment Deep Review providers

5. test(v4.4): A/B editorial feasibility + MCP Japanese quality report

----------- Gate V44-0 -----------

6. feat(cockpit): intake launches real pipeline

7. feat(cockpit): LLM review interpretation over existing command validator

8. feat(review): execute real partial rebuild from Cockpit

----------- Gate V44-1 -----------

9. feat(finishing): real-footage Production Kit preview/selection

10. fix(subtitle/audio/color): only real blockers found on Episode 0

11. test(live): real Resolve full episode + applicable quality gate

12. review(v4.4): operator publishability record

----------- Gate V44-2 -----------

13. test(publish): one approved real YouTube upload/schedule

14. measure: actual next episodes steady-state

15. evaluate: advanced taste/channel learning activation
```

If Step 5 fails, do not proceed to Step 6 by default.

---

# 22. Definition of implementation completion

The v4.4 code change is not complete when the new modules exist.

It is complete when the following can be demonstrated on the operator's real workflow:

```text
Cockpit
  source folder + brief + optional reference
        |
        v
real analysis
        |
        v
actual model editorial plan
        |
        v
useful preview
        |
        v
natural-language correction
        |
        v
real partial rebuild
        |
        v
DaVinci finalization
        |
        v
operator: publishable
        |
        v
approved publish path
```

The implementation agent's job in v4.4 is therefore not to make the repository look more complete.

It is to turn the broad v4.3 implementation into a proven, comfortable production path for sudaさん's actual videos.
