# TikTok/Reels/Shorts系 能力地図（systematic capability map, Phase 1）

sudaさん指示（2026-09-06）・Opus構成で作成。目的はひとつ: **sudaさんが新しい動画
スタイルを試すとき、ダビンチエージェントのシステム起因で検証が入ったり止められ
たりしないよう、領土を事前に地図化する**。sudaさん原文:

> 私は新しい動画スタイルを試すときに、ダビンチエージェントのシステム起因で検証が
> 入ったり止められるのが嫌なの。事前に貴方やsolが検証をすべき

## 冒頭集計 — 機能 69 件中 未検証 54 件 (78%)

| 状態 | 件数 | 意味 |
|---|---|---|
| 未検証 | **54** | 一度も試していない。この地図の要点 |
| 検証済みA | 8 | API書込＋読み戻しで検証済み |
| 検証済みC | 4 | 座標＋Directでlive検証済み（T12はIと重複） |
| 検証済みI | 3 | interchange（.drt/.drx）で検証済み（T12はCと重複） |
| 危険 | 1 | 実測クラッシュ記録あり |
| 不可 | 0 | 到達不能と測定されたものはなし |

状態語彙は固定（言い換え禁止）。「たぶんできる」「should work」は書かない。
検証済みタグはすべて根拠path付き。根拠なきものは未検証のまま置く。

- **検証済みA** — API write+readback verified（本日の8: Zoom/Position/Rotation/
  Crop/Opacity/Text+本文/Text+書式/Voice Isolation＋書込＋読戻しのあるv4.3 probe）
- **検証済みC** — coordinate+Direct verified live（本日: 速度25%・音量-6dB・
  lift・字幕キュー本文）
- **検証済みI** — interchange（.drt/.drx）verified（速度0.5・CDL .drx
  generate+apply。後者は値校正の注意付き）
- **未検証** — never attempted
- **不可** — measured unreachable
- **危険** — measured crash risk

## 機能表

機能IDは `~/.metacua/DAVINCI_CAPABILITY_INDEX.md` の verbatim。TikTok系での用途は
3目標スタイル（style-vocabulary.md B節: ①宋世羅-style静的テロップ＋変調 ②TikTok
縦型太字字幕ズーム多用 ③vlog地点表示）＋ vendor rough-cut ガイドの toolkit を
filterに derive した。表にない索引行（T18/T19、U07、C10、M02〜M05/M08〜M12、
A01〜A03/A06/A07、O01〜O04、Q01/Q02/Q04/Q05/Q07/Q08、R07）は縦型・短尺の通常
編集で直接使わないため省略した（省略自体が判断であることを明示する）。

### A/M. プロジェクト・素材（縦型の前提＋素材搬入）

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| A04 | Project設定の読替・変更 | 縦型9:16（解像度・fps）の設定変更。前提条件ブロック参照 | 検証済みA | 未検証（v4.3 project-timeline-creation probeはtimelineFrameRate=30のみ。解像度変更は未試行） |
| A05 | Timeline設定の読替・変更 | 縦型Timelineの tall 解像度設定。前提条件ブロック参照 | 検証済みA | project_settings.set_setting（timelineResolutionWidth/Height→1080x1920、readback OK）→新規タイムラインが縦型を継承→レンダリング出力も1080x1920を確認。**既存タイムラインの解像度は timeline.set_setting で変更不可（success False 実測）— 作成前のプロジェクト設定かGUIで変更** |
| M01 | 素材Import | 縦型素材・写真素材（wishlist #3挿入カット）の搬入 | 検証済みA | `video-pipeline/capabilities/v4.3/mcp-fit.json`（import-media: 3 clips＋media-pool ids読戻し、status accepted） |
| M06 | 既存ProxyのLink/Unlink | 4K縦型の重い素材を軽く扱う | 検証済みA | 未検証 |
| M07 | Proxy/Optimized Media新規生成 | 同上（生成はCU route） | 検証済みC | Media Pool右クリック→「最適化メディアを生成」で実生成を確認（6.8GB・8,467キャッシュファイル、進捗ダイアログもAX読取可）。APIはリンク系のみで生成actionなし＝生成はGUI起動。メニュー項目はAX読めるので自動化可。プロキシー生成も同メニュー系 |

