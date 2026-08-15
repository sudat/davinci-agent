# AI動画編集パイプライン 方針・実行設計 v4.1

> 判定：条件付きGO
>
> v3の中心思想は、目的に対して合っている。とくに、Active Human Timeを最上位KPIに置き、編集判断を構造化データとして保存し、TimelineをBuild Artifactとして扱う設計は維持する。
>
> ただし、汎用的な動画制作基盤を先に完成させる進め方は採らない。対象動画を狭く定義し、編集判断、時間座標、Resolve Buildの三点を独立に検証する。通常処理は状態機械と決定論的コードで実行し、AIエージェントは意味判断と例外診断に限定する。

- 文書バージョン：4.1
- 基準日：2026-08-15
- 対象：DaVinci Resolve Studioを使ったYouTube動画制作
- 最上位KPI：撮影後のActive Human Time

## 0. V3レビューとV4.1の結論

v3は「AIがDaVinci Resolveの画面を人間のように操作する」という壊れやすい発想を避け、素材、編集判断、実装を分離している。この判断は正しい。Source Manifest、Selection Plan、Edit Plan、Compiler、Builder、Review Eventsを別Artifactにしたことで、失敗の原因を切り分け、モデルやNLEを交換しても編集知識を残せる。

一方で、v3のまま実装を始めると、編集判断の価値を検証する前に、汎用Media Store、複数ジャンル、学習基盤、広範なResolve機能対応へ工数が流れる可能性が高い。最大のリスクは技術的に作れないことではない。必要以上に広いシステムを作り、一本あたりの人間時間が減らないことである。

### 0.1 評価

| 評価軸 | 判定 | 理由 |
|---|---:|---|
| 目的との整合 | A | 自動化率ではなくActive Human TimeをKPIに置いている。Editorial Preview、Review Commands、Review Eventsが目的へ直結している。 |
| 基本アーキテクチャ | A- | Artifact-first、CompilerとBuilderの分離、再Build原則は強い。Control Planeと例外境界が不足している。 |
| 発話主導動画の実現性 | 高い | Transcriptを判断根拠にでき、カット、字幕、基本音声、Intro、Outroまでを限定すれば縦に通せる。 |
| 旅行、POVの実現性 | 中程度 | 視覚的な採否、決定的瞬間、連続性、プライバシー確認に人間の好みが強く入り、30分KPIの難度が上がる。 |
| Resolve自動化の実現性 | 条件付き | 基本配置とRenderは可能だが、タイトル配置、既存Itemのtrimやmove、Transition、Fairlightの細粒度操作などに公開API上の制約がある。 |
| 30分KPIの実現性 | 対象限定で高い | 完成尺、素材量、ジャンル、編集文法を制限すれば狙える。対象無制限では成立しない。 |
| 過剰設計リスク | 高い | 62節の多くは長期形として妥当だが、最初の価値検証より先に作る必要はない。 |

### 0.2 維持する設計

- Active Human Timeを最上位KPIとする。
- 元素材を不変とし、編集判断をArtifactへ保存する。
- Edit PlanをResolveの操作列にしない。
- 時間、配置、制約、検証をコードへ寄せる。
- Selection PlanとEdit Planを分離する。
- Buildは完成形の再構成を基本とし、壊れたTimelineの途中状態を復旧元にしない。
- ReviewはDaVinci Resolveとは分離する。MVPでは低解像度Preview MP4とAIとの自然言語対話を使い、専用Review UIは長期ターゲットとする。
- Review Eventsをappend-onlyで保存する。
- Capability Matrixを実機検証に基づいて管理する。

### 0.3 V4.1で採用する設計

- 30分KPIが適用されるSupported Episode Contractを定義する。
- OpenCode、OMO、MCPをProduction Orchestratorから外し、開発、運用、例外診断の入口へ移す。
- 通常BuildとRetryをAIエージェントではなくJob State Machineが実行する。
- LLM出力をProposalとして扱い、ValidatorとCommitterを通過したArtifactだけを正本にする。
- VFR元素材のsource frameを正準座標にしない。MVPではCFRのEdit Sourceを生成し、Original PTSとのConform Mapを残す。
- 映像Frame、音声Sample、Timeline Frameを別座標として保持する。
- Edit PlanとResolve固有形式の間にNLE非依存のTimeline IRを置く。
- Resolve Buildの前にPreview Rendererを置き、編集判断を先に人間が確認する。
- 短期のEditorial Reviewは、低解像度Preview MP4を通常の動画プレイヤーで確認し、AIへ自然言語で修正指示を送る。AIは指示をReview Commandへ構造化してArtifactへ保存する。
- 専用Review UIは長期ターゲットとし、実測された人間時間のボトルネックを解消できる場合にだけ実装する。
- Resolveの機能ごとにdirect、preset、external、manual、unsupportedの退避経路を定義する。
- Manual Finalization時はTimelineが正本ではない、という原則に例外を設け、Manual Finalization Packageを正本として保存する。
- Profileの自動更新を禁止し、Review履歴からProfile Change Proposalを作って人間が承認する。

## 1. 目的

カメラで撮影した複数の動画素材を入力として、DaVinci Resolve Studioを使ったYouTube向け動画制作を、撮影後の人間作業を最小化した状態で完了できる仕組みを作る。

目指すものは、AIがDaVinci Resolveを人間の代わりに何百回も操作する仕組みではない。素材を機械可読な状態へ変換し、編集判断を構造化データとして保存し、その判断からPreview、Timeline、完成動画を再構築できる制作システムである。

人間が担当する作業は原則として以下へ限定する。

- 撮影する。
- チャンネルと動画の方向性を決める。
- 低confidenceの候補だけを必要に応じて選ぶ。
- 低解像度のEditorial Preview MP4で構成を確認し、MVPではAIとの自然言語対話で修正要求を入力する。
- QC Engineが抽出した要確認箇所を確認する。
- 最終的な公開可否を判断する。

評価指標は自動化率ではない。素材投入後に人間が動画制作へ使うActive Human Timeを減らすことを最終目的とする。

初期の長期目標は、Supported Episode Contractを満たす動画について、候補確認、修正指示、最終確認を含むActive Human Timeの中央値を30分以内へ収めることである。平均値だけでは一部の失敗を隠せるため、P90も同時に計測する。

## 2. 非目標

V4.1では、次の状態を初期目標にしない。

- あらゆるYouTubeジャンルへ同時に対応する。
- AIへ完全なクリエイティブ責任を渡す。
- DaVinci Resolveに存在する全機能を自動化する。
- AIが同じ入力から毎回同じ編集判断を再生成することを保証する。
- Timeline上で行った任意の手作業を自動的にEdit Planへ逆変換する。
- Native Multicam、Variable Speed Retime、複雑なMask、手作業前提のFusion CompositeをMVPへ含める。
- 最終的な公開判断、権利判断、プライバシー判断をAIへ移す。
- 専用Review UIをPhase 0またはPhase 1の必須成果物にする。

AI判断の再現性とBuildの再現性は分ける。採用済みのEdit Planを保存することで編集判断をReplayし、同じEdit Planと固定されたToolchainから同じ構造のTimelineを再Buildする。LLMへ再問い合わせして同じPlanを作らせることは再現性の要件に含めない。

## 3. Supported Episode Contract

30分KPIは、対象を定義しないまま掲げると測定不能になる。完成動画が30分を超えれば、通常速度の最終確認だけでKPIを使い切る。素材が数時間あれば候補確認だけでも増える。そこで、KPIを適用するEpisodeの契約を持つ。

### 3.1 MVPの適格条件

初期Reference Caseは、次の条件を満たす発話主導動画とする。

| 項目 | MVP既定値 |
|---|---|
| 言語 | 日本語 |
| 完成尺 | 6分から15分 |
| 総素材尺 | 90分以内を既定上限とする |
| 主体 | 単一話者、または明確に分離できる少人数 |
| 撮影形態 | 単一の主映像、連続する複数take、任意のB-roll |
| 音声 | 48kHzを推奨。DialogueとAmbientを判別できること |
| 編集 | Hard Cut中心。一定速再生。複雑なMulticamなし |
| Presentation | 字幕、少数テロップ、Intro、Outro、基本BGM |
| Color | Project TemplateとPresetで処理できる範囲 |
| Privacy | Critical候補を人間が確認できること |

