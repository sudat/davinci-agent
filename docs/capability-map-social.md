# TikTok/Reels/Shorts系 能力地図（systematic capability map, Phase 1）

sudaさん指示（2026-09-06）・Opus構成で作成。目的はひとつ: **sudaさんが新しい動画
スタイルを試すとき、ダビンチエージェントのシステム起因で検証が入ったり止められ
たりしないよう、領土を事前に地図化する**。sudaさん原文:

> 私は新しい動画スタイルを試すときに、ダビンチエージェントのシステム起因で検証が
> 入ったり止められるのが嫌なの。事前に貴方やsolが検証をすべき

2026-09-07 レビュー（suda指示・10エージェント監査+Fable指摘）: 判定修正10件・根拠更新22件・高頻度欠落の新規行25件（N接頭辞・索引に不在）を追加、索引省略行からQ01/M08の2件を復帰。
## 冒頭集計 — 全 102 項目（2026-09-07 レビュー反映後・実測）

| 状態 | 件数 | 意味 |
|---|---|---|
| 未検証 | **46** | 一度も試していない（N行27件を含む）。この地図の要点 |
| 検証済みA | 26 | API書込＋読み戻しで検証済み |
| 検証済みI | 12 | interchange（.drt/.drx）で検証済み |
| 検証済みC | 8 | 座標＋Directでlive検証済み |
| 複合状態 | 6 | 1行に2状態併記（T12/T13/S09/S07/C08/C03） |
| C経路未試行 | 2 | API/interchange試行済み・GUI操作は未試行 |
| 不可 | 1 | 到達不能と測定 |
| 未決定 | 1 | 判断未了 |
| **計** | **102** |  |

状態語彙は固定（言い換え禁止）。「たぶんできる」「should work」は書かない。
検証済みタグはすべて根拠path付き。根拠なきものは未検証のまま置く。

- **検証済みA** — API write+readback verified（本日の8: Zoom/Position/Rotation/
  Crop/Opacity/Text+本文/Text+書式/Voice Isolation＋書込＋読戻しのあるv4.3 probe）
- **検証済みC** — coordinate+Direct verified live（本日: 速度25%・音量-6dB・
  lift・字幕キュー本文）
- **検証済みI** — interchange（.drt/.drx）verified（速度0.5・CDL .drx
  generate+apply。後者は値校正の注意付き）
- **未検証** — never attempted
- **C経路未試行** — API/interchange試行済み・GUI操作は未試行
- **不可** — measured unreachable
- **危険** — measured crash risk

## 機能表

機能IDは `~/.metacua/DAVINCI_CAPABILITY_INDEX.md` の verbatim。TikTok系での用途は
3目標スタイル（style-vocabulary.md B節: ①宋世羅-style静的テロップ＋変調 ②TikTok
縦型太字字幕ズーム多用 ③vlog地点表示）＋ vendor rough-cut ガイドの toolkit を
filterに derive した。表にない索引行（T18/T19、U07、C10、M02〜M05/M09〜M12、
A01〜A03/A06/A07、O01〜O04、Q02/Q04/Q05/Q07/Q08、R07）は縦型・短尺の通常
編集で直接使わないため省略した（省略自体が判断であることを明示する。Q01・M08は2026-09-07レビュー+Fable指摘で復帰）。