### T. Timeline構築・カット・配置・Retime

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| T01 | 新規Timelineを素材から構築 | アセンブリ、写真挿入カット（wishlist #3）の配置 | 検証済みA | `video-pipeline/capabilities/v4.3/mcp-fit.json`（exact-source-range-placement: 配置＋source/record span読戻し／project-timeline-creation: 作成＋current読戻し、共にaccepted） |
| T02 | 新規DRT/DRPをoffline authoring | 再構築方式の切断・再配置全般 | 検証済みI | 未検証（drt.assemble自体はT12速度検証で使用実績あり。カット構成用途の検証なし） |
| T03 | Track追加・削除・名前・Lock・Enable | 字幕・テロップ用V2/V3 track確保 | 検証済みA | 未検証 |
| T04 | Clip/RangeのCopy・Move・Duplicate | まとめ切り抜き（wishlist #2）の切貼り | 検証済みI | 未検証 |
| T05 | Ripple Insert / Timeline Ripple | 詰め編集・挿入 | 検証済みI | 未検証 |
| T06 | Overwrite・Lift・Range置換 | 区間差替え | 検証済みA | 未検証 |
| T07 | Blade/Razor/Split | ジャンプカット（TikTok系の高頻度技法） | 検証済みI | 未検証（LiveはCU route。Offline drp.split_clipも未試行） |
| T08 | Trim/Slip/Slide/Roll | 間詰め・尺調整 | 検証済みI | 未検証 |
| T09 | Transitionを新規・再構築Timelineへauthoring | wipe/dip/glitch等（cross-dissolve以外全部） | 未検証 | 未検証（vendorはv2.111+/v2.138+でrender検証を主張。vendor api-coverageは参考記録であり我々の検証ではない） |
| T10 | Transitionを開いているTimelineで追加・変更 | 同上（既存Timelineへの追加） | 未検証 | 未検証（API経路の到達不能はv4.3 transition-path probeで実測 failed。CU経路は未試行。Phase-0指示では proven とされるが対応evidence path未確認 — Phase-2で証拠確定要） |
| T11 | 既存Transitionの検出・QC・削除 | 既存遷移の確認・除去 | 未検証 | 未検証 |
| T12 | 一定速度Retime | スロー・倍速（速度演出の基本） | 検証済みC / 検証済みI | C: `private/runtime/sol-input-mech-20260906/summary_report.txt`（item6_run5b_VERIFIED_WORKFLOW_SUCCESS: 25%をverified-button workflow＋render cadence SSIMで確認）。I: 同（item6_run1b: .drt経由50%をrender cadenceで確認。ただし変更ボタン押下の帰属補正 CORRECTION_item6_run1b_attribution あり） |
| T13 | Reverse | 逆再生演出 | 危険(.drt)/未検証 | .drt経路: `private/runtime/sol-input-mech-20260906/summary_report.txt`（RECORD_reverse_crash_verbatim: reverse（cuts[].reverse）の.drt importはResolve 21.0.4.5でクラッシュする（実測1回）。再現確認は費用対効果から未実施）。GUI経路: 2026-09-07 C-route試行15steps（T13_REVERSE_TOGGLED / T13_REVERSE_CHECKED_CLICK — speed dialogのreverse checkboxをverified pressで押下）したが読み戻し確定に至らず未検証（journal 04:14:10） |
| T14 | 可変Speed Ramp | スピードランプ演出 | 未検証 | 未検証（freezeは同Sm2TimeMap系でsuspended。crash-tolerant宣言なしに再開しないこと — summary_report.txt RECORD_reverse_crash_verbatim） |
| T15 | 既存clipのRetime対話編集 | ramp・easingの手直し | 未検証 | 未検証（CU route。T12の25%はダイアログ値の確定でありCurve/easing編集ではない） |
| T16 | Stabilize / Smart Reframe | 手ブレ・縦型reframe | 検証済みA | 未検証 |
| T17 | Compound / Fusion Clip作成 | 複合演出の1object化 | 検証済みA | 未検証 |