既定値はChannel Profileで変更できる。ただし、契約を広げた場合は同じ30分KPIを自動的に引き継がない。完成尺、素材尺、複雑性ごとに別の基準値を持つ。

### 3.2 適格性の判定

Ingest後にEpisode Eligibilityを機械判定する。

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

契約外のEpisodeは失敗として扱わず、Assisted Modeへ送る。Assisted Modeでは人間時間を同じように記録するが、30分KPIの分母からは分ける。

ただし、適格Episodeの割合もCoverage Ratioとして測る。狭い契約だけを残して30分を達成しても、実際に作りたい動画の多くが契約外なら目的は達成していない。

## 4. 設計原則

### 4.1 Artifact-first

各工程は、前工程の暗黙状態ではなく、Versioned Artifactを入力として実行する。チャット履歴、エージェントの記憶、開いたままのTimelineを正本にしない。

### 4.2 ProposalとCommitを分離する

LLMはSelection Plan、Edit Plan、QC Issue、修正案をProposalとして出力する。Schema Validation、Semantic Validation、Lock Validation、Capability Validationを通過したProposalだけを新VersionとしてCommitする。

### 4.3 決定論的処理をコードへ寄せる

時刻変換、尺計算、Constraint solving、Track割当、配置、字幕分割、Build、再Build、検証、Retryはコードで実行する。LLMは意味判断、候補評価、構成、例外原因の仮説生成を担当する。

### 4.4 単一Writer

Resolve ProjectとJob Stateへの書き込みは一系統に限定する。Analyzerや検索は並列化してよいが、同じTimelineへ複数エージェントが並行mutationを行わない。

### 4.5 公開された安定面を優先する

Production Pathは、Resolveの公開Scripting API、公開Interchange Format、事前生成したTemplate、通常のMedia Fileを優先する。内部Project Databaseや非公開ファイル形式の直接編集はExperimental Pathへ隔離する。

### 4.6 不確実性を早く見せる

重いResolve Buildの前に低解像度のEditorial Preview MP4を作る。MVPでは通常の動画プレイヤーで確認し、AIとの自然言語対話で修正する。構成や採否が間違っていれば、PresentationとFinal Renderへ進む前に止める。

### 4.7 機能追加は実素材の失敗から行う

想定される全機能を先に作らない。Reference Episodeで人間が修正した内容を記録し、頻出するStructured CorrectionからSchema、Profile、Template、Adapterを広げる。

## 5. 全体アーキテクチャ