### A/M. プロジェクト・素材（縦型の前提＋素材搬入）

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| A04 | Project設定の読替・変更 | 縦型9:16（解像度・fps）の設定変更。前提条件ブロック参照 | 検証済みA | journal 20:05:11 検証済みA: 解像度 1920x1080→1080x1920 書換+readback実証（write+readback）。旧記述（v4.3 probeはfps=30のみ）は陳腐化。 |
| A05 | Timeline設定の読替・変更 | 縦型Timelineの tall 解像度設定。前提条件ブロック参照 | 検証済みA | project_settings.set_setting（timelineResolutionWidth/Height→1080x1920、readback OK）→新規タイムラインが縦型を継承→レンダリング出力も1080x1920を確認。**既存タイムラインの解像度は timeline.set_setting で変更不可（success False 実測）— 作成前のプロジェクト設定かGUIで変更** |
| M01 | 素材Import | 縦型素材・写真素材（wishlist #3挿入カット）の搬入 | 検証済みA | `video-pipeline/capabilities/v4.3/mcp-fit.json`（import-media: 3 clips＋media-pool ids読戻し、status accepted） |
| M06 | 既存ProxyのLink/Unlink | 4K縦型の重い素材を軽く扱う | 検証済みA | journal 20:10:48 検証済みA: link_proxy書込+Proxy属性readback（Proxy=1920x1080・Proxy Media Path設定確認）。 |
| M07 | Proxy/Optimized Media新規生成 | 同上（生成はCU route） | 検証済みC | Media Pool右クリック→「最適化メディアを生成」で実生成を確認（6.8GB・8,467キャッシュファイル、進捗ダイアログもAX読取可）。APIはリンク系のみで生成actionなし＝生成はGUI起動。メニュー項目はAX読めるので自動化可。プロキシー生成も同メニュー系 |
| M08 | Relink・Replace source clip | メディアオフライン時の再接続・素材差し替え（プロジェクト移動・素材整理で必須の復旧操作） | 未検証 | 索引行M08は省略リストにあったが2026-09-07レビュー+Fable指摘で復帰。API面: media_pool.safe_relink / media_pool_item.replace_clip / replace_clip_preserve_sub_clip（索引記載、未試行。file identity曖昧なら停止の注意付き）。TimelineのReplace Editとは別。 |