### F. Transform・Edit Effects・Fusion・Magic Mask

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| F01 | Inspector Transform/Crop/Composite | ズーム・位置・回転・クロップ・不透明度（パンチインの基本） | 検証済みA | `private/runtime/sol-matrix-20260906/summary.md`（#1 Zoom 1.0→1.35／#2 Position／#3 Rotation 5.0／#4 Crop 64／#5 Opacity 80、いずれもget_*読戻し）＋mcp-fit.json clip-transform-punch-in probe |
| F02 | propertyのkeyframe | **スムーズなアニメatedズーム**（Transform zoom静止画はAだが動きはここ） | 検証済みC | 検証済み（2026-09-07 C-route: Inspector ビデオタブのdiamondで86400:1.0→86849:1.5。render A/B: a=baseline同一(PSNR inf)/b=980,942px差分/c=1,217,639px差分の単調プログレッション、視覚確認済み。タブ切替はhover-dwell 1.5s必須（即時click・AXPress・AXSetValは全部不発）。API add_keyframeはNoneType破損、Fusion compは21.0.4でrender inert） |
| F03 | Fusion Comp追加・Import/Export・切替 | Text+/エフェクトの土台 | 検証済みA | mcp-fit.json fusion-template-insertion（insert＋comp count=1読戻し、accepted）＋matrix #8（insert_fusion_title→set_text_plus読戻し） |
| F04 | Fusion node追加・削除・接続 | モーション系の下地（Blur/Transform/Merge/Mask） | 検証済みA | fusion_comp API（add_comp→add_tool→connect→set_input、スコープは timeline_item={track_type,track_index,item_index} ネスト必須）でBlur構成を作成→render A/Bで実ブラーを確認（全ピクセル差分、平均保存・分散減 = 本物のブラー）。削除・再接続も成功。注意: 2026-09-06には同一API経路でrender失敗（Fusionコンポジション処理エラー）を計測済み — 条件不明だが普遍ではない |
| F05 | Fusion parameter・animation | RPG風地点表示の速い出入り（style ③）等 | 検証済みA | fusion_comp add_keyframeでXBlurSize 0→40アニメーションを作成、render A/Bで2地点の出力差异（blur0: std25.7 / blur40: std21.4）を確認＝アニメーションはレンダリングに反映される。**要件事項: キーフレーム時刻はコンポローカル時間（クリップ先頭=0）必須。タイムラインレコードフレームを渡すと範囲外で黙って無視される（2026-09-06の「render不反映」計測はこの時間基準違いが原因）** |
| F06 | Edit ResolveFX/OpenFXをclipへ追加 | glitch等の質感エフェクト付与 | 未検証 | APIにfx追加actionなし（確認済み）。C経路: エフェクトライブラリを開いてResolveFXドラッグを3方式で試行したが合成イベントではドラッグ&ドロップが登録されず（render A/Bで効果なしを確認）。手動操作は可能と推奨されない理由なし — 人手またはUI自動化の別手段で再挑戦余地あり |
| F07 | Edit FXのInspector parameter調整 | 同上の調整 | 未検証 | F06に依存（追加できたResolveFXが作業タイムラインに存在せず未検証） |
| F08 | OFX Generator挿入 | 独立Generator clip | 不可 | 本機にOFX generatorプラグイン未インストール（/Library/OFX・~/Library/OFXとも不在を実測）。API挿入は失敗、ドラッグ対象も存在しない。独立Generatorはnative generator（insert_generator成功済み）で代替可能 |
| F09 | Color node内OFXをoffline編集 | Color内エフェクトの構造編集 | 未検証 | 未検証 |
| F10 | Magic Mask | 背景差替え（wishlist #4）・被写体分離（style ①モノクロ演出） | 不可 | 未検証（subject clickはHITL必須。MCPはclick生成不能） |