V4.1では、制作データを流すData Planeと、Jobの状態を管理するControl Planeを分ける。

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
    AI QC
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
Observability
+
Budget / Policy Guard
```

Control Planeは、どのArtifactを入力にどのStageを実行したか、Retryしてよいか、人間確認が必要か、どのVersionが承認済みかを管理する。

## 6. Job State Machine

長時間のLLM tool-call chainをJob Orchestratorにしない。各Stageは明示的な入力、出力、成功条件を持ち、同じ入力hashに対して再実行しても不整合を起こさないIdempotentな処理とする。

```text
CREATED
↓
INGESTED
↓
NORMALIZED
↓
ANALYZED
↓
PLAN_PROPOSED
↓
PLAN_COMMITTED
↓
PREVIEW_READY
↓
EDITORIAL_APPROVED
↓
RESOLVE_BUILT
↓
QC_PASSED
↓
FINAL_APPROVED
↓
FROZEN
```

各Stageは以下の補助状態を持てる。

```text
RUNNING
FAILED_RETRYABLE
FAILED_BLOCKING
NEEDS_HUMAN
CANCELLED
SUPERSEDED
```

Stage実行は、`stage_name + input_artifact_hashes + runner_version`からIdempotency Keyを生成する。同じKeyの成功Artifactがある場合は再利用する。

Retryはエラー種別ごとに決める。Timeoutや一時的なResolve接続断は回数を制限して再実行できる。Schema違反、Capability不足、元素材欠落は自動Retryせず、入力修正または人間確認へ送る。

## 7. Source of TruthとArtifact分類

### 7.1 Authoritative Artifact

長期的な判断と再現に必要な正本である。

- Camera Original
- Source Manifest
- Conform Map
- Job Manifest
- Resolved Configuration Snapshot
- Asset Registry Snapshot
- Committed Selection Plan
- Committed Edit Plan
- Review Events
- Profile Change Approval
- Final Timeline IR
- Resolve Build Report
- Final QC Report
- Final Render

Timeline IRはCompilerから再生成できるが、容量が小さく、当時のCompiler挙動を固定する証跡になるため、最終承認版を長期保存する。

### 7.2 Rebuildable Artifact

再生成可能だが、処理速度や監査のため一定期間保持する。

- Proxy
- Edit Mezzanineの低解像度版
- Analysis Frame
- Contact Sheet
- Embedding
- Query Index
- Editorial Preview
- Resolve Package
- Intermediate Timeline
- Temporary Audio Analysis

### 7.3 Runtime State

Job State、Lock、Lease、Retry Count、進捗、Cache Indexなどである。SQLiteやDuckDBへ保存できるが、編集判断の正本にはしない。

### 7.4 Manual Finalizationの例外

通常ModeではTimelineはBuild Artifactであり、正本ではない。しかし、Schemaが表現できない手作業を行い、AutomationをFreezeした場合は例外となる。

```yaml
automation_frozen: true
manual_finalization: true
```

この場合は、次の集合をManual Finalization Packageとして正本にする。

- 手作業後のDRTまたはDRP Export
- Final Render
- Timeline Fingerprint
- Manual Change Log
- 最後に対応していたEdit Plan Version
- Freeze理由

この例外を明示しないまま「Timelineは常に正本ではない」とすると、手作業後の完成状態を再現できない。

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

Job Manifestは実行環境と採用Artifactを固定する。

```json
{
  "episode_id": "tokyo-001",
  "job_id": "job_01J...",
  "source_manifest_hash": "sha256:...",
  "conform_map_hash": "sha256:...",
  "resolved_config_hash": "sha256:...",
  "asset_registry_snapshot_hash": "sha256:...",
  "selection_plan_hash": "sha256:...",
  "edit_plan_hash": "sha256:...",
  "timeline_ir_hash": "sha256:...",
  "toolchain": {
    "os": "...",
    "hardware_profile": "...",
    "ffmpeg_version": "...",
    "asr_version": "...",
    "resolve_version": "21.x",
    "resolve_build": "...",
    "adapter_version": "git:...",
    "builder_version": "git:...",
    "mcp_commit": "git:..."
  },
  "models": {
    "editorial": {
      "provider": "openai",
      "model_id": "gpt-5.6-sol",
      "snapshot": "...",
      "prompt_bundle_hash": "sha256:...",
      "output_schema_version": "4.0.0",
      "request_id": "..."
    },
    "diagnostic": {
      "provider": "zai",
      "model_id": "glm-5.2",
      "snapshot": "..."
    }
  }
}
```

モデル名やMCPのSemantic Versionだけでは挙動を固定できない。可能な限りModel Snapshot、Prompt Bundle Hash、Schema Version、Git Commit、TemplateとAssetのContent Hashを残す。

## 9. Ingestと時間座標

### 9.1 元素材とEdit Sourceを分ける

Camera Originalは一切変更しない。編集に使うSourceは、素材特性に応じて決める。

- CFRで座標整合が実機確認できた素材：OriginalをEdit Sourceとして使用できる。
- VFR、壊れたTimestamp、複雑なRotation、Codec相性がある素材：CFRのEdit Mezzanineを生成し、これをEdit Sourceとする。
- Proxy：Edit Sourceを置き換えず、表示と解析の派生Assetとして扱う。

MVPでは、VFR Originalへ直接Frame指定して最終Buildする経路を必須にしない。CFR Mezzanineを最終Render Sourceとして使う。OriginalへのOnline Relinkは、Conform精度がGolden Fixtureで確認できた後に追加する。

### 9.2 三つの正準座標

映像、音声、Timelineを一つのframe座標へ押し込まない。

- Original Timestamp：PTSとtime_baseで表す。
- Edit Video Position：Edit Source上の整数Frameと正確なFrame Rateで表す。
- Edit Audio Position：Edit Source上の整数SampleとSample Rateで表す。
- Record Position：Timeline上の整数Frameで表す。

すべての区間は半開区間 `[start, end)` とする。

```json
{
  "video_span": {
    "edit_source_id": "src_cam_a_001_cfr",
    "start_frame": 1234,
    "end_frame": 1492,
    "rate": {"num": 30000, "den": 1001}
  },
  "audio_span": {
    "edit_source_id": "src_cam_a_001_cfr",
    "start_sample": 1976384,
    "end_sample": 2389275,
    "sample_rate": 48000
  }
}
```

SubtitleやTranscript Tokenは原則としてAudio Sampleへ結び付ける。映像へ同期して表示するときにCompilerがTimeline Frameへ変換する。

### 9.3 Conform Map

OriginalとEdit Sourceの対応を記録する。

```json
{
  "original_source_id": "cam_a_001",
  "edit_source_id": "cam_a_001_cfr",
  "video_map": {
    "type": "pts_to_frame_table",
    "artifact": "conform/video-map.parquet"
  },
  "audio_map": {
    "type": "sample_affine",
    "offset_samples": 0,
    "ratio": {"num": 1, "den": 1}
  },
  "normalization": {
    "target_rate": {"num": 30000, "den": 1001},
    "frame_policy": "cfr_duplicate_or_drop",
    "audio_sample_rate": 48000
  }
}
```

Analyzerの秒、ffprobeのPTS、ResolveのSource Frameが一致するとは仮定しない。すべての変換はConform Layerへ集約する。

### 9.4 Source Manifestの追加項目

V4.1では、v3の項目に次を追加する。

- `start_time`
- `stream_index`
- `pix_fmt`
- `color_primaries`
- `color_transfer`
- `color_space`
- HDR Metadata
- Audio Channel Layout
- Encoder DelayまたはStart Offset
- Timestamp Monotonicity
- VFR判定根拠
- Edit Sourceの生成RecipeとHash

Color MetadataやHDRを無視すると、編集判断が正しくても最終映像が崩れる。Ingest時に契約外として止めるか、明示的なColor Management Recipeを選ぶ。

## 10. Queryable Media StoreとAnalyzer

Media Storeの一次保存先はSQLiteまたはDuckDBとする。巨大なJSONをLLMへ一括投入しない。ファイルArtifactが正本で、Databaseは検索IndexとRuntime Stateを担う。

### 10.1 MVPで実装するAnalyzer

発話主導Reference Caseでは、次だけを先に作る。

- ffprobeによるStream Metadata
- Audio Decode Validation
- TranscriptとWord Timestamp
- SilenceとPause
- Filler候補
- False Start候補
- Audio Peak、Loudness、Clipping候補
- Scene Changeの最低限検出
- Blur、Black、Exposureの最低限検出
- Contact Sheet

Motion、Shot Clustering、Visual Similarity、Face Tracking、Plate Trackingは旅行、POV Phaseで追加する。解析項目を増やすこと自体は価値ではない。採否の精度または人間時間を改善するAnalyzerだけを残す。

### 10.2 Cache Key

Analyzer出力は以下をCache Keyにする。

```text
edit_source_content_hash
+
analyzer_name
+
analyzer_version
+
parameter_hash
```

Modelを使うAnalyzerは、Model SnapshotとPrompt Hashも含める。

### 10.3 Query Interface

Editorial Directorへは読み取り専用の検索Interfaceを公開する。

```text
get_episode_summary()
get_source_summary(source_id)
get_transcript(source_id, audio_span)
find_transcript(query, filters)
find_silence_ranges(filters)
find_low_quality_ranges(filters)
get_contact_sheet(source_id, range)
get_candidate_frames(source_span, density)
get_range_statistics(source_span)
get_review_examples(context)
```

ツールは、返したResultにSource ID、Span、Analyzer Version、Confidenceを付ける。Editorial DirectorはEvidenceのない映像内容をPlanへ書かない。

## 11. モデルの役割と境界

Architecture上の役割名と、現在採用するモデル名を分ける。モデルは交換可能な実装であり、Roleが契約である。

### 11.1 Editorial Director

現在の候補はGPT-5.6 Solとする。担当は以下である。

- 素材の検索方針を決める。
- Transcript、Contact Sheet、必要なFrameを確認する。
- 候補Shotと発話区間を評価する。
- Story Blockと構成を考える。
- Selection Plan Proposalを作る。
- Edit Plan Proposalを作る。
- 少数のテロップ内容とBGMの意味的選択を行う。
- Review Eventを踏まえた修正Proposalを作る。
- AI QC Issueを生成する。

Editorial DirectorはRecord Frame、Track Index、fps変換、Subtitle Cueの最終分割を決めない。Resolveを直接操作しない。

### 11.2 Visual Judge

旅行、POVなど、候補区間の視覚的比較が必要なときだけ呼ぶ。全素材を高密度で見る役割にはしない。

- 動作開始と終了
- 決定的瞬間
- 構図差
- 被写体方向
- Shot間の連続性
- 類似Shotの代表選択

### 11.3 Diagnostic Assistant

現在の候補はGLM-5.2とする。担当は、標準処理が失敗した後の診断である。

- Build Reportと構造化Logの確認
- Capability Matrixの参照
- 既知障害との照合
- Retry可能性の判定案
- Builder修正案またはInput修正案の生成

通常のBuilder実行、状態確認、Retry、Render開始はJob Runnerが行う。Diagnostic Assistantが毎回MCPを何十回も呼ばなければ完成しない構造にはしない。

### 11.4 OpenCode、OMO、MCPの位置づけ

OpenCodeとOMOは、開発、対話的な検証、運用コンソール、例外診断に使う。Production Jobの正本状態とStage遷移はJob State Machineが管理する。

MCPはResolve機能へアクセスする便利なInterfaceだが、Production Builderそのものではない。BuilderはCLIまたはLocal Serviceとして公式Scripting APIを決定論的に呼び、MCPは同じ能力をエージェントへ公開する補助面とする。

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
  "duration": {
    "min_frames": 60,
    "ideal_frames": 120,
    "max_frames": 180
  },
  "handles": {
    "head_frames": 15,
    "tail_frames": 15
  },
  "redundancy_group": "sensoji_gate",
  "evidence": [
    {"type": "contact_sheet", "artifact_id": "art_..."},
    {"type": "transcript", "audio_start_sample": 12000, "audio_end_sample": 92000}
  ],
  "reasons": [
    "場所が明確に分かる",
    "前後の移動Shotとつながる"
  ]
}
```

Candidate IDはLLMに採番させない。`source_id + normalized_span + intent + analyzer_version`などからControllerが決定的に生成する。LLMがSpanを微修正した場合はCandidate Relationとして親子関係を残す。

発話カットには前後Handleを持たせる。Transcript上の語境界だけで切ると、子音欠け、呼吸の消失、Audio Click、窮屈なJump Cutが起きるためである。最終TrimはPlannerとAudio RuleがHandle内で決める。

## 13. Duration and Constraint Planner

完成尺をEditorial Directorの一発生成へ依存させない。PlannerはStory Blockの尺Budgetを配り、その中でCandidateを選ぶ。

### 13.1 Hard Constraint

- Must Include
- Must Exclude
- Story Order
- Dependency
- Approved Lock
- Source範囲の有効性
- Capability Matrix上の実装可能性
- Episode Contract

### 13.2 Soft Constraint

- Target Duration
- Redundancyの削減
- Pacing
- Shot Diversity
- Channel固有の好み
- 過去Reviewとの整合
- Confidence

PlannerはHard Constraintを黙って破らない。解が存在しない場合はInfeasibility Reportを返し、Editorial Directorへ構成変更を依頼する。

```json
{
  "status": "infeasible",
  "conflicts": [
    "must_include_total_frames exceeds story_block.max_frames",
    "locked_order conflicts with chronological_order"
  ],
  "suggestions": [
    "increase target duration",
    "unlock decision 9fb0ac",
    "split story block"
  ]
}
```

同じScoreの解が複数ある場合は、Candidate IDなどによる決定的Tie-breakを使う。PlanのDiffを安定させるためである。

## 14. Edit Plan

Edit Planは採用された編集判断を表す。Resolve固有の命令列を書かない。

```json
{
  "decision_id": "dec_01J...",
  "type": "video",
  "role": "primary_video",
  "source_span": {
    "edit_source_id": "cam_a_001_cfr",
    "start_frame": 2800,
    "end_frame": 3190
  },
  "intent": "arrival_establishing",
  "story_block": "arrival",
  "link_group_id": "av_004",
  "locks": [],
  "provenance": {
    "candidate_id": "cand_sha256_...",
    "proposal_id": "prop_01J..."
  }
}
```