### T. Timeline構築・カット・配置・Retime

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| T01 | 新規Timelineを素材から構築 | アセンブリ、写真挿入カット（wishlist #3）の配置 | 検証済みA | `video-pipeline/capabilities/v4.3/mcp-fit.json`（exact-source-range-placement: 配置＋source/record span読戻し／project-timeline-creation: 作成＋current読戻し、共にaccepted） |
| T02 | 新規DRT/DRPをoffline authoring | 再構築方式の切断・再配置全般 | 検証済みI | journal 19:47:10 検証済みI: drt.assemble+import実走、authored構成の着地をreadback（fps含む）。 |
| T03 | Track追加・削除・名前・Lock・Enable | 字幕・テロップ用V2/V3 track確保 | 検証済みA | journal 19:35:59 検証済みA: track 2→3→2追加/削除、名称「sag_telop」設定、lock true・enabled falseを全てreadback。 |
| T04 | Clip/RangeのCopy・Move・Duplicate | まとめ切り抜き（wishlist #2）の切貼り | 検証済みA | journal 19:46:24 検証済みA（API route: append+delete primitivesによるcopy/move/duplicate配置をreadback）。interchange走行はゼロ——旧I表記は誤り。vendor trap（issue #74）はAPI insert系の別問題。 |
| T05 | Ripple Insert / Timeline Ripple | 詰め編集・挿入 | 未検証 | ripple_insert API 6ラウンド全失敗、末尾破損landmine実測（tail 360f→288f, journal 19:46:24 未検証）。interchange re-author（T02系）でのripple代替も未走行。旧I表記は誤り。 |
| T06 | Overwrite・Lift・Range置換 | 区間差替え | 検証済みA | journal 19:42:05 検証済みA: overwrite/lift実行後のitem配置をreadback。 |
| T07 | Blade/Razor/Split | ジャンプカット（TikTok系の高頻度技法） | 検証済みI | journal 19:48:09 検証済みI: drt.assembleでsplit構成を実authoring+import、split_record_exact=true / source_halves_distinct=true。※drp.split_clipは未試行。 |
| T08 | Trim/Slip/Slide/Roll | 間詰め・尺調整 | 検証済みI | journal 19:48:09 検証済みI: drt.assembleでtrim/slip構成を実authoring、rec 100f exact・slip src 600→750の決定論的変換をreadback。 |
| T09 | Transitionを新規・再構築Timelineへauthoring | wipe/dip/glitch等（cross-dissolve以外全部） | 未検証 | 未検証（vendorはv2.111+/v2.138+でrender検証を主張。vendor api-coverageは参考記録であり我々の検証ではない） |
| T10 | Transitionを開いているTimelineで追加・変更 | 同上（既存Timelineへの追加） | 未検証 | 未検証（API経路の到達不能はv4.3 transition-path probeで実測 failed。CU経路は未試行。Phase-0指示では proven とされるが対応evidence path未確認 — Phase-2で証拠確定要） |
| T11 | 既存Transitionの検出・QC・削除 | 既存遷移の確認・除去 | 未検証 | 未検証 |
| T12 | 一定速度Retime | スロー・倍速（速度演出の基本） | 検証済みC / 検証済みI | C: `private/runtime/sol-input-mech-20260906/summary_report.txt`（item6_run5b_VERIFIED_WORKFLOW_SUCCESS: 25%をverified-button workflow＋render cadence SSIMで確認）。I: 同（item6_run1b: .drt経由50%をrender cadenceで確認。ただし変更ボタン押下の帰属補正 CORRECTION_item6_run1b_attribution あり） |
| T13 | Reverse | 逆再生演出 | 危険(.drt)/未検証 | .drt経路: `private/runtime/sol-input-mech-20260906/summary_report.txt`（RECORD_reverse_crash_verbatim: reverse（cuts[].reverse）の.drt importはResolve 21.0.4.5でクラッシュする（実測1回）。再現確認は費用対効果から未実施）。GUI経路: 2026-09-07 両ラウンドで到達点更新も未達成。第1ラウンド（15steps）: speed dialogのreverse checkboxをverified pressで押下したがAX読取はval=0のまま（journal 04:14:10）。第2ラウンド（4th agent）: Inspector/速度ダイアログの逆再生checkboxをAXPressで**val=1まで到達・確認**（初）→「変更」押下でダイアログは閉じるがrenderは一切不変（5記録点を全ソースフレーム照合してマッピング導出: src≈1.25×kの125%順方向のまま、RetimeProcess=0のまま）＝checked状態はclipの再生方向にコミットされず未検証維持。逆再生の読み戻しはduration/source extentが判定不能（既存125%clipと混同注意）で、render順序照合が唯一の確定手段（journal 05:05-05:35） |
| T14 | 可変Speed Ramp | スピードランプ演出 | 未検証 | 未検証（freezeは同Sm2TimeMap系でsuspended。crash-tolerant宣言なしに再開しないこと — summary_report.txt RECORD_reverse_crash_verbatim） |
| T15 | 既存clipのRetime対話編集 | ramp・easingの手直し | 未検証 | 未検証（CU route。T12の25%はダイアログ値の確定でありCurve/easing編集ではない） |
| T16 | Stabilize / Smart Reframe | 手ブレ・縦型reframe | 未検証 | API stabilize/smart_reframe呼び出しはsuccess=Trueを返した（journal 19:58:51）が、効果のreadback・render証拠なし＝API受理のみ。検証済みAの要件（write+readback）未達のため格下げ。 |
| T17 | Compound / Fusion Clip作成 | 複合演出の1object化 | 検証済みA | journal 19:49:48 検証済みA: Fusion Clip 1作成をitem一覧+spanでreadback（Compound Clipも同様に名称+span readback）。 |

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
| F10 | Magic Mask | 背景差替え（wishlist #4）・被写体分離（style ①モノクロ演出） | C経路未試行 | API実測: create_magic_mask→needs_hitl=True（subject clickはAPIから生成不能、journal 2step）。Color pageで人間が被写体をクリックする経路は未試行——U06/U09と同型（人間操作前提なら到達可能性が残る）。 |