### S. Title・Text+・Native Subtitle

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| S01 | Text+/Fusion Title作成・文字変更 | 常時テロップ2階層（style ①実装済み承認分）・太字字幕の器 | 検証済みA | matrix summary.md #8（null→"マトリクス確認"完全一致）・#9（Size 0.09→0.06／Red1 1.0→0.5読戻し。Font/Size/色input存在確認＋product実績） |
| S02 | Titleを指定track・frameへ配置 | V2/V3への正確な重ね置き | 検証済みI | 未検証（vendor trap: Insert系はtrackIndexを持たず常にV1 — issue #74） |
| S03 | 自動字幕生成 | 字幕起こしの起点 | 検証済みA | timeline_ai.create_subtitles（background=true＋job poll）で字幕トラック0→1・実素材日本語音声から3キュー生成を確認。同期呼び出しは5連続失敗（2026-09-06）— background実行が実レシピ。追加音声は文字起こし範囲外だった点に注意 |
| S04 | SRTからNative字幕付き新規DRT | 字幕付きTimeline再構築 | 検証済みI | 未検証 |
| S05 | SRTを開いているTimelineへImport | 既存Timelineへの字幕追加 | C経路未試行 | 未検証（CU route。API経路なし） |
| S06 | Native字幕1件の本文修正 | 誤字修正 | 検証済みC | matrix summary.md #12（AI locate移動のみ＋IME保護＋Direct入力→get_transcript「指揮らい」→"MATRIX-EDIT-OK"読戻し。他キュー不変） |
| S07 | Native字幕1件の開始・終了・分割・結合 | 語を切らない分割（style-vocab A節の硬制約）・word-by-word字幕の部品 | 検証済みI | 未検証（CU route） |
| S08 | Subtitle track全体のstyle | 全字幕のfont/サイズ一括変更 | 未検証 | 未検証（Advanced project_db。Resolve完全終了必須） |
| S09 | Native字幕ごとのstyle/位置調整 | 1件だけの強調 styling | 危険(API書込)/未検証 | **危険: 字幕アイテムへのscripting-APIプロパティ書込（SetProperty。inline・MCP server両経路）はResolve 21.0.4.5をハング→クラッシュさせる（3/3再現、読取は安全）——パイプライン全体で書込禁止にすべきハザード（Opus判断待ち）**。C経路: キュー選択→キャプションエディタ（キュー毎ナビ・テキスト・「キャプションをカスタマイズ」チェックボックス、ON持続を確認）・スタイル欄（フォント/サイズ/位置）存在まで確認。ただし値変更まで到達せず（サイズ行が画面外・ポップアウト不安定・クリックで選択解除）。なおDeliverレンダリングは字幕を焼き込まない |
| S10 | 字幕付き納品（Burn-in/embedded/sidecar） | プラットフォーム別字幕納品 | 検証済みA | 未検証 |

