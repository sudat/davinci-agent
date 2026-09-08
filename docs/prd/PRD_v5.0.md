# ダビンチエージェント PRD V5.0.8

## 0. 文書メタデータ、権威関係、読み方

| 項目 | 内容 |
|---|---|
| 文書 | ダビンチエージェント PRD V5.0.8（V5.0.8: 2026-09-08 工程2P（進捗と待機の可視化）の実装検証を記録。検証済み: backend ruff全pass・pytest 396 passed/2 skipped・job_runner 72/72・basedpyright 0エラー / UI typecheck・vitest 142 passed・production build / 隔離browser E2E 17 passed・2 skipped（port 8799で実backend+実frontend。2 skippedは実chainの`LIVE_V44_E2E=1`ゲート）。受入specの陳腐化assertion 2件を2P正直語彙へ整合（`PREVIEW_READY`の接尾辞表示対応、rebuild phase語彙に正直な停止状態を追加）。合成fast-path環境はrun/成果物を持たないため実rebuild再renderの完走を検証せず、完走の証明はlive-episode.spec.ts（実chain）が担うとspecコメントへ明記。2P設計表は`private/runtime/sol-ux-redesign-20260907/step2p-design.md`（codex条件8項を反映）。【履歴】V5.0.7=2026-09-08 工程2修正ラウンド4のcodex独立レビュー**合格**（指摘修正範囲）。静止画の配送事実契約は現行どおり `frames_delivery_attempted`=**呼出試行**（起動前失敗を含む・**実配送は未確認**）/ `frames_verified`=試行かつ応答あり。ラウンド3=順次原子性+適用記録境界、ラウンド2=束ね/代替案区別+3段階正直性、ラウンド1=安全対策（いずれも合格）。【未確認】codexの2P独立レビュー結果。実素材・実LLMでの2P動作。実素材・実LLM・修正後実ブラウザE2Eの製品合格。 |
| 作成・証拠カットオフ | 2026-09-07（Asia/Tokyo） |
| 対象 | sudaさんの実際の動画制作。DaVinci Resolve Studio 21 と Cockpit を用いる |
| リポジトリ | `/Users/stc/Developer/davinci-agent` |
| HEAD | `368314259df0f7d4c3d8788bd8cc78093279136b`（`main` / `origin/main`） |
| 作業木 | UX工程1・工程2・工程2PのWIP（codexレビュー合格までコミット禁止）。本文の `in_progress_wip` はこの差分を含む |
| 正本 | 本PRD（製品の方向・要件・受入基準） |
| 実行計画 | `docs/prd/implementation-plan-v4.4.md`（実装順序・作業契約の根拠。V5工程へ読み替える） |
| 改修指示書 | `docs/prd/ux-redesign-implementation-brief.md`（UX要求。正本を上書きしない） |
| 歴史資料 | `docs/prd/PRD_v4.4.md` と `video-pipeline/capabilities/v4.3/**`（書き換えない） |

### 0.1 権威と状態の読み方

本PRDは、V4.4から派生して現在使っている実装・運用知識・UX改修指示をひとつの製品仕様へまとめる。証拠の強さは次の順で読む。

1. **verified_current**: カットオフ時点で、実ファイル・実行ログ・実機画面/読み戻しなどで現行動作を確認できたもの。
2. **implemented_unproven**: コードやテストは存在するが、実素材・実UI・実Resolveによる製品受入がまだないもの。
3. **in_progress_wip**: 作業木に差分があり、テストや機構の一部は確認できるが製品経路へ接続されていないもの。
4. **target**: 目指す製品要件。実装済みを意味しない。
5. **blocked**: 必須条件不足または明示的な実測失敗により、現在の受入を止めるもの。
6. **deferred**: First Publish前の凍結対象、または実素材の需要が出るまで保留するもの。
7. **historical**: V4.3/V4.4の過去記録。現状の合格証明には使わない。

「実装済み」は機構の存在を示すだけでは足りない。製品品質を主張するときは、ソースファイル/commit、実行条件、実素材または実機、成功ログ、画面または読み戻し、オペレーター判断を同じ証跡へ紐づける。PRD自身の記述、synthetic fixtureの成功、MCP probeのacceptedは、単独では製品合格の証拠にしない。

### 0.2 Evidence Scope（証拠範囲）

このPRDの証拠カットオフは2026-09-07である。同期された`/sources/` snapshotに含まれるのは`PRD_v4.4(1).md`と`Muse_metacua_Computer_Use_環境メモ.md`の2資料だけであり、派生改修指示書・実装・テスト成果物の唯一の所在とはみなさない。現行判断は、絶対パスで確認した`/Users/stc/Developer/davinci-agent`の実repo、`/Users/stc/.metacua`、および秘密値・実素材を含まない次の日付付きprivate observationに依存する。

- `/Users/stc/Developer/davinci-agent/private/reference-episodes/v44-real-01/runs/observation.json`
- `/Users/stc/Developer/davinci-agent/private/reference-episodes/v44-real-01/runs/observation-t11/observation.json`

旧README・旧PRDは設計/歴史の根拠として使い、同じ対象に上記のような日付付きprivate observationがある場合は、現行機構の観測には後者を採用する。ただしprivate observationもその条件で機構が動いた証拠に過ぎず、product pass、利用者の満足、公開可否を証明しない。

古いREADMEや過去PRDの記述は設計・歴史の根拠として保持する。実素材本体、APIキー、個人情報は本PRDへコピーしない。

V5.0のauthoritative artifact方針は、既存の`EditorialGroundTruthV1`と`ProductProofReportV1`を維持し、First Publish前に第3のauthoritative artifact typeを追加しない。必要な状態・証拠は既存artifactの参照、event、route record、Evidence Ledgerへ収める。

### 0.3 設計原則

- **YAGNI**: First Publish前は新しい汎用フレームワーク、抽象層、学習機構、artifact族を増やさず、実素材で測定したブロッカーだけを直す。
- **KISS**: 既存のCockpit、Review、Artifact、単一Writerの流れを小さく接続し、感想から試作までの経路を短くする。
- **DRY**: 既存の版管理・検証・実行経路を再利用する。同じ動作であることをテストで示せない統合は行わない。
- **Artifact-first**: チャット履歴や開いたTimelineを正本にせず、版・証拠・採用判断を保存する。
- **Proposal / Commit分離**: AIは提案まで。検証済みの提案だけを単一Writerがコミットする。
- **実素材優先**: syntheticは回帰・安全試験に使えるが、編集品質の証明には使わない。
- **不確実性を見せる**: 「確認済み」「原因仮説」「未対応」「手動確認必要」を混同しない。

## 1. Executive Summary

### 1.1 現在の姿

ダビンチエージェントは、カメラ素材を取り込み、解析し、AIが編集案を作り、プレビュー、自然言語による修正、部分再構築、DaVinci Resolve仕上げ、QC、公開準備までをつなぐ基盤を持つ。V4.4の広い基盤に加え、現行WIPでは「退屈」「映画っぽくない」のような感想を原因調査へ回し、仮説を表示し、採用済み版からの復帰を履歴として残す方向へ拡張している。

ただし、現在確認できるのは機構と一部の実素材経路であり、完成した製品UXではない。V44-1は実素材のCockpit intake→preview→自然言語修正→部分再構築の機構証明があるが、TTFRP（最初に判断できるプレビューまで）は約761.38分、部分再構築には別runで115.4秒と490.339秒の観測があり、定常UXの水準には未達。r4の77/77はオペレーターが補正した診断値で製品合格ではない。system ASRはCER 0.1037、timestamp p95 5740ms、欠落31、重複37でblockedである。

MCP v2.210.0は22 probe中17 accepted / 5 failed。失敗は transition-path、audio-property-operation、bgm-track-ducking、edit-engine-selects、alternate-shot-similarity。acceptedは「その操作面に届いた」ことの証明であり、実素材の品質・見た目・公開可否の証明ではない。

### 1.2 目指す姿

利用者は技術用語や秒数を探さず、素材フォルダ、今回の動画の説明、必要なら参考コメントを渡し、「ここ退屈」「もっと落ち着いて」「前の案に戻して」と話す。システムは映像・音・文字を実際に確認し、原因を仮説として説明し、最大二案の短い試作品を提示する。利用者の採用・却下・復帰を確認したあと、影響範囲だけを再構築し、編集可能なResolve納品物として仕上げる。公開は必ず利用者が承認する。

### 1.3 V5.0の判断

V5.0は「新しい大規模基盤を作る版」ではなく、V4.4の仕組みを現在のUX責任へ反映し、**感想→調査→仮説→試作→比較→採用/却下/復帰**を製品の主導線にする版である。First Publish前の広い基盤追加凍結は維持する。V5.0の完了判定は、テスト件数や画面の存在ではなく、代表実素材で利用者が続きを作る価値を感じ、自然言語修正を経て、公開可能と明示することとする。

## 2. V4.4からV5.0への変更点

| 観点 | V4.4 | V5.0 |
|---|---|---|
| 中心課題 | 最初の公開可能な実エピソードを通す | 実エピソード経路を、感想で任せられるUXへ定着させる |
| 入力 | Source folder + brief + optional reference | 同じ入力に、チャンネル/スタイル/届け先/今回だけの条件を明示的に加える（既存互換を保持） |
| 感想 | bounded parserとNL reviewの製品化が未証明 | 感想を命令へ強制変換せず、観測・原因仮説・試作・判断を記録する |
| 版管理 | Proposal/Commit、復旧基盤 | 採用版・未採用試作・復帰版を一つの履歴で扱う。復帰は新しい前向き版 |
| UX | Intake/status/reviewの基盤 | 原因仮説、調査済み、差分説明、revert/restoreを画面とAPIで接続 |
| 仕上げ | 7品質領域を評価 | 全領域を考慮し、適用済み/意図的に不要/手動対応必要/blockedを表示。publishabilityは別判定 |
| 証拠 | V44 gatesと機構証明 | 証拠台帳、状態分類、gate間の矛盾、現在のWIPを統合して再判定 |
| 学習 | 重いreference/profile learningを将来扱い | Taste Seed v0のみFirst Publish対象。広い学習は凍結 |
| フォーマット | 横型中心、縦型は目標/未検証が混在 | 実需要が出た出力ごとに独立版・独立承認・独立QC。縦型全体は別gate |

## 3. 対象ユーザー、Jobs、成功体験

### 3.1 主利用者

主利用者はsudaさん。編集者向けの数値・Resolve用語を覚えていることを前提にしない。利用者ができるのは、素材を渡すこと、動画の意図を短く話すこと、映像を見て「前より良い/違う/任せる/戻す」と判断することである。

### 3.2 Jobs to be Done