### S. Title・Text+・Native Subtitle

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| S01 | Text+/Fusion Title作成・文字変更 | 常時テロップ2階層（style ①実装済み承認分）・太字字幕の器 | 検証済みA | matrix summary.md #8（null→"マトリクス確認"完全一致）・#9（Size 0.09→0.06／Red1 1.0→0.5読戻し。Font/Size/色input存在確認＋product実績） |
| S02 | Titleを指定track・frameへ配置 | V2/V3への正確な重ね置き | 検証済みI | journal 19:49:06 検証済みI: drt elementsでText+がV2 86450-86550に正確着地。issue #74の罠はAPI insert系の話で、drt経路は実証済み。 |
| S03 | 自動字幕生成 | 字幕起こしの起点 | 検証済みA | timeline_ai.create_subtitles（background=true＋job poll）で字幕トラック0→1・実素材日本語音声から3キュー生成を確認。同期呼び出しは5連続失敗（2026-09-06）— background実行が実レシピ。追加音声は文字起こし範囲外だった点に注意 |
| S04 | SRTからNative字幕付き新規DRT | 字幕付きTimeline再構築 | 検証済みI | journal 20:30:08 検証済みI: drt subtitlesSrtで3キューimport、cue text readback一致（スイープ一号/二号）。 |
| S05 | SRTを開いているTimelineへImport | 既存Timelineへの字幕追加 | 検証済みC | 2026-09-07 C-route 2段: File>読み込み>字幕…（メニューAX chain press）でSRTはMedia Poolへ字幕メディアとして入る（Type=字幕・Duration 00:00:08:00 読戻し）→ プール行右クリック「選択した字幕を挿入…」で開いているTimelineへ新規字幕トラック付きでキュー挿入（subtitle track 1→2、get_transcript 3→6キュー、テキスト3/3完全一致）。配置には実測クセ（開始+10fオフセット・尺0.8x＝24fps基準とみられる近似、順序・本文は保持）。GUI経路は全行程クラッシュなし＝API経路BAN（safe_import_media+SRTクラッシュ）はAPI固有の危険と確定。実装ノート: 挿入/オーバーライト系メニューはソースビュワー対象でプール選択に効かない、字幕メディアはソースビュワーに読み込めない（ダブルクリック不発）、type-text/pasteはIMEがASCIIを全角化する（AX setValueで回避可）、プール右クリックメニューはAX読取可 |
| S06 | Native字幕1件の本文修正 | 誤字修正 | 検証済みC | matrix summary.md #12（AI locate移動のみ＋IME保護＋Direct入力→get_transcript「指揮らい」→"MATRIX-EDIT-OK"読戻し。他キュー不変） |
| S07 | Native字幕1件の開始・終了・分割・結合 | 語を切らない分割（style-vocab A節の硬制約）・word-by-word字幕の部品 | 検証済みI / 未検証 | 開始・終了のtiming制御はS04のdrt authoring証拠で検証済み（cue時刻exact、journal 21:05:35がS04 evidence引用）。分割・結合はCU経路到達不能のまま未試行。 |
| S08 | Subtitle track全体のstyle | 全字幕のfont/サイズ一括変更 | 未検証 | 未検証（Advanced project_db。Resolve完全終了必須） |
| S09 | Native字幕ごとのstyle/位置調整 | 1件だけの強調 styling | 危険(API書込)/未検証 | **危険: 字幕アイテムへのscripting-APIプロパティ書込（SetProperty。inline・MCP server両経路）はResolve 21.0.4.5をハング→クラッシュさせる（3/3再現、読取は安全）——パイプライン全体で書込禁止にすべきハザード（Opus判断待ち）**。C経路: キュー選択→キャプションエディタ（キュー毎ナビ・テキスト・「キャプションをカスタマイズ」チェックボックス、ON持続を確認）・スタイル欄（フォント/サイズ/位置）存在まで確認。ただし値変更まで到達せず（サイズ行が画面外・ポップアウト不安定・クリックで選択解除）。なおDeliverレンダリングは字幕を焼き込まない |
| S10 | 字幕付き納品（Burn-in/embedded/sidecar） | プラットフォーム別字幕納品 | 検証済みA | journal 20:56:49 検証済みA: burn-in画素差分3638px+TTML sidecar納品を実証。embedded caption modeは未試行。 |