### C. Color・Grade・Scope

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| C01 | LUT/CDL/DRXを既存clipへ適用 | LUT・film-look・saturation pop適用 | 検証済みI | summary_report.txt CDL_DRX_INTERCHANGE_ROUTE（drx.generate lift→safe_apply_drx→render変化 2073597/2073600画素。**値校正の注意**: drx lift 0.05はGUI lift 0.05の+21.94ではなく原画近傍に着地。vendor CALIBRATION-STATUS/DRX-VALUE-SCALING未校正）＋mcp-fit color-grade-preset-drx accepted |
| C02 | Grade Copy・Version・Restore | cut間look統一 | 検証済みA | グレード適用（25万px差分）→別クリップへCopyGrades（29万px差分＝カット間ルック統一）→バージョン保存・復元（identityとgradedを行き来しrender両方向で実証）すべてAPI＋render A/Bで確認。注意: safe_set_cdl/safe_copy_gradeラッパーは一部動作不良（生APIとinline CopyGradesを使用）。バージョンAPIのtypeは文字列でなく整数（0=local） |
| C03 | Gallery Still・LUT Export | look保存・持越し | 検証済みA | 未検証 |
| C04 | Primaries/Curvesを画を見ながら調整 | lift等（強調区間の背景モノクロ/黒 — style ①複合演出の映像側） | 検証済みC | summary_report.txt item11_REMEASURED（Color page lift数値field→画素diff 2073597/2073600変化・平均輝度35.48→57.42。matrix午前の0画素は座標miss artifactと確定） |
| C05 | Primaries/Curves/Node treeをoffline authoring | 決定済みgradeの構造生成 | 検証済みI | C01と同一証拠（generate＋apply実証済み。値スケール校正が残作業） |
| C06 | Power Window/Qualifier/HDR/Blur/Key | 被写体だけ残し背景モノクロ（style ①）・背景ぼかし（wishlist #4系） | 検証済みC | 2026-09-07 C-route: Color pageでcircle Power Window追加→彩度0のgrade適用、render A/Bで検証（journal 00:32:44）。**実測: gallery stills PNGはgradeの視覚証拠にならない（viewerにgrade出ているのに0px差）— grade証明はrender A/Bのみ** |
| C07 | Tracker / Color Warper | モーショントラッカー正対の地点表示（style ③） | 検証済みC | ColorページTrackerのウィンドウ追跡はrender A/Bで実証（追従ウィンドウがフレーム毎に実レンダリング内で変位: 純追跡差分 100/99px vs ビット一致ゼロ床、sat再飽和クラスタがmid→endで進行）。注意: 素材がほぼ無彩色のため効果は小さめ、Warperグリッドドラッグは未実証、track開始クリックがsilent-failする例あり（playhead進行で要確認） |
| C08 | Shot/Skin/WB/Reference match | 複数cutの色統一 | 検証済みI | 未検証 |
| C09 | Scope/Gamut/Legal QC | 品質測定 | 検証済みI | 未検証 |

### U. Audio・Fairlight

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| U01 | Audio構造・mapping・level調査 | mix前の測定 | 検証済みA | 未検証 |
| U02 | Voice Isolation | 声の分離 | 検証済みA | matrix summary.md #10（{false,0}→{true,70}読戻し）＋mcp-fit voice-isolation accepted |
| U03 | Fairlight Preset適用 | 定型mix再適用 | 未検証 | 未検証（Resolve 20.2.2+ version-gated — vendor resolve-audio skill） |
| U04 | Clip/Track Volume・Pan個別調整 | 音量調整（-6dB等） | 検証済みC | summary_report.txt item7_REMEASURED（Inspector volume field→render -6.000000dB exact。keyboard routeはinert確定） |
| U05 | EQ/Compressor/Automation/FairlightFX | **エコー**（style ①強調セリフの音声側）・声変調 | 未検証 | 未検証（CU route。MCP手段の有無自体未確認 — style-vocab B①） |
| U06 | AI Audio Assistant | one-click mix | 未検証 | スクリプト経路なし（vendor issue #128のまま再確認）。GUI: Fairlight上にAIアシスタントの直接ボタンは見つからず、タイムライン>AIツール submenu は存在するが合成入力では展開失敗（3回）。人手UIなら到達可能と推定 |
| U08 | BGM ducking計画・offline試聴mix | 声に合わせたBGM計画 | 検証済みI | 未検証（v4.3 bgm-track-ducking probe failed: scripting APIにducking面なし） |
| U09 | BGM ducking実施・微調整・音楽sync | 声に合わせたBGM・音楽同期カット | C経路未試行 | API: ducking/sidechain書込actionなし＋音量キーフレーム不可（AddPoint不在、Volume書込も拒否: 2ラウンド実測）。**CU Fairlight automation-lane routeは未試行（高コストのため未実施 — 3rd agent journal 04:23:39自身のnote）**。結果レベルはU08 offline plan + ffmpeg mixで代替達成可能（suda review 43729f7と同じ結論）。「実現不可」は誤り |
| U10 | Audio fileのsplit/trim/convert | SFXタイミング用offline加工 | 検証済みI | 未検証 |
| U11 | Loudness/True Peak/LRA納品QC | 納品 loudness 測定 | 検証済みI | 未検証 |