### 14.1 VideoとAudioの分離

Video ItemとAudio Itemは別に持ち、`link_group_id`で関連付ける。通常は一緒に動かし、J Cut、L Cut、B-roll、Ambient Sound Bridgeを作るときだけSource SpanとPlacementを分ける。

### 14.2 永続Decision ID

Decision IDはControllerが採番する。Planの再生成時は、Source、Intent、Story Block、近接Span、親Candidateなどを使って既存Decisionとの対応をReconcileする。LLMへ既存UUIDの維持を期待しない。

### 14.3 Lock

LockはField単位で持つ。

```text
selection
source_span
order
text
presentation
audio
```

Lockには、付与者、付与時刻、根拠、Base Plan Versionを保存する。新ProposalがLockと競合した場合はCommitせず、ConflictとしてReview Interfaceへ出す。

## 15. Timeline IR

Edit PlanとResolve固有形式の間にTimeline IRを置く。Timeline IRは、NLE非依存でありながら、PreviewとFinal Buildに必要なRecord Placementを持つ。

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

Resolve固有の`track_index`、Media Type番号、API Method、Template適用手順はResolve Packageで初めて追加する。これにより、Preview Rendererと将来の別NLE Adapterが同じTimeline IRを使える。

Timeline Compilerは、Duration変換、Anchor解決、Gap検出、Audio SampleからRecord Frameへの変換、Subtitle Cue生成、Logical Trackの整合を一か所で行う。

## 16. Editorial Preview

Resolve Buildの前に、Timeline IRから低解像度のPreview MP4を生成する。FFmpegなどを使い、Hard Cut、基本Audio、仮字幕、簡易Overlay、BGMを再現する。

短期ターゲットでは、Previewは専用UIの中で再生しない。通常の動画プレイヤーまたはブラウザでMP4を再生し、修正はAIとの自然言語対話で伝える。

例えば、人間は次のように指示する。

```text
3:12〜3:35は削除。
5:40は切るのが早いので、一文前から残す。
7:20の字幕を「OpenClaw」に修正。
それ以外はOK。
```

AIはこの文章を、`remove_segment`、`adjust_source_span`、`correct_subtitle`、`approve_remaining`などの構造化Review Commandへ変換する。曖昧な指示だけを人間へ確認し、解釈結果は会話履歴ではなくReview Eventとして保存する。

Previewの目的は最終画質ではない。次の判断を先に終えることである。

- Shotの採否
- 発話の削除
- 構成順序
- 尺とPacing
- Must Includeの充足
- Subtitle内容の大きな誤り
- BGMの方向性

```text
Edit Plan
↓
Timeline IR
↓
Preview Renderer
↓
Low-resolution Editorial Preview MP4
↓
Human Review in Standard Player
↓
Natural-language Instruction to AI
↓
Structured Review Commands / Events
↓
Human Approval
↓
Resolve Finalization
```

Previewを通さずResolve Buildまで進むと、編集判断の問題とResolve自動化の問題が混ざる。V4.1では`EDITORIAL_APPROVED`をFinal Buildの必須Gateとする。

## 17. Resolve AdapterとBuilder

Resolve AdapterはTimeline IRを、Resolveで実行できるResolve Packageへ変換する。BuilderはResolve Host上でPackageを適用する決定論的なCLIまたはLocal Serviceとする。

### 17.1 Build手順

```text
Timeline IR
↓
Capability Validation
↓
Resolve Package
↓
Staging Project / Staging Timeline
↓
Base Media Placement
↓
Subtitle / Overlay / Audio / Preset適用
↓
Item-level Conformance Check
↓
Preview Render
↓
Final Render
```

既存Timelineを人間のようにBladeし、少しずつ変形し続けることを基本にしない。公開APIでは既存Timeline Itemのtrim、move、bladeに制約があるため、構造変更はClean Rebuildのほうが安全である。

### 17.2 Adapter Strategy

機能ごとに次の優先順位を持つ。

```text
direct
↓
interchange
↓
template
↓
external
↓
manual
↓
unsupported
```

- `direct`：公開Scripting APIから直接実行する。
- `interchange`：FCPXML、OTIO、EDLなどの公開FormatをImportする。
- `template`：事前に作ったProject、Timeline、Fusion、Fairlight、Grade Presetを再利用する。
- `external`：字幕、Overlay、Audio処理などを外部で生成し、Media Assetとして配置する。
- `manual`：人間のUI操作が必要で、EpisodeをFreezeする可能性がある。
- `unsupported`：自動化対象外とする。

内部DRP、DRT、Project Databaseを直接書き換えるAdvanced Modeは、この優先順位とは別のExperimental Laneへ置く。Productionの必須経路にしない。

### 17.3 Build Mode

- `clean`：空のStaging Timelineへ全体を再構成する。構造変更の既定値。
- `patch`：Capability Matrixで検証済みの低リスク変更だけを適用する。字幕文字修正などから始める。
- `frozen`：Manual Finalization後。自動Buildしない。

Patchに失敗した場合は、元Timelineの修復を続けずClean Buildへ戻る。

## 18. Capability Matrix

DaVinci Resolve Studioに機能が存在することと、公開APIから安全に自動化できることは別である。Capability Matrixは、実機FixtureとResolve Buildごとに更新する。

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

### 18.1 初期に検証する機能

| 機能 | MVP方針 | 検証事項 |
|---|---|---|
| Base Cut | direct | Source In/Out、Record Frame、Track、Mixed FPS、Gapなし |
| Subtitle | direct、interchange、externalを比較 | Text、Timing、Style、再Build、文字化け |
| Fusion Title | templateまたはexternal | 任意Trackへの配置、長さ、Text差し替え |
| Intro、Outro | media-backed asset | Track配置、音声Link、尺一致 |
| Voice Isolation | track preset | Track単位の適用可否、再現性、Artifact化 |
| Dialogue Gain | externalまたはpreset | Clip単位自動化ができない場合の退避 |
| Fairlight | saved preset | DialogueとAmbientの分離、Preset適用、Readback |
| Transition | Hard Cutを既定 | 自動追加をMVP外とする |
| Stabilization | partial | 実機API、処理完了検出、再現性 |
| Reframe | partial | CropとPositionの直接制御、Template代替 |
| Retime | unsupported in MVP | 一定速のみ。誤ったSource参照を検出するFixture |
| Native Multicam | unsupported in MVP | 外部Sync Mapと通常Trackへの展開を将来検討 |
| Grade | template、DRX、LUT | Per-shot AI GradeをMVP外とする |
| Proxy | external generation | ResolveへLinkできるか、Relink精度 |
| Render | direct | Preset、Path、Codec、Job状態、Failure検出 |

Capability Matrixに`api_available: true`だけを書かない。要求した結果がTimeline上で得られ、ReadbackとRenderで確認できたときに`live_verified: true`とする。

## 19. 機能別方針

### 19.1 構成編集

通常接続はHard Cutを基本とする。発話主導では、TranscriptとAudioを中心に、失敗take、言い直し、重複、Filler、長いPause、本題外の区間、説明順序を判断する。

自動削除は、語の前後Handle、最小Shot長、Audio Crossfade、映像Jumpの許容Ruleを通す。Fillerを機械的に全削除しない。話者の自然さ、意味、Lip Syncを壊す場合は残す。

旅行、POVでは無音を削除条件にしない。場所変化、動作、決定的瞬間、類似Shot、手ブレ、Focus、Exposure、環境音を評価する。ただし、最初は完全自動Selectionではなく、AIが絞った候補を人間が選ぶCandidate-assisted Modeから始める。

### 19.2 Subtitle

Source TranscriptはAnalyzer ArtifactとしてAudio Sampleに結び付ける。Subtitle CueはEdit Plan確定後にCompilerが生成する。

```text
Source Transcript Tokens
↓
採用Dialogue Spanへ限定
↓
Edit Boundaryで分割
↓
日本語の句読点と意味境界で整形
↓
最低表示時間、行数、文字量を検証
↓
Timeline Cueへ変換
```

Editorial Directorが全Cueを一件ずつ生成しない。固有名詞、誤認識、表記統一、要約字幕など意味判断が必要な箇所だけModelへ渡す。

管理対象は以下とする。