### C. Color・Grade・Scope

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| C01 | LUT/CDL/DRXを既存clipへ適用 | LUT・film-look・saturation pop適用 | 検証済みI | summary_report.txt CDL_DRX_INTERCHANGE_ROUTE（drx.generate lift→safe_apply_drx→render変化 2073597/2073600画素。**値校正の注意**: drx lift 0.05はGUI lift 0.05の+21.94ではなく原画近傍に着地。vendor CALIBRATION-STATUS/DRX-VALUE-SCALING未校正）＋mcp-fit color-grade-preset-drx accepted |
| C02 | Grade Copy・Version・Restore | cut間look統一 | 検証済みA | グレード適用（25万px差分）→別クリップへCopyGrades（29万px差分＝カット間ルック統一）→バージョン保存・復元（identityとgradedを行き来しrender両方向で実証）すべてAPI＋render A/Bで確認。注意: safe_set_cdl/safe_copy_gradeラッパーは一部動作不良（生APIとinline CopyGradesを使用）。バージョンAPIのtypeは文字列でなく整数（0=local） |
| C03 | Gallery Still・LUT Export | look保存・持越し | 検証済みA / 未検証 | Gallery Still取得・書き出しは実証（sweep_still_1.1.1.drx+png生成、journal 20:05:10）。LUT書き出しは失敗実測（lut_exists=false・0 bytes）——LUT Export分は未検証（失敗記録あり）。 |
| C04 | Primaries/Curvesを画を見ながら調整 | lift等（強調区間の背景モノクロ/黒 — style ①複合演出の映像側） | 検証済みC | summary_report.txt item11_REMEASURED（Color page lift数値field→画素diff 2073597/2073600変化・平均輝度35.48→57.42。matrix午前の0画素は座標miss artifactと確定） |
| C05 | Primaries/Curves/Node treeをoffline authoring | 決定済みgradeの構造生成 | 検証済みI | C01と同一証拠（generate＋apply実証済み。値スケール校正が残作業） |
| C06 | Power Window/Qualifier/HDR/Blur/Key | 被写体だけ残し背景モノクロ（style ①）・背景ぼかし（wishlist #4系） | 検証済みC | 2026-09-07 C-route: Color pageでcircle Power Window追加→彩度0のgrade適用、render A/Bで検証（journal 00:32:44）。**実測: gallery stills PNGはgradeの視覚証拠にならない（viewerにgrade出ているのに0px差）— grade証明はrender A/Bのみ** |
| C07 | Tracker / Color Warper | モーショントラッカー正対の地点表示（style ③） | 検証済みC | ColorページTrackerのウィンドウ追跡はrender A/Bで実証（追従ウィンドウがフレーム毎に実レンダリング内で変位: 純追跡差分 100/99px vs ビット一致ゼロ床、sat再飽和クラスタがmid→endで進行）。注意: 素材がほぼ無彩色のため効果は小さめ、Warperグリッドドラッグは未実証、track開始クリックがsilent-failする例あり（playhead進行で要確認） |
| C08 | Shot/Skin/WB/Reference match | 複数cutの色統一 | 検証済みI / 未検証 | offline drx match tools実走（journal 21:29:44）: shot/WB gradeCount=2・drx出力あり。ただしskin_gradeCount=0（肌マッチ生成ゼロ）・match品質のrender検証なし——skin分は未検証。 |
| C09 | Scope/Gamut/Legal QC | 品質測定 | 検証済みI | journal 20:15:46: signalstats実測（YMIN=11/YMAX=229）。※経路はffmpeg offline測定で.drt/.drx不使用——I定義との適合は要確認（Opus判断待ち）。 |