- 素材を置いたら、何を確認しているか分かる状態で最初の編集を見る。
- 「退屈」「素人っぽい」などの感想を、技術的な命令へ言い直さず伝える。
- なぜそう感じるのかを、システムが観測事実と仮説に分けて示す。
- 試作品を少数比較し、理由がなくても採用・却下・前の案への復帰を選ぶ。
- チャンネルの好み、今回だけの条件、公開可否を別々に管理する。
- 必要な場合だけResolveの編集可能な納品物を受け取り、公開を自分で承認する。

### 3.3 成功体験

「素材を渡して一言話すだけで、システムが本当に映像と音を確認し、違いの分かる試作品を返し、私は良い/違うを選べる。採用した案は戻せて、Resolveで後から編集できる。公開するかは私が決める」と感じられること。

## 4. 現在のシステム構成と状態台帳

### 4.1 構成

```text
入力: source folder + episode brief + optional reference/taste comments
  ↓
Ingest / normalize / conform（原素材→編集用の時間座標）
  ↓
Media Intelligence（映像・音・日本語文字・証拠の検索可能化）
  ↓
Editorial Director（Story → Moment → Creative の三段階提案）
  ↓
Proposal → validation → Commit（単一Writer）
  ↓
Editorial Preview
  ↓
Review: 感想 → 観測 → 仮説 → 試作 → 比較 → 採用/却下/復帰
  ↓
Partial rebuild（影響範囲のみ） / Clean rebuild（構造変更）
  ↓
Timeline IR → Resolve adapter / Interchange / Template / Computer Use
  ↓
Finishing（7 quality domains）→ technical/editorial QC
  ↓
Final Preview → operator publishability → publication approval → upload/schedule
```

Control PlaneはJob State、Artifact Registry、Capability Matrix、Resolved Configuration、Budget/Policy Guard、Observabilityを担う。各工程は入力artifactのhashとrunner versionから再実行識別子を作る。

#### 4.1.1 Lifecycle state と control flag

ライフサイクル（制作の段階）とcontrol flag（停止・再試行など）は別軸で持つ。候補の判断を制作段階の状態へ直接混ぜない。

| 軸 | 値 | 意味 |
|---|---|---|
| lifecycle | `CREATED → INGESTED → NORMALIZED → ANALYZED → PLAN_PROPOSED → PLAN_COMMITTED → PREVIEW_READY → CONTENT_APPROVED → RESOLVE_BUILT → QC_PASSED → FINAL_APPROVED → PUBLICATION_APPROVED → PUBLISHED / SCHEDULED → FROZEN` | artifactと制作成果の進行。`CONTENT_APPROVED`は編集内容、`FINAL_APPROVED`は最終映像、`PUBLICATION_APPROVED`は宛先への公開操作を承認する別状態 |
| control | `active` | 通常実行中 |
| control | `paused` | 利用者が再開するまで停止。採用版や承認は失効させない |
| control | `blocked` | 入力・能力・権利・安全条件が不足。理由と解消条件を必須にする |
| control | `failed` | 実行が失敗。原因分類、retry可否、復帰先を記録 |
| control | `retrying` | 許可された一時失敗だけ再試行中。無制限retryは禁止 |
| control | `canceled` | 利用者または安全ポリシーで中止。公開承認を自動復活させない |

承認記録は対象final artifact hash、output/destination、visibility、title/description/metadata、actor、時刻、対象plan version、根拠証拠を束ねる。対象artifactまたはmetadata、destination、visibility、scheduleが変更されたら承認を失効させる。retryやschedule変更は、同じhashを再利用できても再承認を要求する。upload/scheduleのremote ack（remote id、status、時刻、返却値のredacted要約）とidempotency keyを記録し、`PUBLISHED`/`SCHEDULED`はremote ackなしで遷移しない。

#### 4.1.2 候補・採用・復帰の遷移と不変条件

| 操作 | 遷移 | 必須条件 |
|---|---|---|
| 候補生成 | 採用版 → `candidate-A/B` | 元版hash、Proposal、route/evidenceを保存。採用版は不変 |
| 採用 | candidate → `PLAN_COMMITTED` → `CONTENT_APPROVED`（必要時） | operator actor、対象版一致、validation/lock/capability pass |
| 却下 | candidate → `rejected` | 理由は任意。採用版・公開承認は変えない |
| 両方違う | candidate集合 → `investigation_required` | 仮説を再検討し、同じ案を自動保存/再提示しない |
| 任せる | `investigation_required` → bounded candidate | 事前に定めた案数・時間・費用内。公開/永続style保存は承認しない |
| 復帰 | 任意の過去版 → 新しい`plan_restored`版 → validation | 過去版を破壊しない。復帰元hash、actor、時刻を保存 |
| 競合 | candidate採用中にhead変更 → `blocked`/再確認 | 古いcandidateを無条件適用しない |
| クラッシュ/再接続 | active → `failed`/`retrying` → 同じstageまたは`paused` | idempotency、既存event、採用版、未回答判断を読み戻す |

不変条件は、(1)採用版だけが納品・公開経路の入力、(2)未採用candidateはpublishabilityを変更しない、(3)一つのeventを二重適用しない、(4)承認対象hashが変われば再承認、(5)remote ackなしで公開済みとしない、(6)一つのWriterだけがResolve/Job Stateを更新、である。状態の変更が何を無効化したかを常に表示する。

#### 4.1.3 Content / final / publication approval

`CONTENT_APPROVED`は選択・構成・表現の採用、`FINAL_APPROVED`は最終renderと7領域/QC、`PUBLICATION_APPROVED`は特定のdestination・visibility・metadata・scheduleに対する公開操作の承認である。いずれもactor/time、対象artifact hash、plan versionを持つ。最終renderを差し替えた、タイトル/説明/thumbnail/visibilityを変えた、宛先やscheduleを変えた場合、該当承認と後続状態を失効させる。

公開処理は`PUBLICATION_APPROVED → PUBLISHED`または`PUBLICATION_APPROVED → SCHEDULED`の一回限りのidempotency keyを使う。upload/scheduleのremote acknowledgementが無い、または返却statusが不明な場合は`blocked`/`failed`に留める。retryは同じ承認を無条件に再利用せず、対象hash・destination・visibility・metadata・scheduleが同一であることを検証し、変更があれば再承認する。remote id、返却時刻、成功/失敗、再試行回数をredactedで記録する。

主要な正本はSource Manifest、Conform Map、Job Manifest、Committed Selection/Edit Plan、Review Events、Timeline IR、Resolve Build Report、QC Report、Final Render。Proxy・analysis frame・preview・Resolve packageは再生成可能なartifact。Runtime stateやDB indexを正本にしない。

### 4.2 状態台帳（カットオフ時点）

| 対象 | 状態 | 現在の観測 | 製品上の扱い | 根拠 |
|---|---|---|---|---|
| 基本Artifact/版/単一Writer | verified_current | 既存契約・イベント履歴・復元イベントの機構がある | 再利用 | `PRD_v4.4.md` §§2,3, `implementation-plan-v4.4.md` §§3,19 |
| MCP v2.210.0 probe | verified_current | 22中17 accepted / 5 failed | acceptedは機構fit、失敗はfallback選択へ | `capabilities/v4.4/mcp-fit.json` |
| Resolve 21.0.4.5 | verified_current（fit artifact） / live unknown | MCP fit保存時の対象build。各runではlive handshakeを毎回要求 | 保存済みfitやindexだけで現在接続中のbuild・能力と断定しない | `mcp-fit.json`, `~/.metacua/DAVINCI_CAPABILITY_INDEX.md` |
| 実素材Cockpit intake→preview→NL修正→部分再構築 | verified_current（機構） / implemented_unproven（製品UX） | V44-1の`/Users/stc/Developer/davinci-agent/private/reference-episodes/v44-real-01/runs/observation.json`（2026-08-25）と`.../observation-t11/observation.json`（2026-08-30）に機構観測がある。両方とも`current_stage=intake`、`job_status=PREVIEW_READY`であり、現在ライブ実行中という意味ではない | UX SLO未達。製品合格には再計測 | 上記2 observation、現行実装・差分 |
| TTFRP | blocked（目標未達） | `ttfrp_seconds=45682.96`秒（約761.38分）を2観測が共有する。代表値は、同一episode/同一出力条件で、最新の完了runを優先し、条件が違えば中央値ではなくrun別に併記する規則をfreezeする | まず計測定義・代表値規則を固定し、工程別ボトルネックを削減 | 上記2 observation |
| Partial rebuild | implemented_unproven | 観測値115.4秒と490.339秒があり、機構・変更範囲・環境が異なる可能性をrunに保持 | 単一代表値へ丸めず、同条件比較後に製品SLOを決める | `private/reference-episodes/v44-real-01/runs/observation*.json` |
| V44-0 ASR | blocked | system CER 0.1037>0.10、timestamp p95 5740ms>500ms、欠落31>5、重複37>5 | V44-0/V5 Editorial gateを止める | `PRD_v4.4.md` §6.7、ProductProofReport |
| r4 77/77 | implemented_unproven | operator-corrected diagnostic。製品合格ではない | system ASRを置換しない | `PRD_v4.4.md` §6.7、証拠台帳 |
| UX工程1: feelings/hypothesis/investigated | implemented_unproven | 2026-09-07 codex再レビュー合格（実装機構）。feelings調査ルート・investigation_state 3値の正直表示・P0保存済み提案のみ適用（proposal ledger + 全項目一致 + stale/consumed拒否）。gate: pytest 320+2skip / vitest 91/91 / tsc清潔 / build成功。実素材・実LLM・実ブラウザ操作は未確認 | 実素材・実UIでの製品受入が必要 | `git diff`, codex再レビュー2026-09-07、2026-09-07 current test snapshot |
| UX工程1: revert/restore/plan_restored | implemented_unproven | 新しい版として復帰するイベントとreducer/store差分。2026-09-07 codex再レビュー合格: 復帰の再実行安全性（未起動復帰の再送は同一step再起動・別versionへ進まない）、起動失敗時の正確な状態表示（版確定＋再構築未起動）含む | 実素材・実UIでの復帰、競合、再起動受入が必要 | `git diff`, codex再レビュー2026-09-07, `ux-redesign-implementation-brief.md` |
| Intake追加入力 | blocked / in_progress_wip | 画面値がAPIへ未送信 | API契約・保存・runnerまで一体で修正 | `ux-redesign-implementation-brief.md` §4、current diff |
| Review TS型/原因仮説/復帰ボタン | blocked | frontend型未反映、原因表示なし、revert未接続 | APIだけを完成扱いしない | `ux-redesign-implementation-brief.md` §4、current frontend audit |
| 2階層テロップ、字幕style、scale-to-fit | verified_current（要素限定） | 実ユーザー評価・実機適用記録あり | approved sampleを再利用。ただし理想全体とは別 | `style-vocabulary.md` A、implementation plan progress |
| 宋世羅風、TikTok風、地点表示 | target / deferred | 目標表現。具体値・必要素材・全経路未検証 | 需要が出た機能を一件ずつprobe | `style-vocabulary.md` B/C、`operator-wishlist.md` |
| Metacua GUI実行層 | verified_current（現行基盤） / historical（旧メモ） / implemented_unproven（製品経路） | 現行stateは`/Users/stc/.metacua`、wrapperは`/Users/stc/bin/metacua-go`、max stepsは400、wrapper BashはON、モデルはMuse Spark 1.3 Contributor。旧メモの`/Users/stc/.meta-cue`、40 steps、Bash OFFは歴史値であり現行設定ではない。max400はwrapperの起動経路、`/Users/stc/Developer/meta-model-cookbook/03_use_cases/13_macos_cua/python/metacua/agent.py`の`DEFAULT_MAX_STEPS=400`、および`/Users/stc/.metacua/traces/*.jsonl`の実行記録を突合して扱う | MCP/script優先、CUは不足面のfallback。Resolve実編集の全経路は未証明 | `/Users/stc/.metacua/AGENTS.md`, `/Users/stc/bin/metacua-go`, `/Users/stc/Developer/meta-model-cookbook/03_use_cases/13_macos_cua/python/metacua/agent.py`, `/Users/stc/.metacua/traces/*.jsonl`, `DAVINCI_*` |
| ネイティブ字幕SetProperty | blocked | Resolve 21.0.4.5で3/3 hang→crash。readは安全 | 書込経路に使用しない。template/interchange/CU/HITLへ | `DAVINCI_KNOWLEDGE.md` S09 |
| Fusion | implemented_unproven / blocked per feature | readbackだけでは映像反映を証明できず、render A/B必須。Blurはrender失敗実測 | render A/Bなしで合格にしない | `DAVINCI_KNOWLEDGE.md` F02/F04 |
| 直近のGit状態 | verified_current | mainはcleanではなくUX WIP差分あり。秘密/実素材は本文へ記載しない | 変更対象と未確認を分けて管理 | `git status --short --branch` |