- Text
- Source Audio Span
- Record Span
- Confidence
- Minimum Display Duration
- Line Count
- Characters per Line
- Safe Area
- Cue Overlap
- Style ID
- Correction Provenance

### 19.3 テロップとOverlay

章タイトル、人物名、肩書き、場所、固有名詞、数字、重要Keyword、補足、注意事項を対象とする。

Resolve Native Titleを任意Trackへ安全に置けない場合に備え、次の二系統を用意する。

- 少数の高表現Title：事前作成したTemplate Timelineまたは検証済みFusion Asset。
- 規則的なOverlay：透明背景のMedia Assetとして外部Renderし、通常Clipとして配置。

Overlay Textが事実主張を含む場合は、Transcript、Episode Config、確認済みMetadataのいずれかへEvidenceを持つ。AIが数値や固有名詞を補完しない。

### 19.4 Audio

Audio Itemは最低限次へ分類する。

```text
dialogue
ambient
music
se
mixed
silence
```

Voice IsolationがTrack単位でしか安定適用できない場合、DialogueとAmbientを同一Trackへ混在させない。Source解析時に役割を分け、Timeline上でも別Logical Trackへ置く。

Clip単位のGain、Pan、EQ、Automationを公開APIで安全に設定できない機能は、次の順で処理する。

```text
外部Audio Derivative生成
↓
Fairlight Track Preset
↓
事前設定済みProject Template
↓
Manual Finalization
```

Dialogue処理をAmbientへ一律適用しない。Environment Soundを残すこと自体が編集意図になる。

### 19.5 BGMとSE

BGMとSEは承認済みAsset Registryから選ぶ。AIがInternetから自由に取得しない。

```json
{
  "asset_id": "bgm_travel_003",
  "placement": {
    "anchor": {
      "type": "story_block_start",
      "story_block": "arrival"
    },
    "offset_frames": 0
  },
  "gain_db": -16,
  "fade_in_frames": 45,
  "fade_out_frames": 60,
  "ducking_profile": "dialogue-standard-v2"
}
```

GainやDuckingをResolveで細かく自動化できない場合は、PreviewとFinal用のMixed Audio Derivativeを外部生成するか、Track Presetで近似する。Channel ProfileにはLoudness TargetとTrue Peak Ceilingを制作基準として持つ。

### 19.6 Color、Crop、Stabilization、Reframe

MVPではProject Color ManagementとCamera別Presetを先に固定し、AIによるShotごとのGradeを行わない。Color異常はQC Issueとして検出し、人間確認またはPreset選択へ送る。

Crop、Position、Zoomは直接操作できる範囲で使う。Stabilization、Reframe、MaskはCapability MatrixのFixtureを通った機能だけを有効にする。処理完了を待てない、結果をReadbackできない、Version差で挙動が変わる場合はAssisted Modeへ落とす。

### 19.7 RetimeとMulticam

一定速以外のRetime、Reverse、Speed Ramp、Native MulticamはMVP外とする。Interchangeで表現できても、Source Clipの誤参照やSilent Failureが起きる可能性があるため、Golden FixtureでFrame Identityまで確認できるまでProductionへ入れない。

## 20. Build VerificationとTimeline Versioning

Builderは「成功しました」だけを返さない。要求と実結果をItem単位で比較する。

```json
{
  "timeline_ir_hash": "sha256:...",
  "resolve_package_hash": "sha256:...",
  "timeline": "episode-v7-staging",
  "items_requested": 84,
  "items_built": 84,
  "item_checks": [
    {
      "decision_id": "dec_01J...",
      "media_hash_match": true,
      "source_start_delta_frames": 0,
      "source_end_delta_frames": 0,
      "record_start_delta_frames": 0,
      "record_end_delta_frames": 0,
      "track_match": true,
      "link_group_match": true
    }
  ],
  "timeline_duration_delta_frames": 0,
  "warnings": [],
  "failures": []
}
```

Timelineの総尺一致だけでは、別素材の誤配置、Track違い、Source In/Outのずれを検出できない。各ItemのMedia Identity、Source Span、Record Span、Track、Link Groupを確認する。

Edit Planが変わった場合は新しいStaging TimelineをBuildする。

```text
edit-plan-v5
↓
timeline-v5-staging
↓
verification
↓
timeline-v5-review
```

人間がResolve上で変更した可能性があるTimelineは、Build前にFingerprintを比較する。Driftがある場合は上書きせず、Manual Overrideへ構造化するか、別Branchを作るか、Freezeする。

## 21. QC

QCはTimeline IR、Editorial Preview、Final Renderの三段階で行う。

### 21.1 IR QC

- Gap
- Overlap
- Track Rule違反
- Anchor未解決
- Source Span範囲外
- Lock違反
- Must Include欠落
- Episode Contract違反
- Unsupported Capabilityの使用

### 21.2 Preview QC

- 不自然に短いShot
- 同一映像の過剰反復
- Subtitle Timing
- Audio欠落
- DialogueとAmbientの役割衝突
- 構成上の明らかな空白

### 21.3 Final Deterministic QC

映像。

- Decode Failure
- Render Failure
- Expected Durationとの差
- Black Frame
- Freeze候補
- 異常輝度
- Frame Drop候補
- Resolution、Frame Rate、Color Metadata

音声。

- Stream欠落
- Clipping
- True Peak超過
- Integrated Loudness逸脱
- 異常な無音
- Channel Layout異常
- Audio Durationとの差

字幕。

- Cue欠落
- 表示時間不足
- Overlap
- Safe Area逸脱
- 文字化け
- Style不一致

### 21.4 AI QC

AI QCは「問題がないことを保証する」役割ではない。人間が見るべき候補をDecision IDまたはRecord Span付きで抽出する。

- Subtitle誤認識候補
- 不自然なCut候補
- 画面とOverlayの競合
- Colorの違和感
- 構成上の違和感
- 重複映像
- 不自然なPause
- BGMと場面の不一致

AI QCはPlanを直接修正しない。IssueとFix Proposalを作り、人間またはPolicyが採用したものだけをCommitする。

### 21.5 Privacy QC

顔、ナンバープレート、住所、個人情報を含む画面などを候補として抽出する。CriticalなPrivacy IssueはReview Budget外とし、解消または人間の明示承認がない限りFinal Approvalへ進めない。

AI検出結果だけで公開可否を決めない。撮影場所、文脈、権利、チャンネル方針を人間が判断する。

## 22. Review Interface

Reviewは二つのGateを持つ。ただし、Review Interfaceと専用Review UIは同じものではない。V4.1ではReviewという機能を先に成立させ、専用UIは人間時間のボトルネックが確認されてから作る。

### 22.1 短期ターゲットのEditorial Review

Resolve Build前の低解像度Preview MP4を、通常の動画プレイヤーまたはブラウザで確認する。人間は気になった時刻と修正内容をAIへ自然言語で伝える。

```text
人間：3:12〜3:35はいらない。
人間：5:40は切るのが早い。一文前から残して。
人間：7:20の字幕は「OpenClaw」。
人間：それ以外はOK。
```

AIは指示を現在のEdit Planと照合し、対象Decisionを特定してReview Commandへ変換する。複数の解釈が成立する場合だけ質問する。承認も「これでOK」「この版で進めて」のような自然言語を`approve_editorial_plan`へ変換する。

MVPでは、専用のRemoveボタン、Trim Earlierボタン、字幕編集画面、Issue Seek UIを作らない。これらは操作頻度とActive Human Timeを実測してから追加する。

### 22.2 短期ターゲットのFinal Review

Resolve RenderとQC結果を確認する。Final Renderは通常の動画プレイヤーで再生し、QC IssueはAIがDecision IDまたは時刻付きで提示する。人間は自然言語でAccept、Reject、修正、公開承認、Manual Finalizationを指示する。

短期ターゲットで人間が確認する対象は次のとおりである。

- Final Render
- Critical Issue
- Deterministic QC結果
- AI QC候補
- Privacy確認候補
- 修正後の差分

DaVinci Resolveを通常の人間Review画面にはしない。DaVinciを直接開くのはManual Finalizationなどの例外時に限定する。

### 22.3 長期ターゲットの専用Review UI

自然言語Reviewを複数本運用し、実際に繰り返し発生する操作が人間時間のボトルネックになった場合にだけ、ローカルWeb UIなどの専用Review UIを追加する。

候補機能は次のとおりである。