### U. Audio・Fairlight

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| U01 | Audio構造・mapping・level調査 | mix前の測定 | 検証済みA | 読取専用機能——audio_mapping_report等で実データ読戻し検証（journal 19:54:23 mapping_report/probe_item/probe_track）。write操作が存在しない機能群のため「write+readback」定義は適用外（読み取り検証済みとして明記）。 |
| U02 | Voice Isolation | 声の分離 | 検証済みA | matrix summary.md #10（{false,0}→{true,70}読戻し）＋mcp-fit voice-isolation accepted |
| U03 | Fairlight Preset適用 | 定型mix再適用 | 未検証 | journal 241/252: apply_fairlight_preset 2回失敗実測（preset catalog空が原因と推定）。suda review裁定（503）: 「機能的不能ではなく前提データ不在——人間が1回preset保存すればAPI適用可能」。vendor version-gate引用は副次情報。 |
| U04 | Clip/Track Volume・Pan個別調整 | 音量調整（-6dB等） | 検証済みC | summary_report.txt item7_REMEASURED（Inspector volume field→render -6.000000dB exact。keyboard routeはinert確定） |
| U05 | EQ/Compressor/Automation/FairlightFX | **エコー**（style ①強調セリフの音声側）・声変調 | 未検証 | 未検証（CU route。MCP手段の有無自体未確認 — style-vocab B①） |
| U06 | AI Audio Assistant | one-click mix | 未検証 | スクリプト経路なし（vendor issue #128のまま再確認）。GUI: Fairlight上にAIアシスタントの直接ボタンは見つからず、タイムライン>AIツール submenu は存在するが合成入力では展開失敗（3回）。人手UIなら到達可能と推定 |
| U08 | BGM ducking計画・offline試聴mix | 声に合わせたBGM計画 | 未検証 | journal 20:15:47の内容はcapability surface JSONの読み取りのみ——ducking mix計画の実生成なし。v4.3 probe失敗記録（scripting APIにducking面なし）が実態。 |
| U09 | BGM ducking実施・微調整・音楽sync | 声に合わせたBGM・音楽同期カット | C経路未試行 | API: ducking/sidechain書込actionなし＋音量キーフレーム不可（AddPoint不在、Volume書込も拒否: 2ラウンド実測）。**CU Fairlight automation-lane routeは未試行（高コストのため未実施 — 3rd agent journal 04:23:39自身のnote）**。結果レベルはU08 offline plan + ffmpeg mixで代替達成可能（suda review 43729f7と同じ結論）。「実現不可」は誤り |
| U10 | Audio fileのsplit/trim/convert | SFXタイミング用offline加工 | 検証済みI | journal 20:17:44 検証済みI: converted.aac+trimmed.wav生成実証（trim/convert分）。splitはエラーのまま未検証。※offline tool経路。 |
| U11 | Loudness/True Peak/LRA納品QC | 納品 loudness 測定 | 検証済みI | journal 20:17:44 検証済みI: ebur128実測（integratedLufs -70 / truePeak -33.2 / LRA 0）。※offline測定経路（I定義との適合は要確認）。 |

### R. Render・Deliver・出力QC

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| R01 | Render設定・Preset・Job準備 | 1080x1920等の設定（縦型は未試行 — 前提条件ブロック参照） | 検証済みA | mcp-fit.json render-configuration（set＋validated echo。GetRenderSettingsは当該buildで不可のためechoが読戻し。1920x1080での実績であり縦型値ではない） |
| R02 | Render開始・停止・進捗確認 | 書出し実行 | 検証済みA | mcp-fit.json render-job-lifecycle（CompletionPercentage=100＋output存在＋delete） |
| R03 | Render出力の即時検証 | 生成物のspec検査 | 検証済みA | journal 20:18:02 検証済みA: verify_output実走（output_probe・duration_ratio。verified_flag falseはlocale差異blockerとして記録済み）。旧記述「verify_output未使用」は陳腐化。 |
| R04 | Deliverページ固有設定をGUI操作 | APIにないcheckbox等 | 未検証 | チェックボックスの所在とAX読取（値・座標）は確認（ネットワーク最適化/チャプター生成/縦型解像度）。ただし合成クリックでは1つもトグルできず（4試行、ボックス領域のピクセル差分0）— 本セッションの自動操作は不調。人手操作は当然可能。なおrender.get_settingsが空dictを返すbuildのため、GUIトグルのAPI読み戻し検証も不可 |
| R05 | 納品QC・Compliance | 規格照合 | 検証済みI | journal 21:28:09 検証済みI: V5+U11証拠の合成でper-field pass/fail判定。※offline経路（単独のinterchange走行ではない——I定義との適合は要確認）。 |
| R06 | 字幕出力検証 | burn-in/embedded/sidecar確認 | 検証済みA | journal 20:56:49 検証済みA: burn-in画素差分3638px+sidecar実証（S10と同一証拠）。 |