### R. Render・Deliver・出力QC

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| R01 | Render設定・Preset・Job準備 | 1080x1920等の設定（縦型は未試行 — 前提条件ブロック参照） | 検証済みA | mcp-fit.json render-configuration（set＋validated echo。GetRenderSettingsは当該buildで不可のためechoが読戻し。1920x1080での実績であり縦型値ではない） |
| R02 | Render開始・停止・進捗確認 | 書出し実行 | 検証済みA | mcp-fit.json render-job-lifecycle（CompletionPercentage=100＋output存在＋delete） |
| R03 | Render出力の即時検証 | 生成物のspec検査 | 検証済みA | 未検証（render.verify_output未使用。本日のSSIM/画素diffはad-hoc scriptであり同toolの検証ではない） |
| R04 | Deliverページ固有設定をGUI操作 | APIにないcheckbox等 | 未検証 | チェックボックスの所在とAX読取（値・座標）は確認（ネットワーク最適化/チャプター生成/縦型解像度）。ただし合成クリックでは1つもトグルできず（4試行、ボックス領域のピクセル差分0）— 本セッションの自動操作は不調。人手操作は当然可能。なおrender.get_settingsが空dictを返すbuildのため、GUIトグルのAPI読み戻し検証も不可 |
| R05 | 納品QC・Compliance | 規格照合 | 検証済みI | 未検証（v4.3 advanced-delivery-qcはsurface到達のみ。QC判定自体の検証なし） |
| R06 | 字幕出力検証 | burn-in/embedded/sidecar確認 | 検証済みA | 未検証 |

### Q. 解析・編集判断（TikTok系に直結するもののみ）

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| Q03 | Scene Cut Detection | cut検出（jump-cut補助） | 検証済みA | 未検証 |
| Q06 | Editorial plan・Selects・Silence edit案 | 冒頭10秒まとめ（wishlist #2）の判断材料 | 未検証 | edit_engine.plan_selects 自体は存在するが解析DBが前提。本機はローカルtranscription backendが未導入で解析パイプラインが起動不可（no_auto_install方針）— backend導入後に再挑戦。v4.3のselects probe失敗と同じ所在 |

## 縦型（9:16）前提条件ブロック — スタイルではなく出力形式の変更

style-vocabulary.md B②の通り、縦型は「スタイル」ではなく**出力フォーマットの
変更**である。現行 pipeline は横型（render-qc.json が 1920x1080 固定）に組まれて
おり、以下が連鎖で変わる。**この連鎖全体が未検証**である。

特記: Phase-1指示にあった「resolve-rough-cut の vertical-timeline-setup 節」は、
同梱版（MCP v2.207.0）の当該スキルに存在しない。同スキルが縦型に触れるのは
phone-footage の rotation/VFR trap 行と timelineFrameRate setup 手順のみである。
以下は索引＋他ガイド＋自 repo 記録からの derive であり、ガイドの既存節の引用
ではない。

| # | 変わるもの | 内容 | 状態 |
|---|---|---|---|
| V1 | Timeline解像度 | tall 解像度への設定変更（A04/A05 route。timelineFrameRateはtimeline作成後にlockされる実測 — vendor trap。解像度も同手順要確認） | 検証済みA |
| V2 | 素材扱い | rotation flag付きphone footageは既に縦（reframe禁止 — vendor trap）。VFRはtimelineがconformしたFPSに合わせる（r_frame_rateを見ない） | 未検証（trapはvendor実測の引用。我々の縦型素材での確認なし） |
| V3 | 構図・テロップ・字幕サイズ | 全部変わる。現行承認値（テロップ2階層・字幕5.5%/W5・中央配置）は1920x1080前提（style-vocab A節＋presentation-profile-default.json） | 検証済みA |
| V4 | Render設定 | 1080x1920での設定・実行（R01/R02 route）。vendorには `tiktok`/`reels`/`shorts`→`h264_vertical_1080_web`（1080x1920）のdelivery target aliasが存在する（`private/vendor/davinci-resolve-mcp/src/utils/delivery_targets.py`）が、**我々は一度も使っていない** | 検証済みA |
| V5 | QC | render-qc.json 1920x1080固定の作り替え（R05/R06 route） | 検証済みI |
| V6 | 両立判断 | 縦型メインか横型との両立か — sudaさんの出力要件決定自体が未（style-vocab C②） | 未決定 |