- Preview再生
- IssueクリックによるSeek
- Keep
- Remove
- Trim Earlier
- Trim Later
- Move Before
- Move After
- Replace Candidate
- Subtitle Text修正
- BGM候補変更
- Before、After比較
- Approve Editorial Plan
- Approve Publication

専用UIの価値は操作を構造化することではない。自然言語からReview Commandへ変換する仕組みだけでも構造化はできる。専用UIは、時刻入力、Seek、反復操作、差分確認などの人間時間を実測で短縮できる場合に導入する。

### 22.4 Active Human Timeの計測

MVPでは専用UIを持たないため、Player上のSeekや再生操作を完全には計測できない。Review Sessionの開始、Preview生成時刻、AIへの修正指示、Approval時刻をJob Logへ残し、必要に応じて人間の自己申告を併用してActive Human Timeを概算する。

専用Review UIを導入した後は、再生、Seek、Command、入力、Approvalまで計測し、一定時間操作がない区間をIdleとして除外する。

```text
Candidate Review Time
+
Editorial Review Time
+
Correction Instruction Time
+
QC Review Time
+
Final Review Time
=
Active Human Time
```

## 23. Review CommandsとReview Events

Review入力はDomain Commandとして受け取り、Validatorが新しいPlan Versionへ反映する。MVPではAIが自然言語の修正指示をDomain Commandへ変換し、長期の専用Review UIではボタンや編集操作を同じCommandへ変換する。どちらの場合もEvent Logへ直接任意JSONを書かせない。

```json
{
  "event_id": "evt_01J...",
  "episode_id": "tokyo-001",
  "actor": "human:suda",
  "base_edit_plan_version": 7,
  "decision_id": "dec_01J...",
  "command": "adjust_source_span",
  "before": {
    "start_frame": 812,
    "end_frame": 1040
  },
  "after": {
    "start_frame": 760,
    "end_frame": 1110
  },
  "reason_category": "needs_more_breathing_room",
  "reason_text": "景色をもう少し長く見せたい",
  "idempotency_key": "...",
  "created_at": "2026-08-15T14:00:00+09:00"
}
```

`base_edit_plan_version`が現在Versionと一致しない場合はConflictとして止める。Review Event ReducerがEventを順に適用し、新しいEdit Planを生成する。

Review Eventは次の三種類へ分ける。

- Episode Correction：この動画だけへ適用する。
- Preference Evidence：繰り返し傾向の証拠として蓄積する。
- Profile Change Approval：人間が上位Profileへの反映を承認した記録。

## 24. Review LearningとProfile Governance

Review Historyは、現在の判断に近い例だけを検索してEditorial Directorへ渡す。

```text
同じGenre
+
同じChannel
+
同じIntent
+
同じStory Block
+
近いSource特性
```

Review Eventを見つけた時点でGenre ProfileやChannel Profileを書き換えない。同種の修正が一定数続き、再現可能なRuleへ落とせる場合にProfile Change Proposalを作る。

```yaml
proposal:
  target: channel_profile
  path: editorial.establishing.ideal_duration_frames
  current: 90
  proposed: 135
  evidence_count: 8
  supporting_events:
    - evt_...
  expected_effect: establishing shotの延長修正を減らす
  risk: 動画全体の尺増加
  status: pending_human_approval
```

承認後も、少数のHoldout Episodeで効果を確認する。過去の一時的な好みへ過適合しないためである。

## 25. Genre、Channel、Episodeの設定

設定は次の優先順位で解決する。

```text
System Defaults
↓
Genre Profile
↓
Channel Profile
↓
Episode Config
↓
Approved Manual Override
```

Job開始時にResolved Configuration Snapshotを作り、以後のProfile変更から切り離す。

### 25.1 Genre Profile

```text
analysis
editorial
presentation_defaults
quality_rubric
```

Genre Profileは編集文法を持つが、固定秒数やThresholdを無制限に増やさない。Ruleが増えすぎた場合は、実際に人間時間を減らしたかを確認し、効果がないRuleを削除する。

### 25.2 Channel Profile

Channel ProfileはBrandと制作基準を持つ。

- Subtitle Style
- Title Template
- Intro、Outro
- Color Management
- Audio Loudness Target
- Allowed Music Collection
- Editorial Rubric
- Review Budget
- Supported Episode Contract

### 25.3 Episode Config

Episode固有の条件だけを持つ。

```yaml
episode_id: tokyo-2026-08-15
genre: travel
channel: tokyo-walk
contract: travel-assisted-v1

timeline:
  frame_rate: {num: 30000, den: 1001}
  width: 3840
  height: 2160

target_duration:
  preferred_sec: 720
  min_sec: 660
  max_sec: 780

special_notes:
  - 浅草寺の本堂は必ず含める
  - 雷門を冒頭候補にする
  - 仲見世の環境音を残す

processing_policy:
  mode: cloud_allowed
  upload_original_video: false
  upload_audio: true
  upload_sampled_frames: true
```

## 26. Asset Registry

BGM、SE、Intro、Outro、Overlay Template、Fusion Template、Grade Preset、Fairlight Preset、Render PresetをVersioned Assetとして管理する。

```json
{
  "asset_id": "bgm_travel_003",
  "type": "music",
  "content_hash": "sha256:...",
  "path": "assets/music/travel_003.wav",
  "license": {
    "status": "approved",
    "usage": ["youtube"],
    "territories": ["worldwide"],
    "expires_at": null,
    "attribution_required": false,
    "evidence_artifact_id": "art_license_...",
    "content_id_notes": "..."
  },
  "tags": ["travel", "calm"]
}
```

PathだけではAssetを固定できない。Content Hash、License Evidence、適用範囲、期限、Attribution、Content ID履歴を保存する。

## 27. SecurityとData Policy

撮影素材、Transcript、OCR Textは信頼できるInstructionではない。画面内の文字や発話に「この指示を無視せよ」と含まれても、Agent命令として扱わない。Media由来データとSystem Instructionの境界をTool Contractで固定する。

Production環境では次を既定とする。

- Resolve MCPとBuilderはLoopbackまたは許可されたLocal Networkだけで公開する。
- Job DirectoryとAsset DirectoryをPath Allowlistにする。
- Agentへ任意Shell、任意File Write、任意Network Accessを渡さない。
- Read-only Media Query Toolと、検証済みCommand Toolを分ける。
- API Key、License情報、個人情報をLogとPromptへ含めない。
- Cloudへ送るData種別をEpisode Configで明示する。
- 外部Modelへ送信したArtifact、範囲、Provider、Request IDを監査Logへ残す。
- Dependency、Model、Adapter、TemplateをVersion Pinする。

`local_only` EpisodeではCloud Modelを使うStageを実行せず、Local AnalyzerまたはHuman Reviewへ切り替える。

## 28. StorageとRetention

### 28.1 長期保存

- Camera Original
- Edit Sourceまたはその生成Recipe
- Source Manifest
- Conform Map
- Job Manifest
- Resolved Configuration
- Asset Registry Snapshot
- Committed Selection Plan
- Committed Edit Plan
- Final Timeline IR
- Review Events
- Build Report
- Final QC Report
- Final Render
- Manual Finalization Package

### 28.2 期限付き保存

- Proxy
- Analysis Frame
- Contact Sheet
- Embedding
- Preview Render
- Resolve Package
- Intermediate Timeline
- Temporary Audio

再生成可能でも、障害調査中のJobは消さない。RetentionはJobの`FROZEN`、`FAILED`、`ACTIVE`状態と連動させる。

## 29. 計測指標

### 29.1 最上位指標

- Active Human Timeの中央値
- Active Human TimeのP90
- Supported Episode Coverage Ratio
- First-pass Editorial Acceptance Rate
- First-pass Final Acceptance Rate
- Blocking Defect Rate

30分という単一値だけでは、難しいEpisodeを契約外へ追い出したり、一部の大失敗を平均で隠したりできる。CoverageとP90を同時に見る。

### 29.2 Active Human Time

- Material Intake Time
- Candidate Review Time
- Editorial Preview Review Time
- Correction Instruction Time
- QC Review Time
- Final Review Time

### 29.3 編集品質

- Must Include見落とし数
- 不要Shot採用数
- Candidate採用率
- Source Span修正量
- 順序変更件数
- Story Block再構成件数
- Subtitle Correction Rate
- Manual Finalization件数
- Edit Plan Version数

### 29.4 Build品質