### Q. 解析・編集判断（TikTok系に直結するもののみ）

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| Q01 | Timeline Marker・review annotation | YouTubeチャプター生成（R04のチャプターcheckboxと直結）・レビュー往復・自然言語修正指示の目印 | 未検証 | 索引行Q01は省略リストにあったが2026-09-07レビュー+Fable指摘で復帰。API面: timeline_markers.*/timeline_item_markers.*/export_review_report（索引記載、未試行）。Source clip markerはM05。 |
| Q03 | Scene Cut Detection | cut検出（jump-cut補助） | 未検証 | detect_scene_cutsを2タイムラインで実行しsuccess=True（journal 19:54:20/19:55:06）だが、検出cut数・markerのreadbackなし＝API受理のみ（verification.status=unverified）。検証済みAの要件未達のため格下げ。 |
| Q06 | Editorial plan・Selects・Silence edit案 | 冒頭10秒まとめ（wishlist #2）の判断材料 | 未検証 | edit_engine.plan_selects 自体は存在するが解析DBが前提。本機はローカルtranscription backendが未導入で解析パイプラインが起動不可（no_auto_install方針）— backend導入後に再挑戦。v4.3のselects probe失敗と同じ所在 |


### N. レビューで追加発見（2026-09-07監査——suda索引に不在、高頻度のみ抽出）