### 4.3 目標と証拠の境界

V5.0は「V44-1の機構証明あり」を「V44-1製品合格」と読み替えない。V44-0はASRと証拠品質がblocked、V44-2はpublishability・実仕上げ・AHT証拠が揃うまでblocked。各gateの歴史的BLOCKED記録と現在WIPが矛盾するときは、履歴を改変せず、上表の状態を現行分類として使う。

## 5. UX主導線

### 5.1 通常フロー

1. 画面で素材フォルダを選ぶ。
2. エピソードの説明を書く。チャンネル、スタイル版、届け先、今回だけの条件は任意入力とし、既知の前回値を表示する。
3. システムは「素材確認中」「最初の試し編集を作成中」のように、測定できる範囲だけ進捗を示す。未計測のETAを表示しない。
4. 最初のPreviewを視聴する。音が原因の可能性がある場合は音付き動画で確認する。
5. 感想をそのまま送る。感想だけなら即時に編集を確定せず、対象と周辺・全体を調査する。
6. 観測事実、原因仮説、保持する条件、実行可能な提案を短く表示する。
7. 原因が明白なら一案、判断が分かれる場合だけ最大二案の試作品を作る。1依頼につき初期予算は二案まで。
8. 利用者は採用、却下、両方違う、任せる、前に戻すを選ぶ。理由は任意。
9. 採用は保存済みProposalと表示中の版を照合してCommitする。試作品は採用版を上書きしない。
10. 字幕など低リスクな変更はpartial rebuild、構造変更はclean rebuildへ進む。
11. 7領域の状態、QC、最終Previewを確認し、利用者がpublishableを判断する。
12. 公開は別の明示承認後にのみ行う。

### 5.1a 編集前の方向性相談（2026-09-07合意追記、target）