- Item Conformance Pass Rate
- Build Retry数
- Clean Build成功率
- Patch Build成功率
- Duration Delta
- Track Mismatch
- Media Identity Mismatch
- Resolve VersionごとのRegression件数

### 29.5 QC品質

Precisionだけを測らない。Flagされなかった区間からSampleを取り、False Negativeを推定する。

- True Positive
- False Positive
- False Negative推定
- Critical Issue見落とし
- Review Cost per True Positive

### 29.6 コストと時間

- Ingest Time
- Analysis Time
- Planning Time
- Preview Time
- Resolve Build Time
- Render Time
- Wall Clock Time
- LLM Cost
- Vision Cost
- Storage Usage

### 29.7 Structured Correction Coverage

```text
DaVinci Resolveを直接操作せず、
Edit Plan、Profile、Episode Config、Template、Asset Registry、Manual Overrideの変更として
表現できた修正要求の割合
```

長期目標は90％以上とする。ただし、無理に不自然なSchemaへ押し込むことは避ける。Manual Finalizationの理由が蓄積し、同じ理由が繰り返されるならSchema拡張候補にする。

## 30. 実装PhaseとGate

Phaseは機能数ではなく、不確実性を一つずつ潰す順にする。

### Phase 0A：Resolve Capability Spike

一本の短いFixtureで次を実機確認する。

- Media Import
- Base CutのSource In/OutとRecord Frame
- Video、Audio Track配置
- Subtitleの一方式
- Intro、Outro
- Audio Presetの一方式
- Render Preset
- Item-level Readback
- Build Report

Exit Criteria。

- 同じTimeline IRからClean Buildを繰り返し、Item Conformanceが一致する。
- Resolve再起動後も同じ結果を得る。
- 失敗時に途中Timelineへ依存せず再実行できる。

Stop Criteria。

- Base CutのSource IdentityまたはFrame位置を安定して検証できない。
- 公式APIと公開InterchangeのどちらでもMVP必須機能を構成できない。

### Phase 0B：Coordinate and Conform Spike

FixtureへCFR、29.97、59.94、VFR、Rotation、Audio Offsetを含める。

Exit Criteria。

- Original PTS、Edit Source Frame、Audio Sample、Timeline Frameの変換がGolden Valueと一致する。
- VFR MezzanineのFrame DropまたはDuplicateをReportできる。
- SubtitleとAudioの同期ずれが許容値内に収まる。

### Phase 0C：Playable Preview and Natural-language Review Spike

Timeline IRから低解像度Preview MP4を生成し、通常の動画プレイヤーで確認する。人間はAIへ自然言語で修正指示を送り、AIがCorrection Commandへ構造化する。専用Review UIは作らない。

Exit Criteria。

- Resolveを起動せず、低解像度Preview MP4で構成、Trim、Subtitle修正を確認できる。
- 自然言語の修正指示を対象DecisionとReview Commandへ変換できる。
- 曖昧な指示だけを人間へ確認できる。
- Review Commandから新Edit Plan、新Timeline IR、新Previewを再生成できる。

### Phase 1：Talking-head Editorial Vertical Slice

実装する。

- Ingest
- Transcript
- Silence、Filler、False Start候補
- Media Query
- Selection Plan Proposal
- Validator、Committer
- Constraint Planner
- Edit Plan
- Timeline IR
- Low-resolution Editorial Preview MP4
- Natural-language Review Translator
- Review Events

Exit Criteria。

- 実素材でShot採否と発話削除の多くを、Preview MP4とAIへの自然言語指示だけで修正できる。
- 修正指示の大半を構造化Review Commandへ変換でき、曖昧なものだけ人間へ確認できる。
- Schemaで表現できない修正を分類できる。
- ResolveなしでもEditorial Qualityを評価できる。

### Phase 2：Resolve Finalization Vertical Slice

実装する。

- Resolve Adapter
- Clean Builder
- Subtitle
- Basic Audio
- Intro、Outro
- Render
- Build Verification
- Deterministic QC
- Final Review

Exit Criteria。

- Editorial Approved PlanからFinal Renderまでを一Jobとして完了できる。
- Build Failureが構造化され、RetryまたはHuman Escalationへ分岐する。
- Resolveを手作業で開かずに一本を公開候補まで作れる。

### Phase 3：Presentation Layer

追加する。

- Subtitle Style
- Keyword Overlay
- Chapter Title
- FusionまたはExternal Overlay
- BGM、SE
- Fairlight Preset
- Camera別Color Preset

Exit Criteria。

- Channel ProfileとAsset Registryの差し替えで見た目と音の基準を変更できる。
- Builder本体へChannel固有条件を埋め込まない。

### Phase 4：Travel、POV Assisted Mode

追加する。

- Scene Detection
- Motion Analysis
- Visual Similarity
- Shot Clustering
- Contact Sheet強化
- Visual Candidate Ranking
- Ambient Preservation
- Privacy Candidate
- Human Candidate Review
- J Cut、L Cut、B-roll

最初から完全自動Selectionを目指さない。AIが候補を絞り、人間が短時間で選べる状態を先に作る。

### Phase 5：Review Learning and Advanced QC

追加する。

- Review Event Retrieval
- Past Edit Plan Retrieval
- Profile Change Proposal
- Holdout Evaluation
- AI QC Ranking
- QC Review Budget
- False Negative Sampling
- Structured Correction Coverage
- 必要性が実測された場合のみDedicated Review UI

### Phase 6：派生動画

本編Pipelineが安定した後に、Shorts、Vertical Cut、Teaser、Digest、Trailerを追加する。Media StoreとCandidateは再利用してよいが、構成目的が異なるためEdit Planは別に作る。

## 31. 成功条件

### 31.1 人間時間

Supported Episode Contractを満たす直近Episode群で、Active Human Timeの中央値が30分以内へ近づき、P90も継続的に低下している。

### 31.2 Coverage

実際に作りたいEpisodeの多くがSupported Contractへ入り、契約外へ逃がす割合が減っている。

### 31.3 編集品質

Must Includeの見落とし、構造的な再編集、Critical QC Issueが許容範囲に収まり、First-pass Acceptance Rateが上がっている。

### 31.4 再現性

Committed Edit Planと固定されたToolchainから、同じ構造のTimeline IRとResolve Timelineを再Buildできる。

### 31.5 復旧性

OpenCode、MCP、Resolve、Model Providerのいずれかが停止しても、最後にCommitされたArtifactから再開できる。

### 31.6 追跡可能性

Final Render上の各ItemをDecision ID、Source Span、Review Event、Build Resultへ追跡できる。

### 31.7 修正容易性

人間の修正の多くをResolve操作ではなくReview Commandと構造化Artifactの変更として表現できる。

### 31.8 モデル交換可能性

Editorial Director、Visual Judge、Diagnostic Assistantを別Modelへ交換しても、Source Manifest、Media Store、Selection Plan、Edit Plan、Review Events、Timeline IRは残る。

### 31.9 NLE交換可能性

Timeline IRまではResolveに依存せず、別Adapterを追加すれば基本的な編集判断を移植できる。Resolve固有のPresentationはAdapter Assetとして明示される。

## 32. ディレクトリ構成

```text
video-pipeline/
  schemas/
    artifact-envelope.schema.json
    source-manifest.schema.json
    conform-map.schema.json
    job-manifest.schema.json
    selection-plan.schema.json
    edit-plan.schema.json
    timeline-ir.schema.json
    resolve-package.schema.json
    build-report.schema.json
    qc-report.schema.json
    review-command.schema.json
    review-event.schema.json

  config/
    system-defaults.yaml
    genres/
      talking-head.yaml
      travel.yaml
      pov.yaml
    channels/
      tokyo-walk.yaml
    contracts/
      talking-head-mvp-v1.yaml
      travel-assisted-v1.yaml

  assets/
    registry.json
    music/
    se/
    intro/
    outro/
    overlays/

  templates/
    resolve-projects/
    fusion/
    subtitle/
    fairlight/
    grade/
    render/

  capabilities/
    resolve-21/
      matrix.json
      fixtures/
      reports/

  services/
    job-runner/
    ingest/
    normalize/
    analyze/
    media-query/
    editorial/
    validate/
    plan/
    compile/
    preview/
    resolve-adapter/
    build/
    qc/
    review-command/

  jobs/
    tokyo-001/
      episode.yaml
      job-manifest.json
      source-manifest.json
      conform-map.json
      resolved-config.json
      media.duckdb
      artifacts/
        selection-plan/
        edit-plan/
        timeline-ir/
        resolve-package/
        build-report/
        qc-report/
      review-events.jsonl
      sources/
      analysis/
      contacts/
      proxies/
      previews/
      renders/
      manual-finalization/
```