## Top 未検証（使用頻度順・ガイドの強調からの記述的 ranking。 feasibility の推測ではない）

1. **F02 keyframeによるスムーズズーム** — TikTok系②の核心（静止zoomはA、動きは未検証）
2. **T07 blade/jump-cut＋T08 trim詰め** — 短尺の pacing の基本動作
3. **T09/T10 transition全種**（wipe/dip/glitch等。cross-dissolve以外）— wishlist #1。T10はPhase-0指示で proven とされるが証拠path未確認
4. **word-by-word字幕の複合**（S07分割＋T07＋transcript。部品すべて未検証）
5. **style ①複合演出**（U05エコー＋C06/C04背景モノクロ/黒＋F10分離。音声・映像とも未検証）
6. **U08/U09 ducking＋音楽sync** — BGMを敷く短尺の必須級
7. **縦型連鎖 V1〜V6** — ②に着手する前の前提全部
8. **C07 tracker正対・F05 RPG風出入り**（style ③バリエーション。難易度高の明記あり）
9. **wishlist #2 冒頭10秒まとめ**（T04＋Q06。編集判断が必要でFirst Publish後に後回しされた経緯あり）
10. **M07 proxy生成・F06/F07 ResolveFX系・S10字幕納品** — 量産期の効き目系

## Sources（この地図の derive 元）

- 骨格: `~/.metacua/DAVINCI_CAPABILITY_INDEX.md`（機能ID verbatim。対象version: Resolve Studio 21.0.4／MCP 2.207.0）
- vendor craft guides: `private/vendor/davinci-resolve-mcp/.agents/skills/`（resolve-rough-cut／resolve-edit／resolve-color／resolve-audio／resolve-fusion／resolve-conform／resolve-delivery／resolve-media-pool／resolve-media-analysis）＋ `src/utils/delivery_targets.py`（tiktok alias）＋各スキルの traps/gotchas（MediaOut配線・trackIndex欠落・timelineFrameRate lock・rotation/VFR・render preset pinning issue #123 等は引用であり我々の検証ではない）
- 目標スタイル: `docs/style-vocabulary.md`（3 styles＋既知/未知分解）／棚上げ要望: `docs/operator-wishlist.md`（#1 transition〜#5 宋世羅-level）
- 本日の検証証拠: `private/runtime/sol-matrix-20260906/summary.md`（matrix表・A=8/C=1/D=2→再測定で更新）＋ `private/runtime/sol-input-mech-20260906/summary_report.txt`（再測定: 速度25%・音量-6dB・lift・CDL .drx route・reverse crash verbatim）
- August baseline: `video-pipeline/capabilities/v4.3/mcp-fit.json`（22-probe: 16 accepted／6 failed。acceptedのうち書込＋読戻しを伴うもののみAの根拠とし、probe-level（subtitle-capability等）は注記に留めた。failedはAPI到達不能の実測として注記し、CU routeが残る限り不可にしない）
- vendor api-coverage.md の live-test 表は未参照（参照しても我々の検証には数えない規律のため）

## 正直さの記録（この地図の限界）

- T10 transition は Phase-0 指示で proven とされるが、本マップの証拠dirに対応記録がなく未検証に置いた。証拠pathの確定が Phase-2 の先頭作業である。
- vendor の主張（DRT transition render検証済み、DRX calibration済み集合等）は引用に留め、我々の検証に数えていない。 vendor の主張と我々の検証の混同は、sudaさんが避けたい事態そのものである。
- `operator-wishlist.md` 自体が「検出済みのものだけであり網羅ではない」。この地図も同様に、未検出の要望領域を覆わない。

## 更新運用

- 検証が1件通るたび該当行の状態＋根拠pathを更新し、冒頭集計を直す
- 状態語彙の追加・言い換えはしない（6語固定）
- 原文（sudaさん指示・crash verbatim）は一字一句を優先する
- 本ファイルはdocs配下でバージョン管理される（会話履歴に依存しない）