機能IDはレビューで新設（N接頭辞）。すべて未試行。低・中頻度の残り約60項目は本レビュー報告参照。

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| N01 | スナッピング on/off | 人間編集の基本トグル（自動化は座標指定のため影響小） | 未検証 | 2026-09-07レビュー: 編集基本監査で完全欠落と判定 |
| N02 | Ripple delete・ギャップ詰め | 間詰めpacingの最頻出操作（T05/T06が部分対応だが明示行なし） | 未検証 | 同上 |
| N03 | Linked selection・音声分離(detach) | 音だけ差し替え・詰めの常見ワークフロー | 未検証 | 同上 |
| N04 | アジャストメントクリップ | 全カット一括ルック（style ①一括モノクロ等） | 未検証 | 2026-09-07レビュー: color監査で完全欠落 |
| N05 | フリーズフレーム | TikTokズーム演出の部品（T14根拠noteにsuspended言及のみ） | 未検証 | 編集基本監査 |
| N06 | ソースIn/Outマーク | 素材区間指定の基本（T01の配置が代替するがマーク自体の行なし） | 未検証 | 同上 |
| N07 | セーフエリア/グリッドオーバーレイ | 9:16縦型テロップ安全域の確認 | 未検証 | 同上 |
| N08 | グリーンスクリーンキーイング（Delta/Ultra/3D/Chroma） | 背景差替え（wishlist #4）——F10不可の唯一の現実的代替経路 | 未検証 | 2026-09-07レビュー: color監査でCRITICAL判定 |
| N09 | 映像ノイズリダクション（temporal/spatial） | 暗所スマホ素材の必須級 | 未検証 | 同上 |
| N10 | 顔補正/Face Refinement・顔トラッキング | 顔出しクリエイターの定番beauty | 未検証 | 同上 |
| N11 | Retime品質設定（Optical Flow/Speed Warp） | スローモーション品質（T12/T15は速度値のみで品質モード未扱い） | 未検証 | 同上 |
| N12 | カラーマネジメント（RCM/ACES） | 素材混在時の色一貫性の土台 | 未検証 | 同上 |
| N13 | Text+深度スタイリング（縁取り/グラデ/カーニング/行間） | 太字字幕の生命線——S01は本文+Size+色のみ | 未検証 | 2026-09-07レビュー: titles監査でCRITICAL判定 |
| N14 | 絵文字/ステッカー/グラフィック素材 | TikTok字幕の頻出装飾 | 未検証 | 同上 |
| N15 | テキスト背景プレート（角丸ボックス・色帯） | 読みやすさの要 | 未検証 | 同上 |
| N16 | ロゴ/ウォーターマーク常時overlay | 収益面で常時使用（M01+T01+F02の統合workflow行なし） | 未検証 | 同上 |
| N17 | Fusionタイトルテンプレート適用 | 既成アニメ付きタイトル（F03/F04は素node構築のみ） | 未検証 | 同上 |
| N18 | 音声クロスフェード/Jカット/Lカット | 声コンテンツの継ぎ目処理の基本（T09/T10は映像のみ） | 未検証 | 2026-09-07レビュー: audio監査でCRITICAL判定 |
| N19 | クリップ端フェードハンドル | BGM/SFX出入りフェード | 未検証 | 同上 |
| N20 | 波形/タイムコード自動同期 | 別録りwavと映像の同期の必須工程 | 未検証 | 同上 |
| N21 | 音声ノイズリダクション・Dialogue Leveler | 部屋ノイズ・声量ムラ（U02 Voice Isolationは別機能） | 未検証 | 同上 |
| N22 | clip color/flag・bin整理・メディアプール検索 | レビュー・素材整理の基盤 | 未検証 | 同上 |
| N23 | タイムライン複製/snapshot退避 | 破壊的操作前の保険 | 未検証 | 同上 |
| N24 | XML/AAF/EDL interchange往復 | 他NLE・長期保管（T02は.drt/.drpのみ） | 未検証 | 同上 |
| N25 | In/Out範囲render・VBR/CBR品質・音声format/ch指定・複数timeline一括render | YouTube+TikTok両納品の毎回操作（R行に存在せず） | 未検証 | 2026-09-07レビュー: delivery監査でCRITICAL判定 |
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
| V1 | Timeline解像度 | tall 解像度への設定変更（A04/A05 route。timelineFrameRateはtimeline作成後にlockされる実測 — vendor trap。（実証: journal 21:12:07 timeline_resolution 1080x1920書換+readback、rendered_dims [1080,1920]） | 検証済みA |
| V2 | 素材扱い | rotation flag付きphone footageは既に縦（reframe禁止 — vendor trap）。VFRはtimelineがconformしたFPSに合わせる（r_frame_rateを見ない） | 未検証（trapはvendor実測の引用。我々の縦型素材での確認なし） |
| V3 | 構図・テロップ・字幕サイズ | 全部変わる。現行承認値（テロップ2階層・字幕5.5%/W5・中央配置）は1920x1080前提（style-vocab A節＋presentation-profile-default.json）（実証: journal 21:22:20 縦型frameでのtelop+subtitle帯render A/B差分608行） | 検証済みA |
| V4 | Render設定 | 1080x1920での設定・実行（R01/R02 route）。vendorには `tiktok`/`reels`/`shorts`→`h264_vertical_1080_web`（1080x1920）のdelivery target aliasが存在する（`private/vendor/davinci-resolve-mcp/src/utils/delivery_targets.py`）が、**sweep 2026-09-07で使用し実証済み**（journal 21:27:23: h264_vertical_1080_web解決→1080x1920 h264 render実証） | 検証済みA |
| V5 | QC | render-qc.json 1920x1080固定の作り替え（R05/R06 route）（実証: journal 21:27:24 deliverable_qc実走・ffprobe実測 h264/1080x1920） | 検証済みI |
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