## 33. V4.1で作るシステム

```text
Camera
↓
Immutable Originals
↓
Ingest / Normalize
↓
Source Manifest + Conform Map
↓
Analyzers
↓
Queryable Media Store

     Genre Profile
     Channel Profile
     Episode Config
     Approved Review History
             ↓

Editorial Director
↓
Selection Plan Proposal
↓
Validator / Committer
↓
Selection Plan
↓
Constraint Planner
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
├─→ Editorial Preview
│   ↓
│   Review Interface
│   （MVP: Standard Player + AI Chat）
│   ↓
│   Review Commands / Events
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
    AI QC
    ↓
    Final Review
    ↓
    Approved Artifacts / Profile Change Proposals
```

Editorial Directorの役割は、素材を理解し、編集判断をEvidence付きの機械可読なProposalへ変換することである。

Job State Machineの役割は、ArtifactのVersion、Stage、Retry、Gate、Budgetを管理することである。

CompilerとBuilderの役割は、時間、尺、配置、再現性、検証を保証することである。

Diagnostic Assistantの役割は、標準処理で解決できなかった例外を構造化Logから診断することである。

人間の役割は、撮影し、方向性と好みを決め、PreviewとIssueを確認し、公開責任を引き受けることである。

動画を作るたびに残る資産は完成MP4だけではない。Source Manifest、Conform Map、Selection Plan、Edit Plan、Timeline IR、Review Eventsが残る。十本目では、どの映像を選び、どのくらい残し、どんな判断を人間が修正したかを検索できる。

ただし、その蓄積は、汎用基盤を先に作る理由にはしない。最初の一本で減らせなかった人間作業を、次の一本で構造化する。その繰り返しがV4.1の実装原則である。

## Appendix A. V3からV4への主要変更

| V3 | V4 | 変更理由 |
|---|---|---|
| GLMがBuilder実行、状態確認、Retryを担当 | Job State Machineが通常処理を担当し、Diagnostic Assistantは例外診断だけを担当 | 標準処理をAgentの長いTool Chainへ依存させないため |
| OMOが親子Agentを編成 | OMOは開発、運用、例外診断のConsole | Production Stateを会話状態から切り離すため |
| Raw Source Frameを正準化 | CFR Edit Source Frame、Audio Sample、Original PTSを分離 | VFRとAudio同期を安全に扱うため |
| Edit PlanからResolve Compilerへ直結 | Edit PlanからTimeline IRを経由してResolve Adapterへ渡す | Previewと別NLE Adapterを同じ中間表現から作るため |
| ReviewはResolve BuildとQCの後 | Editorial Preview ReviewとFinal Reviewの二段階 | 編集判断とResolve障害を分離するため |
| Compiled Timeline PlanはResolve固有 | Timeline IRはNLE非依存、Resolve PackageだけがResolve固有 | 長期のNLE交換可能性を実質化するため |
| Timelineは常にBuild Artifact | Manual Finalization時だけManual Finalization Packageを正本にする | 手作業後の完成状態を失わないため |
| Model名がArchitecture上の役割 | RoleとModel実装を分離 | Model交換とVersion変化に備えるため |
| Capability MatrixにAPI可否を保存 | Fixture、Readback、Fallback、Known Limitationを保存 | 存在するAPIと安全に使える機能を区別するため |
| ProfileへReviewを反映 | Profile Change Proposalを人間承認 | 一時的な修正への過適合を防ぐため |
| 30分KPIを全体へ適用 | Supported Episode Contract、P90、Coverageを併記 | KPIを測定可能にし、契約外への逃避を防ぐため |

## Appendix B. 初期リスク登録簿

| Risk | 影響 | 初期確率 | 対策 | Gate |
|---|---|---:|---|---|
| AIのShot採否が人間の好みと合わない | 高 | 高 | Preview、Candidate-assisted Mode、Review Event | Phase 1 |
| VFRとMixed FPSでSource位置がずれる | 高 | 高 | CFR Mezzanine、Conform Map、Golden Fixture | Phase 0B |
| Resolve APIの機能不足 | 高 | 高 | Capability Matrix、Fallback Ladder、Hard Cut MVP | Phase 0A |
| Resolve Buildの失敗原因が不明 | 高 | 中 | Item-level Build Report、Structured Log、Staging Timeline | Phase 0A |
| 自然言語Reviewの時刻指定や反復指示が人間時間を食う | 高 | 中 | Preview MP4で先に運用し、頻出操作だけDedicated Review UIへ昇格 | Phase 1以降 |
| Profile Learningが過適合する | 中 | 中 | Change Proposal、Human Approval、Holdout | Phase 5 |
| Cloud処理で機密、個人情報が漏れる | 高 | 中 | Episode Data Policy、Local-only Mode、Audit Log | 全Phase |
| Asset権利情報が欠落する | 高 | 中 | License Evidence、Content Hash、期限管理 | Phase 3 |
| Manual変更を再Buildで失う | 高 | 中 | Drift Detection、Freeze、Manual Finalization Package | Phase 2 |
| 汎用基盤の開発が先行する | 高 | 高 | Phase Gate、Reference Episode、Stop Criteria | 全Phase |

## Appendix C. 最初に固定する技術判断

1. ひとつのResolve HostとひとつのJob State Storeから始める。分散実行は必要になってから追加する。
2. VFRはCFR MezzanineへNormalizeし、OriginalへのRelinkをMVP要件にしない。
3. Timelineの構造変更はClean Buildを既定にする。
4. PreviewはResolveを使わずTimeline IRから低解像度MP4として生成する。
5. Base Cut、Subtitle、Intro、Outro、Basic Audio、Renderだけで最初のEnd-to-Endを通す。
6. Transition、Retime、Native Multicam、Per-shot Grade、複雑なFusionはMVP外とする。
7. Profileは自動更新しない。
8. LLMはProposalを生成し、Validatorを通過したArtifactだけをCommitする。
9. BuildとRetryはJob Runnerが実行し、Agentは例外時だけ使う。
10. 30分KPIはSupported Episode Contract、P90、Coverage Ratioとセットで評価する。
11. MVPのReviewは標準動画プレイヤーとAIへの自然言語指示で成立させ、Dedicated Review UIは実測されたボトルネックがある場合だけ作る。

## Appendix D. V4からV4.1への変更

| V4 | V4.1 | 変更理由 |
|---|---|---|
| 専用Review UIをPhase 0C、Phase 1から実装 | MVPでは専用UIを作らず、標準動画プレイヤーとAIとの自然言語Reviewを使う | 動画レビューアプリの開発が先行し、E2E価値検証が遅れることを避けるため |
| Review Commandは主にUI操作から生成 | AIが自然言語の修正指示をReview Commandへ変換 | 同じ構造化Artifactを、低い実装コストで先に検証するため |
| Review UIで再生、Seek、入力、Approvalを計測 | MVPはReview Sessionと会話ログで概算し、Dedicated UI導入後に精密計測 | 計測精度のためだけにUI開発を先行させないため |
| 専用Review UIが初期アーキテクチャの前提 | 専用Review UIは長期ターゲット | 頻出操作と人間時間の実測結果に基づいて必要なUIだけ作るため |

## References

[R1] Blackmagic Design, DaVinci Resolve Studio 21 and 21.0.3 support information, 2026.

[R2] Blackmagic Design, DaVinci Resolve Scripting API documentation distributed with Resolve; API surface and behavior are additionally checked against live-tested community documentation.

[R3] samuelgursky/davinci-resolve-mcp, README, API Coverage and API Limitations, accessed 2026-08-15. The repository reports broad API coverage while documenting limitations around title destination tracks, trim and move operations, transitions, Fairlight controls, proxy generation, multicam and retime workflows.

[R4] FFmpeg Project, FFmpeg and ffprobe documentation. `time_base`, timestamp handling and CFR/VFR conversion behavior are used as the basis for the Conform design.

[R5] OpenAI, GPT-5.6 official product information, accessed 2026-08-15.

[R6] Z.ai, GLM-5.2 official product information, accessed 2026-08-15.