§5.1の素材受付後・最初のPreview作成前に、予算内の方向性相談を挿入する。実装順序は改修指示書の工程2完了レビュー→工程2.5→工程3。工程2.5と独立した時間計測作業Mの詳細・受入条件は [改修指示書 §9](ux-redesign-implementation-brief.md#9-実装工程と担当の切り方) を現在の実行差分とする。旧v4.4の実行計画と過去の証拠は保持する。本追記は実装済み・製品合格の宣言ではない。

- 粗い素材確認から構成・狙い・尺・候補場面・表現方針・未確認を日本語で一案、判断が分かれる場合最大二案提示する。案の成立を左右する未確認部分は予算内で限定的に詳しく確認できる。
- 見本は二案合計30秒以内。相談までの壁時計・費用・追加確認回数も実行前に固定し、再試行を累計へ含める。上限到達時は追加処理を止め、完成分と確認済み情報を保持する。UsageLimitは解除・迂回・変更しない。
- 採用・修正・却下・両方違う・いつもの方向で任せるを理由任意で扱う。記録済みの委任は対象・条件・予算が一致すれば再確認を省略できる。無回答を許可とせず、構成のみ採用など判断範囲を保持する。
- 方針採用は、映像のCONTENT_APPROVED、最終確認、公開、永続スタイル保存を兼ねない。既存Proposalと判断記録へ対応付け、Story→Moment→Creativeの入力へ接続する。独立した正本型・状態機械は増やさない。
- 参考動画で観測した特徴と今回取り入れる選択を分け、継続保存は工程3の明示操作へ接続する。文章・静止画だけで音やテンポを確認済みにしない。初回映像の字幕・BGMの有無は判断目的に応じて選ぶ。
- 原因探索と見本提示は製品、作品の採用・公開可否は利用者の責任。原因不明でも可逆な予算内試作は仮説として行える。意味の曖昧さ・希望の衝突・安全な経路なしは既存stop_and_askに従う。評価実験の事前凍結を日常制作の全素材分類義務にしない。
- Mは既存run記録を先に調べ、足りない計測を次回runで取得する。相談・短い見本・全編ラフの到達時刻、人の判断時間、承認待ちを分離する。既存TTFRPの定義と過去値は維持し、文章提示を映像到達として数えない。ASR等を計測前に主因と断定しない。

受入は改修指示書U18〜U25と実素材の「選択した方針が試し編集に反映され、違えば相談へ戻れる」往復。既存のV5-INT/EDT/UX/REV/STY/METおよびV50-1の確認対象へ追加し、旧U01〜U17と安全条件を維持する。機構試験と本人による製品判定を分ける。

### 5.1b UI方向性と待機表示（2026-09-07合意追記、target）

4枚の生成見本は「素材と希望→方針相談→試し編集と対話→完成確認」という操作の方向性を示す。正式な配置・装飾・文言の確定ではない。改修指示書§3.4を文章による実装契約とし、既存DESIGN.mdに沿って実画面で検証する。

全待機経路で改修指示書§3.5を必須にする。操作後1秒以内の送信反応、10秒超の待機で現在作業・完了と次段階・経過・最後の実進捗・操作要否を表示する。実件数のみ表示し、架空の百分率・残り時間を出さない。通信状態、処理担当の動作報告、実際の成果進捗を分ける。表示中は原則5秒以内の更新を試み、15秒以上古い状態は更新途絶と表示する。工程別の停滞判定値は実測と既存timeoutから実装前に固定する。秒数はUIの受入目標であり処理時間保証ではない。

確認待ち、再試行と上限、状態不明、停止要求と停止確認、失敗、今回の成果物確認後の完了を区別する。再接続で同じrunへ戻り、過去の成功を今回の完了にしない。詳細・再確認・安全な中止等の実行可能な操作を提示し、無限再試行やUsageLimit解除を認めない。

実装順序は工程2の完了レビュー→工程2P（進捗と待機）→工程2.5。工程2.5の新規相談待機と後続仕上げにも同じ条件を適用する。独立計測Mは先行調査可能。V5-INT-003・UX-003・NFR-001およびV50-1の受入に改修指示書U26〜U31を追加する。進捗復元の最小範囲は今回のFIRST-PUBLISH差分とし、広い定常運用基盤の追加は含めない。本追記は実装済み・実画面検証済みを意味しない。

### 5.1c 生成絵コンテで方向性を合わせる（2026-09-08追記、target）

Codex App Serverの画像生成を使う絵コンテは、全編編集前の構成・見た目の相談、および見た目の修正で、説明の言い直しと作り直しを減らすために用いる。用途・操作・判断範囲・実編集への引き渡しは実装前に定義し、画像生成が動いてからUXを考える順序にしない。詳細な実装・受入契約は [改修指示書 §8](ux-redesign-implementation-brief.md#8-設計-絵コンテによる方向性相談と-codex-app-server) とU32〜U39に置く。本追記は設計要件であり、実装・実生成成功・製品受入を意味しない。

- 基本一案、必要な比較のみ最大二案・各代表場面最大三コマ。場面順・役割・元素材・変更点・未確認を示す。三コマと三つの代替案を混同せず、単一場面なら一コマでよい。迷いがない回や委任範囲が合う回は生成を省略できる。
- 撮影素材の切り出し、生成した見た目の案、実際の試し編集を区別する。「二枚目だけ直す」「構成はA、字幕はB」を理由任意で扱い、無関係なコマや選択を保つ。実素材にない人物・物・出来事や実現不能な表現は、実現可能な編集として黙って採用しない。
- 「この方向で試す」は今回の構成・表現と試作範囲への判断。音・テンポの確認、完成映像の承認、恒久スタイル保存、公開、生成画像を動画へ挿入する許可を兼ねない。既存の提案・判断記録へ案/コマ/元素材/元版/選択範囲を対応付け、実編集へ引き渡す。新規正本型や汎用基盤は追加しない。
- 実素材の試し編集を選択した案と対応付け、方針を再入力させず比較・再相談できる。生成画像だけで面白さやテンポを証明せず、画像表示を実動画のTTFRPに数えない。
- 部分失敗・一コマ再生成・古い版・再接続でも完成画像と選択を保つ。画像・動画を同じ相談の累計予算で扱い、§5.1bの進捗表示を適用する。利用者の判断時間、待ち時間、言い直し、実編集の作り直し、本人評価で効果を確認する。

工程2.5の実装前にこの契約を既存の型・保存・画面・編集入力へ対応付け、工程4で実生成を接続して往復を検証する。生成未接続で先行する相談経路の受入と、生成絵コンテを使うUXの受入を分ける。工程2の現在の修正範囲・工程2レビュー→2P→2.5→3→4の順序・既存安全条件は維持する。技術的な生成成功と本人による判断しやすさを別に評価する。

#### 5.1c.1 画像生成を選んだ場合の任意シナリオ（2026-09-08追加、target）

「落ち着いた動画にしたいが、場所が変わる時は大きなテロップで示したい」を代表入力とする。製品は撮影素材の代表フレームを元にA/B加工案を生成し、好みに近い案を聞く。「A案がいいがBのフォントが好き、背景はもっと暗く」という返答を要素別に統合し、その新方針による複数シーンの静止画を返す。静止画段階が問題ないとの判断または該当する委任を得てから、同じ方針の短い実素材動画で動き・表示時間・音・つながりを確認する。違えば対象だけ修正し、納得した方針で全編へ進む。各段階で同じ指示を入力し直させない。

通常場面の落ち着き、場所変更時だけの大テロップ、特定シーンの例外を区別する。カット変更をすべて場所変更にせず、暗くする背景の範囲が曖昧なら画像または短い質問で確かめる。生成画像の人物・場所を撮影内容から変えず、実際に利用可能なフォント・字幕配置・色・動きへ対応付ける。架空の書体や再現不能な表現を黙って採用せず、サンプルを生成静止画のスライドショーで代替しない。

詳細は改修指示書§8.7、連続受入ケースはU40〜U43。工程2.5の設計に入力から保存・混合方針・適用条件・実設定への対応を含め、工程4では静止画と実動画を通した一連の往復を検証する。比較・修正・複数シーン展開・動画試作の予算は累計で保持する。段階ごとの不要な承認を増やさず、無回答や未解決の修正を了承に読み替えない。本追記で実装済み扱いや現在の工程2への割込拡張は行わない。

#### 5.1c.2 毎回の画像生成を前提にしない（2026-09-08見直し、target）

画像生成は既定OFFの任意補助であり、§5.1c/.1の生成・A/B・生成見本の展開は明示希望または説明後の了承と予算範囲がある場合だけ適用する。初回・好み未確定・「絵コンテを見たい」だけでは生成を起動しない。生成しない通常経路は、希望→文章・場面順・実素材フレームによる相談→実字幕・色・配置設定の静止画または短い実編集動画→部分修正→全編とする。生成なしでも同じ方針・条件・例外・復元を保持し、新しい好みを具体化できることを必須とする。

生成OFF時は画像生成呼出0回とし、生成接続・認証・生成画像IDを主経路の必要条件にしない。生成の拒否・中止・失敗・利用上限時は方針を失わず通常経路へ戻る。生成は必要最小限とし、A/B・再生成・複数場面展開・再試行を無断で連鎖させない。既に許可された枚数・修正回数の範囲は再承認不要、超過時だけ負担と追加範囲を説明して確認する。利用枠が不明なら不明と示し、無消費や固定消費率を約束しない。

画像生成ON時は、生成担当のCodexセッションをLunaで起動し、実際の選択モデルを確認・記録してから生成する。統合担当がAstraでも生成はLuna側で実行する。Luna利用不能・モデル確認不能なら生成呼出0回とし、別モデルで代行せず生成なしの経路へ戻れる。LunaはCodex側の担当モデルであり、画像生成モデルの名称ではない。利用者の追加観測ではLunaによる一枚の生成は5時間枠の1%未満で、利用可能と判断された。固定消費率の保証にはしない。既存起動経路への最小差分とし、製品全体のモデル変更は行わない。

工程2.5で生成なしの主経路を先に受け入れ、工程4の任意生成経路の受入とは分ける。利用枠節約を目的とするChatGPT Web連携の追加開発は今回行わず、Web/API/別エージェントへの無断切替で上限を回避しない。詳細と今回の消費観測の限界は改修指示書§8.8、受入はU44〜U47。既存工程順序・安全条件は維持し、この文書変更を実装済みとしない。

### 5.2 感想の扱い

「ここ退屈」「全体が素人っぽい」「もっと映画っぽく」は `feelings` として調査へ送る。再生位置があっても、その一点だけが原因だと断定しない。具体命令（「この区間を削除」「7:41の字幕を直す」）は既存の安全な構造化コマンドへ送れるが、感想語が含まれても明示命令を優先する。

システムは最低限、元の発言、対象版、観測した映像/音/文字の参照、原因仮説、提案ID、試作状態、利用者判断を残す。仮説は事実やAI確信度と別表示にする。原因を確認できない場合は「未確認」と表示し、映像にない要素を発見したことにしない。

### 5.3 チャンネル・スタイル・届け先の分離

優先順位は「今回の明示指示 → 選択したスタイル版 → チャンネル既定 → システム既定」。ただしロック、安全、権利、素材の実現可能性、出力必須条件は上書きしない。「今回採用」と「今後保存」を分離し、別チャンネルへ黙って伝播させない。横版と縦版は、同じ素材や分析を再利用しても編集案・承認・QC・版を分離する。

### 5.4 Representative Real Episode Contract

`v44-real-01`は、次に利用者が実際に作りたい動画から選び、実素材、複数の判断が分かれる場面、日本語音声、関係する固有名詞、視覚的な不確実性を含むことを目標とする。簡単に処理できるsyntheticや、transcriptのない単一clipを代表episodeとみなさない。実素材の絶対パス、素材hash、brief、GroundTruth凍結時刻、使用model/provider、出力先をrun recordへ保存する。素材が無い、GroundTruthがplaceholder、認証が未設定ならgateはblockedである。

### 5.5 Editorial Feasibility Spike（A/B/C）

実行前に`must_keep / good_optional / must_remove / uncertain`を、モデル出力を見る前に凍結する。分母、閾値、proper-noun sample、同一素材範囲、model/provider、cost/time budgetをfreezeし、後から都合よく変更しない。

| 実験 | 入力 | 判定 |
|---|---|---|
| A coarse | shot境界、sampled frames/description、transcript、basic audio、brief。Deep Reviewなし | Must-Keep/Removeとoperator continuationを記録 |
| B progressive deep | Aと同じ素材・modelに、低信頼/高影響/不一致部分のdense framesまたは短clip、local transcript、neighbor/audio contextを追加 | Aより少なくとも1つのobjective criterionまたはoperator verdictが改善し、Must-Keep recallを下げない |
| C human-rich diagnostic | A/Bが事前基準を失敗した場合のみ。凍結済みの素材、GroundTruth、対象範囲、model/provider、time/cost条件はA/Bと同一にし、Bの失敗区間へ人が補正した証拠だけを追加する | 成功ならevidence問題、失敗ならreasoning/model問題として分類 |

CはA/B成功時には実行しない。失敗時も、凍結済みの比較基準を変えずに原因に対する限定修正を一回だけ行い、その一回の再実行後にNO-GOまたは次gate進行を決める。A/B/Cを別条件で繰り返して合格を作らない。

### 5.6 Deep Reviewの証拠束

Deep Reviewは、粗いshot要約と異なるsub-spanを発見できる。証拠束は対象source span、dense framesまたは短clip、local transcriptとtimestamp、前後shot、音量/無音/環境音、brief、必要なTaste Seedを含み、各要素のprovider・取得時刻・hash・未確認範囲を参照する。synthetic frame/transcript/audio providerやdeterministic placeholderはmechanism testに限り、product gateに使わない。

### 5.7 First Previewの3値評価とInterruption Policy

最初のPreviewは次の3値で本人が評価する。分母や閾値が未確定の指標は、実行前にfreezeし、値を捏造しない。

| 値 | 意味 |
|---|---|
| `acceptable_minor` | 大きな構成変更なしに、少数の修正で採用判断を続けられる |
| `structurally_useful_major` | 構成の方向は使えるが、複数の大きな修正が必要 |
| `unusable` | 実質的に作り直し。最初のPreview受入失敗 |

割り込みは次の3分類にする。

| 分類 | 条件 | 扱い |
|---|---|---|
| `auto_continue` | safe retry、cache miss、承認済みfallback、一時的provider失敗 | 制限回数内で続行し、履歴へ記録 |
| `continue_and_report` | optional capabilityの代替、非ブロッカーの品質fallback | 作業を続け、次の判断時に影響範囲を表示 |
| `stop_and_ask` | 意味が変わる曖昧さ、権利/PII、公開、safe pathなし、taste conflict | 利用者の回答なしに進めない |

## 6. 詳細要件

### 6.0 要件→Gate→Evidence→判定の追跡

各要件は、`status`（現状分類）と`phase`（実施段階）を別に持つ。実行時は要件IDをgate checklistへ紐づけ、gateごとに必要なEvidence Ledger E-ID、run id、対象commit、pass criteria、実測値、operator判断、未確認を記録する。`target`や`implemented_unproven`の要件は、証拠が揃うまでpassと表示しない。Gateのpassは要件のすべてがpass、または適用外理由が証拠付きで記録されたときだけ発行する。

| 追跡 | 記録規則 |
|---|---|
| 要件 → Gate | 各Gate checklistに対象要件IDと、その要件の`status`/`phase`を記録する |
| Gate → Evidence | 要件ごとにEvidence Ledger E-ID、run id、対象commit、証拠path、実測値を紐づける |
| Evidence → Pass / Fail | 事前freeze済みpass criteriaとoperator判断を記録し、未確認・blocked・適用外理由なしはpassにしない |

### 6.1 INT — Intake / Orchestration

| ID | status | phase | 要件 | 理由・受入 | 根拠 / 依存 |
|---|---|---|---|---|---|
| V5-INT-001 | target | FIRST-PUBLISH | Cockpitはsource folder、episode brief、任意のreference/taste commentsを受け、実ジョブをlaunch/enqueueする。 | intake後に実素材からPREVIEW_READYまで進むライブ記録。画面値がAPI・保存・runnerへ届く。 | `PRD_v4.4.md` §§15,16; UX brief §4 / 依存: EVD-001 |
| V5-INT-002 | in_progress_wip | FIRST-PUBLISH | channel、style version、destination、今回だけの条件を既存create契約へ後方互換で通す。 | 旧folder+briefだけの作成を壊さず、保存→再起動→読出しで一致する。 | UX brief §§3,5.4 / 依存: SAF-001 |
| V5-INT-003 | target | FIRST-PUBLISH | stage表示は測定済み進捗のみ。blocked理由とoperator actionを日本語で示す。 | 未計測ETAを捏造せず、利用者が待つ/判断するを選べる。 | `cockpit/DESIGN.md`; `PRD_v4.4.md` §15 / 依存: MET-001 |

### 6.2 EVD — Evidence / Media Intelligence

| ID | status | phase | 要件 | 理由・受入 | 根拠 / 依存 |
|---|---|---|---|---|---|
| V5-EVD-001 | blocked | FIRST-PUBLISH | 実素材のsource span、shot、visual/audio context、transcript、confidence、provenanceを正準時間座標で参照できる。 | `v44-real-01`でCoverage/usefulnessを測定。CER、timestamp、欠落、重複を基準内に戻すまでV44-0をpassにしない。 | `PRD_v4.4.md` §§7,8; current ASR report / 依存: INT-001 |
| V5-EVD-002 | target | FIRST-PUBLISH | coarse evidenceとtargeted deep reviewを比較し、Deep ReviewがMust-Keep recallを下げずに1指標以上改善する。 | Progressive Attentionを追加する価値を実測する。synthetic placeholderはgateに使わない。 | `PRD_v4.4.md` §§6,8 / 依存: EVD-001, EDT-001 |
| V5-EVD-003 | implemented_unproven | FIRST-PUBLISH | 低信頼・高影響の判断はdeep/alternate/operator flagへ回し、証拠が未確認なら自動採用しない。 | 「confidenceが高い」だけで品質保証しない。 | `PRD_v4.4.md` §9 / 依存: SAF-002 |

### 6.3 EDT — Editorial Intelligence

| ID | status | phase | 要件 | 理由・受入 | 根拠 / 依存 |
|---|---|---|---|---|---|
| V5-EDT-001 | implemented_unproven | FIRST-PUBLISH | Production pathは明示したactual model/providerを使用し、heuristicはtest/diagnostic/fallbackとして明示する。 | model pin、呼出ログ、Proposal、障害時のtyped blockedを記録。heuristicをAI編集の成功と表示しない。 | `PRD_v4.4.md` §9; implementation plan §2.2 / 依存: EVD-002 |
| V5-EDT-002 | target | FIRST-PUBLISH | Story→Moment→Creativeの三段階で、情報・新規性・進行・感情・視覚・冗長性・cuttabilityを根拠付きで判断する。 | Must-Keep recall 100%、catastrophic removal 0、Must-Remove retention ≤25%、operator continuation YESをV44-0基準とする（分母・閾値は実行前freeze）。 | `PRD_v4.4.md` §§6,9 / 依存: EVD-001 |
| V5-EDT-003 | deferred | POST-PUBLISH | heavy pairwise/profile learning、広いchannel learningをFirst Publish前の経路へ入れない。 | 既存機構は削除せず凍結。実測した改善仮説ができてから別gateで評価する。 | `PRD_v4.4.md` §§10,22 / 依存: FIN-002 |

### 6.4 UX — Operator UX

| ID | status | phase | 要件 | 理由・受入 | 根拠 / 依存 |
|---|---|---|---|---|---|
| V5-UX-001 | implemented_unproven | FIRST-PUBLISH | 感想入力は命令への言い直しを要求せず、調査済み、原因仮説、提案、未確認を表示する。 | U01〜U03で位置の有無に関係なく原因調査へ進む。frontendでも同じ状態が表示される。 | UX brief §§2,5; current diff / 依存: EVD-001。2026-09-07工程1+2でU01〜U03経路を実装、ダミーエピソードで実LLM仮説表示を確認（実素材未確認） |
| V5-UX-002 | implemented_unproven | FIRST-PUBLISH | 「B」「両方違う」「違いが分からない」「任せる」「前に戻す」を理由なしでも処理する。 | 理由の捏造やループを防ぎ、採用/却下/復帰を明示的に履歴化する。 | UX brief §§2,3 / 依存: REV-002。2026-09-07工程2: B選択・両方違う・理由任意を理由の捏造なく実装（任せる/違いが分からないは未実装で残置） |
| V5-UX-003 | in_progress_wip | FIRST-PUBLISH | Cockpit UIはDESIGN.mdの平面・1px border・720px single column・日本語ラベル・ETA非表示規約を守る。 | UI改修で別のデザイン基盤を増やさない（YAGNI/KISS）。表示、主要操作、狭い画面、console errorを確認する。 | `cockpit/DESIGN.md`; UX brief §10 / 依存: INT-003 |

### 6.5 REV — Review / Proposal / Version

| ID | status | phase | 要件 | 理由・受入 | 根拠 / 依存 |
|---|---|---|---|---|---|
| V5-REV-001 | in_progress_wip | FIRST-PUBLISH | 試作品は採用版から派生し、元版・Proposal・設定・生成状態を持つ。採用前に納品Timelineやpublishabilityを更新しない。 | restart後も未採用試作と採用版を区別できる。 | UX brief §5.3 / 依存: SAF-001 |
| V5-REV-002 | implemented_unproven | FIRST-PUBLISH | revert/restoreは過去版を破壊せず、operator decisionとして新しいplan version/eventを作る。 | `plan_restored`の版連鎖・payload hash・reducerを検証し、二重適用を防ぐ。 | current diff `review_command/*`; UX brief §5.3 / 依存: SAF-002。2026-09-07工程1で実装・codex再レビュー合格（実素材未確認） |
| V5-REV-003 | target | FIRST-PUBLISH | 構造化interpretationは既存validator/lock/conflictに通し、曖昧なtargetだけ確認する。 | 技術IDを利用者へ要求せず、悪い対象へ書き込まない。 | `PRD_v4.4.md` §14; UX brief §5 / 依存: EDT-002 |

### 6.6 STY — Taste / Style / Channel

| ID | status | phase | 要件 | 理由・受入 | 根拠 / 依存 |
|---|---|---|---|---|---|
| V5-STY-001 | implemented_unproven | FIRST-PUBLISH | Taste Seed v0はreference(+range)+自然コメント+明示domain/polarityをruntime projectionする。 | 色の好みを pacingや字幕へ黙って広げない。入力・domain・影響範囲をProductProofReportへ記録。 | `PRD_v4.4.md` §10; `style-vocabulary.md` / 依存: EDT-001 |
| V5-STY-002 | verified_current（要素限定） | FIRST-PUBLISH | 2階層テロップ、字幕Hiragino Sans W5/影、語中分割回避、scale-to-fitは承認済みサンプルを再利用する。 | 「承認」はその要素を公開可能と見た事実であり、理想の全体スタイルとは表示しない。 | `style-vocabulary.md` A; implementation plan progress / 依存: FMT-001 |
| V5-STY-003 | target | POST-PUBLISH | 宋世羅風の複合演出、TikTok縦型、RPG/Tracker地点表示は、素材・具体値・利用者判定を揃えた機能ごとに実証する。 | 目標を実装済みと誤表示しない。縦型はstyleでなく出力形式として独立管理する。 | `style-vocabulary.md` B/C; `capability-map-social.md` / 依存: FMT-002 |

### 6.7 FMT — Output Formats / Production Kit

ここでいうFMTは`Output Format`を指し、Finishing（Resolveでの仕上げ/QC）とは分離する。横（landscape）と縦（portrait）は別outputとして扱い、同じ素材・分析を再利用しても、編集案、plan/version、承認、QC、time coordinate、render設定を共有しない。

| ID | status | phase | 要件 | 理由・受入 | 根拠 / 依存 |
|---|---|---|---|---|---|
| V5-FMT-001 | target | FIRST-PUBLISH | `Output Format`ごとに、横/縦の寸法・aspect・delivery preset、editorial plan、Timeline IR、plan version、承認、QC、正準時間座標、render設定を独立させる。 | 横版の承認や修正を縦版へ黙って伝播させない。縦型はスタイルでなくoutput形式である。 | UX brief §7; `style-vocabulary.md` B②; `capability-map-social.md` vertical block / 依存: INT-002 |
| V5-FMT-002 | implemented_unproven | FIRST-PUBLISH | Production Kit candidateは実素材でA/B/neither/currentを比較し、recipe/versionを保存する。 | synthetic cardだけで受入しない。bootstrap時間を通常AHTと分ける。 | `PRD_v4.4.md` §11 / 依存: STY-002 |
| V5-FMT-003 | deferred | POST-PUBLISH | 未使用のtransition/effect/genre breadthを先に増やさない。 | V4.4のFirst Publish前凍結を維持（YAGNI）。 | `operator-wishlist.md`; `PRD_v4.4.md` §26 / 依存: FIN-002 |

### 6.8 RSL — Review Loop / Natural Language

| ID | status | phase | 要件 | 理由・受入 | 根拠 / 依存 |
|---|---|---|---|---|---|
| V5-RSL-001 | in_progress_wip | FIRST-PUBLISH | 感想は nearby transcript、shot、映像/音の確認へ回し、仮説を事実から分離してstructured proposalを作る。 | U01「ここ退屈」は位置があっても原因確認を省略しない。 | UX brief §§2,5; current diff / 依存: EVD-001 |
| V5-RSL-002 | implemented_unproven | FIRST-PUBLISH | 最低限、区間削除、長く残す、別テイク、B-roll、字幕、効果、BGM、色合わせ、episode-only/channel-levelを解釈できる。 | relevantな実素材で少なくとも1件を実行し、partial rebuild→新Previewを確認。bounded parser単体は合格としない。 | `PRD_v4.4.md` §14 / 依存: REV-003 |
| V5-RSL-003 | implemented_unproven | FIRST-PUBLISH | 原因が明白なら一案、分岐する場合最大二案。反応が「両方違う」なら仮説を更新し、同じ案を繰り返さない。 | 予算・時間・費用を記録し、試作乱立を防ぐ。2026-09-07工程2: 最大2案の契約上限・両方違う時の同一案再提示禁止を実装（予算・時間・費用の記録は未実装で残置） | UX brief §3.3 / 依存: MET-002 |

### 6.9 FIN — Resolve Finishing / Preview / QC

| ID | status | phase | 要件 | 理由・受入 | 根拠 / 依存 |
|---|---|---|---|---|---|
| V5-FIN-001 | implemented_unproven | FIRST-PUBLISH | Timeline IRを共通入力にPreviewとResolveへ分岐し、source-record span、audio twin、media online、frame rateを検証する。 | 実Resolve開閉・readback・renderを含むProductProofReportを残す。 | `AGENTS.md`; `mcp-fit.json`; implementation plan progress / 依存: FMT-001 |
| V5-FIN-002 | blocked | FIRST-PUBLISH | Native subtitle SetPropertyは使用禁止。字幕はsafe routeをlive handshake後に選び、**意味訳ではなく発話どおり**の日本語を、実音声を聞いて確認する。未聴取・未確認なら`provisional/未確認`と表示し、製品合格にしない。 | 21.0.4.5で3/3 crash。読取成功やsubtitle probe acceptedを製品合格と解釈しない。CER/固有名詞とcue・segmentation・line-break・duration・legibilityを分離する。 | `~/.metacua/DAVINCI_KNOWLEDGE.md` S09; `mcp-fit.json`; `PRD_v4.4.md` §§7,13 / 依存: EVD-001 |
| V5-FIN-003 | implemented_unproven | FIRST-PUBLISH | FusionやMotionはreadbackだけで合格にせずrender A/Bで映像差分を確認する。 | Transformはreadbackできても映像不変、Blurはrender失敗実測。 | `DAVINCI_KNOWLEDGE.md` F02/F04 / 依存: SAF-003 |
| V5-FIN-004 | target | FIRST-PUBLISH | 7品質領域のapplicability recordは、domain、status、reason、evidence、owner、done criterionを持つ。`manual_fallback_required`は人が実行すれば完了可能な状態、`blocked`は安全な完了経路がなく進行停止する状態と区別する。 | 全領域を考慮し、未実施理由と責任者を隠さない。publishabilityとは別に判定する。 | `PRD_v4.4.md` §12; `cockpit/DESIGN.md` finishing chips / 依存: FMT-001 |

### 6.10 SAF — Safety / Rights / Approval

| ID | status | phase | 要件 | 理由・受入 | 根拠 / 依存 |
|---|---|---|---|---|---|
| V5-SAF-001 | implemented_unproven | FIRST-PUBLISH | source/transcript/OCRはuntrusted dataとして扱い、任意Shell/File Write/Networkをcreative modelへ与えない。U15ではprompt injection、外部送信、権利、PII（個人情報）の扱いを実データ境界で実証する。 | 素材内の「指示を無視せよ」を命令として実行せず、権利・個人情報・network境界を記録する。規約の存在だけでは製品安全passにしない。 | `AGENTS.md` §8; `PRD_v4.4.md` §23; UX brief U15 / 依存: OPS-003 |
| V5-SAF-002 | implemented_unproven | FIRST-PUBLISH | 受入済みdecisionだけをvalidation後に決定論的実行し、Resolve/Job StateのWriterを一つにする。lock・手作業・automation_frozenを尊重する。 | 二重送信、古い試作適用、半端な採用を防止。U09/U10/U11/U17を通す。 | `PRD_v4.4.md` §§2,23; current diff / 依存: REV-002。2026-09-07工程1: 保存済み提案のみ適用（stale/consumed/mismatch/not-found拒否）・半端採用防止（復帰の正確な状態）を実装、codex再レビュー合格（実素材未確認） |
| V5-SAF-003 | target | FIRST-PUBLISH | operatorのpublishability（publishable / publishable_after_fixes / not_publishable）とpublication approvalを分離し、AIはadvisoryに留める。 | public uploadは明示承認なしに実行しない。 | `PRD_v4.4.md` §§17,18 / 依存: FIN-001 |

### 6.11 NFR — Non-functional Requirements

| ID | status | phase | 要件 | 理由・受入 | 根拠 / 依存 |
|---|---|---|---|---|---|
| V5-NFR-001 | target | FIRST-PUBLISH | 失敗はevidence / editorial / planning-execution / taste / UXへ分類し、原因を隠した成功状態を返さない。 | ブロッカーごとに一つの修正と一回の再実行を基本とし、無制限調整を防ぐ。 | `PRD_v4.4.md` §21 / 依存: MET-003 |
| V5-NFR-002 | target | STEADY-STATE | 再起動後に採用版、試作品、未回答、失敗理由、進捗が復元される。 | 人間セッションを増やさず、同一イベントを二重適用しない。 | UX brief §5.3 / 依存: REV-001 |
| V5-NFR-003 | target | STEADY-STATE | 通常運用のCLI、JSON確認、直接Resolve編集は0を目標とし、実際のfallback時は利用者に影響範囲を表示する。First Publishでは暫定的な手動Resolveを許容するが、手動範囲・理由・時間を記録する。 | 30分目標の前提となる定常UX SLO。First Publishの暫定手動とsteady-stateの0を混同しない。 | `PRD_v4.4.md` §§1,15,19; implementation plan §§8,9 / 依存: INT-003 |
| V5-NFR-004 | target | FIRST-PUBLISH | Computer Use traceはgoal/text、対象版、実行時刻、画面証拠、結果を保持し、retention、redaction、delete policyを定める。 | 機密画面・発話・個人情報を無期限に残さず、再現性と最小保管を両立する。 | `~/.metacua/AGENTS.md`; UX brief §8 / 依存: SAF-001 |

### 6.12 MET — Metrics / Observability

| ID | status | phase | 要件 | 理由・受入 | 根拠 / 依存 |
|---|---|---|---|---|---|
| V5-MET-001 | target | FIRST-PUBLISH | TTFRP、AHT、壁時計、試作数、却下数、修正数、partial/full rebuild、blocking sessionsを同一runで記録する。 | 761分/490秒のような現実のボトルネックを隠さず工程別に比較する。 | `PRD_v4.4.md` §19 / 依存: INT-003 |
| V5-MET-002 | target | FIRST-PUBLISH | Bootstrap AHTはordinary review、kit bootstrap、taste calibration、troubleshooting、direct Resolveの5区分で記録し、direct Resolveは合計へ二重加算しない。 | 10分のdirect_resolve記録は代表性・スキル天井の注記付きで、30分達成とは扱わない。 | implementation plan progress 2026-09-03 / 依存: FIN-001 |
| V5-MET-003 | target | STEADY-STATE | 製品品質はMust-Keep recall/Remove retention、first preview判断、operator YES、publishabilityと結びつける。 | 速度だけで「良い編集」と判定しない。 | `PRD_v4.4.md` §§6,19 / 依存: EDT-002 |

### 6.13 OPS — Operations / Toolchain

| ID | status | phase | 要件 | 理由・受入 | 根拠 / 依存 |
|---|---|---|---|---|---|
| V5-OPS-001 | implemented_unproven | FIRST-PUBLISH | 実行前にlive capability handshakeを行い、stored capability indexは候補 routingとしてだけ使う。 | 保存済みfitは確認済みだが全runでのhandshake実施は製品受入未済。能力索引は2.207.0基準であり、現行MCP 2.210.0の証明ではない。 | `~/.metacua/AGENTS.md`; `DAVINCI_CAPABILITY_INDEX.md`; `mcp-fit.json` |
| V5-OPS-002 | target | FIRST-PUBLISH | routingは機能ごとの実測とfallback条件で選び、構造化・決定論的な経路をComputer Useより優先する。 | MCPは能力提供者で編集判断者ではない。GUIは不足面だけに限定する。 | `DAVINCI_CAPABILITY_INDEX.md`; `DAVINCI_KNOWLEDGE.md` |
| V5-OPS-003 | target | FIRST-PUBLISH | MetacuaはGUI実行層として、操作前のUI固定、座標内側補正、IME切替往復、操作後の画面/読み戻しを必須とする。 | foreground干渉、通知、座標依存、クラッシュ後待機を前提にする。 | `~/.metacua/AGENTS.md`; `DAVINCI_KNOWLEDGE.md` |

## 7. ツールルーティングと完了証拠

### 7.1 ルーティング原則

経路は固定の直列順位で決めない。機能ごとに、構造化・決定論的な経路を優先候補として比較し、能力の実測と安全性・再現性で選ぶ。

| 経路 | 使う場面 | 完了証拠 | 切替条件 |
|---|---|---|---|
| Live MCP | media query、import、source-range placement、readback、render lifecycleなどacceptedかつ実素材で適合するもの | provider/version、request、成功応答、readback、必要なrender | live handshake失敗、capability failed、実素材で映像不一致なら下段へ |
| AdvancedMCP / script | 公式・検証済みの高度操作、直接値設定、構造化実行 | script version、入力、戻り値、UI/readback、再現手順 | APIが書けない/不安定ならInterchange/CUへ |
| Interchange | `.drt/.drx`など、直接書込より再現可能な受け渡し | 生成物hash、Resolve import、media online、render/readback | 形式が表現できない、受渡し後に不一致ならCU/HITL |
| Fusion | Text+/composite/motionなど、映像差分をrender A/Bできるもの | comp、設定、前後render、pixel/visual確認 | readbackのみ、render失敗、MediaOut不備ならfallback |
| Computer Use | MCP/API/Interchangeで届かない、GUIで到達できる機能 | 操作前後screen、操作ログ、読み戻し、必要ならrender | foreground占有、権限/機密、再現性不足ならHITL |
| HITL | 意味、権利、公開、未解決の好み、手動しか安全でない仕上げ | 利用者の判断、対象版、時刻、コメント、承認 | 明示承認なしに公開・不可逆操作を進めない |

### 7.2 機能ごとの route record

各実行は次のroute recordを既存のProof/ledgerから参照可能にする。`target`（何を達成するか）、`chosen_route`、`capability/version`、選ばなかった経路の`refusal_reason`、`fallback`、`timeout/retry`、`before/after/readback/render evidence`を持つ。能力索引の記載だけでchosen routeを確定しない。

```text
route_record = {
  target, chosen_route, capability, provider_version, resolve_version,
  refusal_reason[], fallback, timeout, retry_policy,
  before_evidence[], after_evidence[], readback_evidence[], render_evidence[]
}
```

MCPで構造化できるimport、range、readback、renderはまず検討する。APIで届かない数値・GUIのみの機能はInterchange/Fusion/Computer Useへ切り替えられるが、CUは操作前後画面、座標補正、IME、読み戻しまたはrenderを残す。どの経路でも「呼べた」と「映像に反映された」を分ける。

### 7.3 known failureの扱い

transition-path、audio-property-operation、bgm-track-ducking、edit-engine-selects、alternate-shot-similarityはfailedとして残す。失敗を隠してacceptedへ変更しない。字幕SetPropertyの3/3 crash、Fusion readback/render乖離、IME/座標制約も常設ハザードとする。代替経路が製品受入されたときだけ、元の失敗証拠を保持したままrouting台帳を更新する。

## 8. 状態・版・採用・復帰・単一Writer・安全設計

### 8.1 版のライフサイクル

```text
採用版 vN
  → 感想/具体命令を受信
  → 観測・原因仮説・Proposal
  → 未採用試作品 vN-candidate-A/B
  → 利用者判断（採用/却下/両方違う/任せる）
  → validation + lock + capability check
  → 採用版 vN+1
  → 必要範囲だけpartial rebuild
  → preview / QC
```

復帰は「vNへDBを巻き戻す」操作ではなく、過去内容をコピーした新しい`plan_restored`版である。復帰元、元版、operator actor、payload hash、結果版を記録する。進行中に採用版が変わった場合は旧候補を自動適用せず、競合として再確認する。

### 8.2 単一Writerと安全境界

Resolve ProjectとJob Stateは単一Writerが直列に更新する。複数AIは証拠生成・提案生成を分担できるが、最終編集意図を同時に確定しない。LLM、素材Transcript、OCR、参考動画の文章に書込権限を与えない。権利・個人情報・Cloud送信種別・model/provider/template versionをrun ledgerに記録する。

## 9. 受入試験 U01–U17

下表はV4.4から引き継ぐ検証シナリオであり、V5.0では`contract`（入力・保存・状態）、`frontend`（画面と主要操作）、`live-product`（実素材・実機・本人）の3層を分ける。各実行はrun id、対象commit、証拠path、pass criteria、未確認事項を記録する。自動試験に通っても面白さ・公開可否を証明したことにはしない。

| ID | シナリオ | contract | frontend | live-product |
|---|---|---|---|---|
| U01 | 「ここ退屈」+再生位置 | feelings→hypothesis/event | 調査中・根拠・提案を表示 | 音/映像/周辺確認、実試作。run/evidence/passを記録 |
| U02 | 「全体が素人っぽい」+位置なし | 全体scopeを保存 | 秒数強制なし | 全体/代表区間の映像確認と仮説 |
| U03 | 「映画っぽく」+未指定 | 未指定domainを維持 | 固定変換を表示しない | 少数比較または未確認を本人表示 |
| U04 | 「Bが好き」+理由空欄 | reason optional、choice event | 理由なし選択可 | 採用と他domain伝播なしを本人確認 |
| U05 | 「両方違う」 | candidateを未採用のまま保持 | 再調査導線 | 前案を採用せず新仮説を実映像で確認 |
| U06 | 「任せる」 | budget/approval scopeを保持 | 上限を表示 | bounded試作のみ、公開/保存なし |
| U07 | 「今回は静かに」 | episode-only scope | 適用範囲を表示 | channel既定不変を再読出し |
| U08 | style保存後再起動 | version/channel isolation | 復元表示 | 別channel混入なしを再起動で確認 |
| U09 | 試作中に採用版変更 | conflict event | 古い案をdisabled表示 | 旧案が適用されないことをreadback |
| U10 | 採用二重送信/再接続 | idempotency invariant | 一件表示 | 二重編集・二重依頼なしをrunログで確認 |
| U11 | 複数修正の一部実行不能 | atomic commit / blocked | 残課題・代案を表示 | 半端な採用なし、版hashを確認 |
| U12 | 音が原因の不満 | audio evidence ref | 音付きpreview | 実音声で確認、静止画完了不可 |
| U13 | 同一素材の横版/縦版 | output別plan/version | 出力を分けて表示 | 独立承認/QC/time coordinate |
| U14 | 画像生成一部失敗 | partial result event | 成功/不足を表示 | 不足分だけ再試行、編集判断権限なし |
| U15 | 素材内の命令文 | untrusted-data contract | 外部送信/権利/PII状態を表示 | injection無実行、送信・権利境界の実証 |
| U16 | 非対応演出/素材不足 | refusal/fallback reason | 代案と未確認を表示 | 未確認能力を約束しない |
| U17 | lock/automation_frozen | lock invariant | 保護状態表示 | candidateと納品を混同せず、書込拒否を確認 |

Uのpassは、各層の証拠が同じrun idへ結びつき、三層すべてを要求するシナリオではcontract/frontend/live-productが揃った場合だけとする。live未実施はpassではなく未確認である。

## 10. 指標

| 指標 | 定義 | V5.0の扱い |
|---|---|---|
| TTFRP | intakeから最初に利用者が判断できるPreviewまでの壁時計 | 現行観測は約761.38分。run別観測を保持し、代表値の選定規則と対象条件を実行前にfreezeする。未計測ETAを表示しない |
| AHT | 利用者が入力・視聴・比較・判断・手動操作に使った時間。機械待ちと分離 | bootstrap/steady-stateを混ぜない。定常中央値≤30分は複数実エピソードで評価 |
| Must-Keep recall | 事前凍結したmust_keepが候補へ残る割合 | 目標100%。分母、anchor範囲、escalationの扱いは実行前freeze。欠落はoperator escalationでも記録 |
| Catastrophic removal | Must-Keepを確認なしに削除した数 | 0 |
| Must-Remove retention | must_removeがkeep推奨に残る割合 | 目標≤25%。分母と判定規則は実行前freeze |
| ASR | CER、proper noun exact match、timestamp p95、欠落、重複 | systemとoperator-corrected diagnosticを分ける。proper nounの分母、timestamp許容、Deep Review coverage/costの閾値は実行前freeze。現状systemはblocked |
| Review効率 | 修正件数、試作数、却下数、partial/full rebuild、blocking sessions | U01–U17と同一runへ紐づける |
| Publishability | operatorの三値判断 | AI scoreで代用しない |

First Previewの3値は`acceptable_minor`、`structurally_useful_major`、`unusable`で記録する。Must-Keep/Removeの分母、proper noun、Deep Review coverage/cost、品質の細かな閾値がrun開始時に未定なら、未定のまま合格値を作らず、実行前のfreeze作業を先に行う。steady-stateの「定着」は、代表的な実エピソード3本連続で、AHT中央値≤30分、blocking sessions≤2、first previewが`unusable`でないことを確認して決定する。一回の単純な10分記録やbootstrap成功では決定しない。

計測境界はrun開始前に固定する。TTFRPはCockpitがintakeを受理してjobをenqueueした時点から、利用者が再生して判断できる最初のPreviewが利用可能になった時点までの壁時計で、機械待ちとretryも含む。AHTは利用者が入力・視聴・比較・判断・手動Resolveに能動的に使った時間だけを区間計測し、機械待ちは別記録にする。blocking sessionは、初回intake後にシステムが利用者の回答なしでは進めない連続した停止区間を一回と数え、同時に解ける質問は束ねる。safe retry、進捗確認、`continue_and_report`はblocking sessionに数えない。欠損・中断・時計不整合があるrunは推定値で補完せず、指標ごとに`invalid/unknown`とする。

## 11. 証拠ゲート型ロードマップ

### 11.0 Gate判定表

| Gate | 主な要件ID | 必須Evidence | Pass / Fail |
|---|---|---|---|
| V50-0（Editorial Feasibility） | EVD-001/002、EDT-001/002 | GroundTruth、同一条件A/B、必要時のみC、ASR、Deep Review coverage/cost、operator continuation | freeze済み基準を満たせばpass。未達はNO-GO/blocked |
| V50-1（Real Preview/Review） | INT-001/003、UX-001、RSL-001/002、REV-001/003 | source-folder起点のrun、Preview、感想原文、仮説、実試作、採用/却下/復帰、partial rebuild、TTFRP | frontend/live-productを含む証拠が揃えばpass。current_stageだけではpass不可 |
| V50-2（First Publishable） | FMT-001/002、FIN-001/002/004、SAF-003、MET-001/002 | 7領域record、subtitle音声確認、Resolve/render/QC、編集可能納品、publishability、AHT | operatorが`publishable`、blockなし、証拠が揃えばpass |
| V50-3（Publication） | SAF-003、NFR-004 | publication approval、対象hash/destination/visibility/metadata、remote ack、idempotency | remote ack付きupload/scheduleのみpass |
| V50-4（Steady State） | NFR-002/003、MET-003 | 代表実episode 3本連続のAHT、blocking sessions、first preview値、fallback | 3本連続で条件を満たしたときに定着と判断 |
| V50-5（Learning） | EDT-003、FMT-003 | 改善仮説、比較run、rollback | V50-2〜4後のみ判断。証拠なしはdeferred |

### 11.1 役割と期限の扱い

| 役割 | 主責任 | 完了を署名する対象 |
|---|---|---|
| Product/Operator owner | 代表episode選定、GroundTruth凍結、Preview/採用/公開の判断 | `EditorialGroundTruthV1`、operator verdict、approval event |
| Evidence/Editorial owner | ASR、Deep Review、A/B/C、Editorial判定、分母・閾値のfreeze | `ProductProofReportV1`のevidence/quality節 |
| UX/Frontend owner | Intake、感想、仮説、候補比較、復帰、3分類の割り込み表示 | contract/frontend層のrun evidence |
| Pipeline/Resolve owner | Route Record、Timeline IR、partial rebuild、7 domain、Resolve readback/render | `ProductProofReportV1`のbuild/QC節 |
| Safety/Publication owner | prompt injection、外部送信、権利/PII、承認、upload/scheduleのremote ack | safety ledger、publication approval/result |

各Phaseの期限はカレンダー日を捏造せず、前Phaseのexit criteriaが満たされた時点を次の開始条件とする。担当が未確定の場合は`owner=TBD`、期限は`TBD（evidence gate待ち）`と記録する。

### Phase 0: Rebaseline / ledger

V4.4、実行計画、UX指示書、style/wishlist/capability map、Cockpit design、Metacua知識、Git差分を正本として台帳化する。改修指示書の適用/未適用/対象commit/期限を別列で管理する。第三の新規authoritative artifactをFirst Publish前に追加しない。

Exit criteria: 入力資料、対象commit、改修指示書の適用状態、owner、未確認事項がEvidence Ledgerへ登録され、未確認を空欄で残していない。

### Phase 1: V50-0 Evidence and editorial feasibility

`v44-real-01`の素材存在、GroundTruth凍結日、proper nounを含む日本語サンプル、actual model/provider、A/B/C条件を固定する。A/Bは同一素材・同一条件で比較し、CはA/B失敗時のみ診断用に実行する。system ASRのblockedを解消し、coarse vs progressive deep review、operator continuation、evidence usefulnessをProductProofReportへ集約する。失敗時は原因をevidence / reasoningに分類し、1回の限定修正と再実行でNO-GOまたは次gateを決める。

Exit criteria: GroundTruth、分母/閾値、model/provider、A/B条件がfreezeされ、A/B（必要時のみC）の結果、Deep Review evidence bundle、NO-GOまたはpass理由が`ProductProofReportV1`へ揃う。

### Phase 2: V50-1 Real Cockpit review loop

source-folder起点の実パイプラインを通し、frontendの追加入力、hypothesis/investigated表示、原因仮説、自然言語解釈、確認、試作、採用/却下/復帰、partial rebuildまで接続する。現在の観測JSONは`/Users/stc/Developer/davinci-agent/private/reference-episodes/v44-real-01/runs/observation*.json`で参照するが、`current_stage=intake`の記録を「現在ライブ中」と読み替えない。TTFRP約761.38分とrebuild 115.4秒/490.339秒は別runの観測値として保持し、対象変更・実行条件・変更範囲が揃ったものだけを代表値に選ぶ。製品UXの改善を実素材で判定する。

Exit criteria: U01–U11、U13、U17の必要3層証拠が同一run idへ揃い、First Previewの3値、採用/却下/復帰、競合・クラッシュ再開、partial rebuildのreadbackが確認される。

### Phase 3: V50-2 Finishing and publishability

必要なProduction Kitだけを実素材でA/Bし、7領域を適用可否込みで評価する。字幕は実音声・固有名詞・cue/segmentation/line-break/duration/legibilityを確認する。live Resolveまたは明示承認済みfallbackで編集可能な納品物を作り、operatorがpublishableを判断する。

Exit criteria: 7領域のapplicability record、字幕の音声確認、Resolve/readback/renderまたは承認済みfallback、technical/editorial QC、operator publishabilityが揃う。未確認やblockedはpassへ数えない。

### Phase 4: V50-3 Publication proof

accepted final renderからpublish packageを作り、`PUBLICATION_APPROVED`相当の明示承認を得て、まずはprivate/unlistedを含むidempotent upload/scheduleを一回証明する。

Exit criteria: final artifact hashと送信hashが一致し、destination、visibility、metadata、actor、time、idempotency、remote ackを含む承認・結果記録が揃う。失敗・不明ackは`PUBLISHED`/`SCHEDULED`へ進めない。

### Phase 5: V50-4 Steady state / V50-5 Learning

次の実エピソード複数本でAHT中央値≤30分、blocking sessions≤2、first previewがunusableでないことを確認する。その後にのみpairwise/profile/channel learningを、効果仮説・比較条件・rollback付きで活性化する。

Exit criteria: 代表的な実episode 3本連続でsteady-state基準を満たし、First Publishの暫定手動Resolveを含めず、ProductProofReportV1へrun別のAHT・blocking・Preview判定を記録する。未達ならlearningへ進めず、原因と次のownerをTBDとして残す。

## 12. 非目標、保留、既知リスク、未解決事項

### 12.1 非目標・保留

- First Publish前の全ジャンル対応、Resolve全機能対応、広いProduction Kit catalog。
- heavy taste learning、audience outcome最適化、自律チャネル戦略、自動公開。
- 縦型の全連鎖を一括完成扱いすること。縦型は別出力として実需要順に検証する。
- 実素材で需要が出ていないエコー演出、背景差替え、Tracker地点表示、冒頭10秒まとめを先回り実装すること。
- 画像生成の成功を動画のテンポ・音・面白さの証拠とすること。

### 12.2 既知リスク

- ASRの欠落・重複・時間ずれがEditorial判断を歪める。
- MCP acceptedでも、字幕・音・transition・selects・similarityは5失敗面を含む。
- Native subtitle SetPropertyはResolveをcrashさせる。
- Fusionはreadbackとrenderが乖離する。
- Computer Useはforegroundを占有し、他プロセス、通知、IME、座標、再起動直後の待機に影響される。
- 現在のUX差分はfrontend未接続で、API/model/testの存在だけでは製品受入にならない。
- `style-vocabulary.md`は承認済み要素と理想目標を分けている。欠落した過去の参照分析を現在の証拠として再利用しない。

### 12.3 未解決事項

- `v44-real-01`の実素材配置、GroundTruth、transcript sample、actual production modelの現行参照場所と凍結日。
- TTFRP 761分の内訳と、約490秒partial rebuildのどの工程が支配的か。
- frontendでintake追加入力、Review TS型、原因仮説表示、revert buttonをいつ接続するか。
- 縦型を主出力にするか、横型と両立するか。
- 宋世羅相当のエコー/背景処理の具体値と、次回素材での「タメ」検出の正解。
- 5 failed MCP面の代替経路を実素材で受入できるか。

## 13. 根拠資料一覧

- `docs/prd/PRD_v4.4.md` — V4.4製品仕様、ゲート、受入、凍結規律。
- `docs/prd/implementation-plan-v4.4.md` — 実行契約、リポジトリ監査、進捗記録、実装完了条件。
- `docs/prd/ux-redesign-implementation-brief.md` — 感想から原因調査・試作・採用・復帰へつなぐUX改修指示書。作成時点では未実装/未検証の設計を含む。
- `docs/style-vocabulary.md` — sudaさんが実際に承認した要素と目標/未検証のスタイル語彙。
- `docs/operator-wishlist.md` — 検出済みの未達要望。網羅的一覧ではない。
- `docs/capability-map-social.md` — TikTok/Reels/Shorts・vlog領域の能力地図。状態語彙と根拠pathを保持。
- `cockpit/DESIGN.md` — CockpitのUI token、レイアウト、アクセシビリティ、finishing chip。
- `video-pipeline/capabilities/v4.4/README.md` — V44 gate状態、runbook、ProductProofReportの扱い。
- `video-pipeline/capabilities/v4.4/mcp-fit.json` — MCP v2.210.0 / Resolve 21.0.4.5の22 probeのaccepted/failed台帳。
- `~/.metacua/AGENTS.md` — Computer UseとResolveの安全・確認規則。
- `~/.metacua/DAVINCI_CAPABILITY_INDEX.md` — Live MCP/Fusion/AdvancedMCP/Interchange/CU/HITLのrouting索引。保存時点の候補でありlive証明ではない。
- `~/.metacua/DAVINCI_KNOWLEDGE.md` — 実機で確認したResolve仕様、API制約、Fusion/CU/IMEの既知リスク。
- `git status --short --branch`, `git diff` — HEAD、UX工程1 WIPの現行作業木。秘密値・実素材本文は記載しない。

## 14. Evidence Ledger

| E-ID | 事実/観測 | 種別 | 状態 | 製品判断 | 参照 |
|---|---|---|---|---|---|
| E-001 | HEADは`59e1276`、MCP v2.210.0 pin、22 probe中17 accepted/5 failed | 実ファイル/履歴 | verified_current | routingの初期条件 | git log、`mcp-fit.json` |
| E-002 | 5 failedはtransition-path、audio-property-operation、bgm-track-ducking、edit-engine-selects、alternate-shot-similarity | 実ファイル | verified_current | native経路を合格扱いしない | `mcp-fit.json` |
| E-003 | V44-1実素材経路でCockpit intake→preview→NL修正→partial rebuildの機構証明。観測JSONは`/Users/stc/Developer/davinci-agent/private/reference-episodes/v44-real-01/runs/observation*.json`。`current_stage=intake`であり現在ライブ中ではない。TTFRP約761.38分、rebuild 115.4秒/490.339秒は複数run値 | 実素材run | verified_current / product未達 | 代表値は条件freeze後に選び、UX改善baselineへ | `private/reference-episodes/v44-real-01/runs/observation*.json` |
| E-004 | r4 77/77はoperator-corrected diagnostic | 実素材診断 | implemented_unproven | system ASR proofへ置換不可 | PRD §6.7、ProductProof |
| E-005 | system CER 0.1037、timestamp p95 5740ms、欠落31、重複37 | 実素材計測 | blocked | V44-0を止める | ASR report / policy v2 |
| E-006 | 2026-09-07現レビュー時点のWIP再確認。UX工程1のfeelings/hypothesis/investigated、restoreイベント。fact reviewer focused 3モジュールは99 passed、`episode_cockpit`全体は228 passed。対象範囲・実行時点をtest snapshotへ記録する | Git差分/テスト | in_progress_wip | frontend/E2E未接続。機構テストのpassを製品passへ読み替えない | current diff、tests、2026-09-07 current test snapshot |
| E-007 | Intake追加入力未送信、Review TS型/原因表示/revert未接続 | コード監査 | blocked | APIだけの完成主張を禁止 | UX brief §4、frontend audit |
| E-008 | 2階層テロップ、字幕style、scale-to-fitは実ユーザー判定と適用記録あり | 実機/ユーザー | verified_current（要素限定） | sample再利用可。理想全体ではない | style vocabulary、progress record |
| E-009 | 現行Metacuaはstate `/Users/stc/.metacua`、wrapper `/Users/stc/bin/metacua-go`、max steps 400、wrapper Bash ON、Muse Spark 1.3 Contributor。旧`/Users/stc/.meta-cue`/40/Bash OFFは歴史値 | 運用設定/知識/trace | verified_current（基盤）+ historical（旧） | max400の根拠はAGENTSではなくwrapper、`/Users/stc/Developer/meta-model-cookbook/03_use_cases/13_macos_cua/python/metacua/agent.py`、traceの突合。DaVinci全E2Eの証明ではない | `/Users/stc/bin/metacua-go`, `/Users/stc/Developer/meta-model-cookbook/03_use_cases/13_macos_cua/python/metacua/agent.py`, `/Users/stc/.metacua/traces/*.jsonl`, `/Users/stc/.metacua/AGENTS.md` |
| E-010 | Native subtitle SetPropertyが3/3でhang→crash | 実機反復 | blocked | 書込経路から除外 | `DAVINCI_KNOWLEDGE.md` S09 |
| E-011 | Fusion readbackだけでは映像反映を証明できずrender A/B必須 | 実機反復 | verified_current（制約） | renderなしの受入禁止 | `DAVINCI_KNOWLEDGE.md` F02/F04 |
| E-012 | 参照スタイルの宋世羅/TikTok/地点表示は目標・未検証。検出済みwishlistも網羅的でない | 文書/ユーザー要望 | target / deferred | 実素材の需要順に一件ずつ | style vocabulary、wishlist、capability map |
| E-013 | 歴史資料にはV44 gate BLOCKEDと未証明記載があるが、現行V44-1機構証明/WIPも存在 | 複数資料の整合 | historical + verified_current | 履歴を改変せず状態台帳で併記 | V4.4、README、現行run/diff |
| E-014 | finishing-runの旧provider 2.98.3 / Resolve 21.0.4 rejectは歴史証拠。現行MCP 2.210.0 / Resolve 21.0.4.5の成功証拠へ読み替えない | historical run | historical | 現行のlive handshakeと別に保持 | `capabilities/v4.3/**`、旧finishing run記録 |

### 14.1 Ledger更新規則

新しい実測ごとに、`E-ID`、実行日、対象commit、provider/model pin、入力artifact hash、観測点、成功/失敗、operator判断、次の状態を追記する。未確認を空欄で埋めず、`unknown`または`blocked`と明記する。歴史的証拠を現行成功へ書き換えない。

## 15. V5.0 Definition of Done

V5.0は次の往復を、代表実素材・actual model・liveまたは明示承認fallback・利用者判断・証拠台帳付きで完了したときに成立する。

```text
source folder
  → real analysis / evidence quality pass
  → actual editorial proposal
  → useful first preview
  → 感想による原因調査
  → 実試作品の比較
  → operator採用/却下/復帰
  → partial rebuild / Resolveで編集可能な納品物
  → 7領域 + technical/editorial QC
  → operator: publishable
  → publication approval（別判断）
  → upload/scheduleのremote acknowledgement
```

この条件を満たすまでは、V5.0の状態は「作成済みPRD」であり「製品合格」ではない。定常状態のAHT中央値≤30分は、bootstrapの一回の成功や単純な動画の10分記録だけで達成扱いしない。
