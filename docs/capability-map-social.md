# TikTok/Reels/Shorts系 能力地図（systematic capability map, Phase 1）

sudaさん指示（2026-09-06）・Opus構成で作成。目的はひとつ: **sudaさんが新しい動画
スタイルを試すとき、ダビンチエージェントのシステム起因で検証が入ったり止められ
たりしないよう、領土を事前に地図化する**。sudaさん原文:

> 私は新しい動画スタイルを試すときに、ダビンチエージェントのシステム起因で検証が
> 入ったり止められるのが嫌なの。事前に貴方やsolが検証をすべき

2026-09-07 レビュー（suda指示・10エージェント監査+Fable指摘）: 判定修正10件・根拠更新22件・高頻度欠落の新規行30件（N接頭辞・索引に不在。内5件はFable指摘2で追加）を追加、索引省略行からQ01/M08の2件を復帰。
2026-09-07 vlog/長尺再審（suda指示・8エージェント）: 新規23行（N31〜N53）・索引11行復帰・用途文8行の両対応化。マルチカムは不使用回答で省略維持。
2026-09-07 QW3 batch（宣言⑲）: 4行を検証済みAへ（A02/A03/T16/N42）。Q03は読戻しを完結させたが検出marker増加ゼロで未検証維持。N26はkeyframe API自体がサーバ側エラーで未検証維持。Q02はbackend不在の環境記録を根拠に追加。A03のarchive legのみ測定不能（bare false）。
2026-09-07 QW4 batch（宣言㉑）: 4行を検証済みAへ（N34/N28/M12/N44＝N44はpartial）。N25はjob生成＋読戻し検証済みA／品質・寸法キー live書込拒否で未検証の複合へ。Q08はgrade_loopがserver側numpy不在で未検証維持。N40はPan書込write=falseで未検証維持。N49はcolorScienceMode書込拒否（davinciYRGBのまま）で未検証維持。
2026-09-08 capE retry batch（共通操作quick・単独インスタンス）: N02を検証済みAへ（timeline.delete_clips ripple対比を数値で完遂——batch Dの未完対比を解消）。N18/N19は未検証維持だが対比データを追加（N18ハードカット境界のe440/e880階段状切替・N19バッチフェード適用前後RMS完全一致）。N05はタイムラインメニューに「フリーズ」項目なしを実測。双子インスタンス（capE_95913・marker無視）が並走したがTWIN-DEFENSE名前アサートで自projectを防衛、死亡確認後にmarker take over。
2026-09-08 batch E 第2インスタンス(capE_95913・twin BATCH_END 09:49:11確認後にmarker takeover): N03を検証済みCへ。N05/S08/N18/N26はtimebox未達の正直skip（journal 2026-09-08T10:09-10:11）。S08は字幕トラックAPI作成まで記録（cue不生成）。
2026-09-08 capL batch（capL_501+capL_99701・2インスタンス逐次・N26 FINAL route）: N26は「クリップ右クリック→キーフレームエディタ」経路の不存在を2インスタンス独立実測で確定（メニュー全ラベル実測×2、overlay到達不達、補間メニュー未到達）——C経路未試行維持。新規UIクセ2件実測（数値欄コミットはTABのみ/return不発・API get_propertyはplayhead非依存）。
2026-09-08 capM batch（capM_33096・15min hard cap・N26 diamond-right-click researched）: manual抽出で正規経路を特定（Inspector橙ダイヤ右クリック→Ease In/Out/In and Out/Linear＝Ch.60 p.1290／Curve Editorツールバー＝p.1301-1302——クリップ右クリック経路はmanualに記載なし＝capJ/Lの否定はmanual通り）。GUI実測は+5キー赤点灯まで確定（ZoomX=1.000）も+65の1.5値セットがIME予測変換に2回妨害され2点化不達、橙条件未充足のまま右クリックはメニューなし（無効試行）。N26はC経路未試行維持（次run手順まで特定済み）。
## 冒頭集計 — 全 141 項目（2026-09-08 capE retry反映後・実測）

| 状態 | 件数 | 意味 |
|---|---|---|
| 未検証 | **31** | 一度も試していない、または記録済みブロッカー付き（実素材・基盤・映像反映問題等） |
| 検証済みA | 53 | API書込＋読み戻しで検証済み |
| 検証済みI | 19 | interchange・offline経路で検証済み |
| 検証済みC | 9 | 画面操作・座標経路でlive検証済み |
| 複合状態 | 14 | 1行に2状態併記（部分検証の分割表示） |
| C経路未試行 | 4 | API/interchange試行済み・GUI操作は未試行 |
| 不可 | 1 | 到達不能と測定 |
| 未決定 | 1 | 判断未了 |
| **計** | **141** |  |

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
filterに derive した。表にない索引行（T18/T19、U07、M03、M09〜M11、
A06/A07、O02〜O04、Q04/Q05/Q07、R07）は縦型・短尺の通常
編集で直接使わないため省略した（省略自体が判断であることを明示する。Q01・M08はFable指摘で、Q02/A01〜A03/C10/M02/M04/M05/M12/O01/Q08はvlog再審で復帰。M09/M10マルチカムはsuda回答「使わない」で省略維持）。

### A/M. プロジェクト・素材（縦型の前提＋素材搬入）

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| A01 | 接続・起動。「Resolveへ接続」「Editページを開いて」 | 全作業の入口（毎session） | 検証済みA | journal 2026-09-07 QW batch（宣言⑰）検証済みA: get_version（Studio 21.0.4.5）→ deliver→edit→color→edit のopen_page往復をget_pageで全段readback一致。API-only（click/CUなし）。旧記述（形式検証未整理）は本実証で解消。 |
| A02 | Project一覧・作成・Load・Save・Close | エピソード毎projectの作成・保存——制作の背骨 | 検証済みA | journal 2026-09-07 10:25:26 QW3 batch（宣言⑲）検証済みA: list(9)→safe_project_create('__fvp_test__sol_qw3_tmp', id 146fb0d5-acbc-475a-a63c-cacbab4bcb46)→load→get_name一致→save→close→list(10)→crouteへload→get_name一致→safe_project_delete(dry_run後real)→list(9)。注意: safe_project_create/deleteは名前が_mcp_始まりでないとallow_non_mcp_name=Trueが必須（実測）。未保存dialogはCU/ユーザー対応のまま範囲外 |
| A03 | Project Export・Import・Archive・Restore | 納品後のArchive保存・危険操作前バックアップ（一人作業の保険） | 検証済みA | journal 2026-09-07 10:29:08 QW3 batch（宣言⑲）検証済みA: safe_project_export→qw3_export.drp 1,115,685B→safe_project_import('__fvp_test__sol_qw3_restored')→listで照合読み→delete掃除。**archiveのみ不可实测**: safe_project_archiveはdry_run would_archive=Trueに対しreal実行がbare success=false（message無し、path直下/サブdir両形、10:26:50/10:26:58）。ArchiveはGUI経路のままC経路未試行 |
| A04 | Project設定の読替・変更 | 縦型9:16（解像度・fps）の設定変更。前提条件ブロック参照 | 検証済みA | journal 20:05:11 検証済みA: 解像度 1920x1080→1080x1920 書換+readback実証（write+readback）。旧記述（v4.3 probeはfps=30のみ）は陳腐化。 |
| A05 | Timeline設定の読替・変更 | 縦型Timelineの tall 解像度設定。前提条件ブロック参照 | 検証済みA | project_settings.set_setting（timelineResolutionWidth/Height→1080x1920、readback OK）→新規タイムラインが縦型を継承→レンダリング出力も1080x1920を確認。**既存タイムラインの解像度は timeline.set_setting で変更不可（success False 実測）— 作成前のプロジェクト設定かGUIで変更** |
| M01 | 素材Import | 縦型素材・写真素材（wishlist #3挿入カット）の搬入 | 検証済みA | `video-pipeline/capabilities/v4.3/mcp-fit.json`（import-media: 3 clips＋media-pool ids読戻し、status accepted） |
| M02 | Bin作成・移動・整理 | 毎エピソードの素材整理（footage/BGM/GFX分け）——取り込み直後の定型 | 検証済みA | journal 2026-09-07 QW batch（宣言⑰）検証済みA: add_subfolder('EP_test')→get_subfolders読戻し→organize_clipsで1clip移動→get_clips読戻し一致（disposable project、API-only）。Smart Bin/Power Bin新規はCUのまま範囲外。 |
| M04 | Metadata・Clip Name・Reel Name | clip名の意味付け・解析metadata書き戻し（素材検索の土台） | 検証済みA | journal 2026-09-07 QW batch（宣言⑰）検証済みA: set_name→get_name完全一致、set_metadata(Keywords=qw-test)→get_metadata完全一致（disposable clips、API-only）。注意: 字幕clip（srt）はKeywords/Keyword/Descriptionのset_metadataを3回とも拒否（False）——metadata書込はwav clipで実証。 |
| M05 | Clip Marker・Flag・Clip Color・Mark In/Out | よい場面を素材側に記すテイク確認・selects範囲指定（Q01の素材側対応） | 検証済みA / 未検証 | journal 2026-09-07 QW batch（宣言⑰）: **Marker・Clip Color・Mark In/Out=検証済みA**（markers.add(frame0/Blue/qw)→get_all完全一致→delete_at_frame、set_clip_color(Orange)→get Orange、set_mark_in_out(0,24)→get audio in=0/out=24→clear。disposable wav clip、API-only）。**Flag=API書込不発と測定**（宣言⑱: add_flagはTrueを返すのにGetFlagsが空のまま——raw API levelでも同一、Orangeは3clipでhard-False。16色paletteにOrange不在も判明。**人間/CUの右クリック経路は未試行**——「API不発」を実現不能と混同しないこと。判定は未検証維持）。 |
| M06 | 既存ProxyのLink/Unlink | 4K縦型の重い素材を軽く扱う | 検証済みA | journal 20:10:48 検証済みA: link_proxy書込+Proxy属性readback（Proxy=1920x1080・Proxy Media Path設定確認）。 |
| M07 | Proxy/Optimized Media新規生成 | 同上（生成はCU route） | 検証済みC / 未検証 | **Optimized Media生成=検証済みC**: Media Pool右クリック→「最適化メディアを生成」で6.8GB・8,467キャッシュファイル実生成、進捗ダイアログもAX読取可（journal 03:57:38）。**Proxy生成=未検証**（「同メニュー系」という推定のみ——Fable指摘4で分割併記）。APIはリンク系のみで生成actionなし＝生成はGUI起動 |
| M08 | Relink・Replace source clip | メディアオフライン時の再接続・素材差し替え（プロジェクト移動・素材整理で必須の復旧操作） | 検証済みA | journal 2026-09-08T02:10:54 capA batch（suda指示・API-only）: replace_clip(disposable clip, qw4_720p60.mp4)→success・File Path readback完全一致（edit-source.mov→qw4→復帰の往復実証）＋safe_relink dry_run=Trueがplan返答（1clip一致・missing=[]・非変異）。replace_clip_preserve_sub_clipは未試行。TimelineのReplace Editとは別。 |
| M12 | Media templateを保存・再利用 | offline DRT構築（T02）をrender可能にする前提処理 | 検証済みA | journal 2026-09-07 10:48:28 QW4 batch（宣言㉑）検証済みA: capture_media_template(edit-source.mov)→success（cache 37511352524fd9ef…json・media_ref 461f8e1c・pool_bytes 5689・resolve 21.0.4.5）→file 10,585B・mtime更新・keys 10種確認。前後get_currentとも__fvp_test__sol_croute_20260906完全一致＝内蔵scratch-project往復が正しく復帰。API-only |

### T. Timeline構築・カット・配置・Retime

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| T01 | 新規Timelineを素材から構築 | アセンブリ、写真挿入カット（wishlist #3）の配置 | 検証済みA | `video-pipeline/capabilities/v4.3/mcp-fit.json`（exact-source-range-placement: 配置＋source/record span読戻し／project-timeline-creation: 作成＋current読戻し、共にaccepted） |
| T02 | 新規DRT/DRPをoffline authoring | 再構築方式の切断・再配置全般 | 検証済みI | journal 19:47:10 検証済みI: drt.assemble+import実走、authored構成の着地をreadback（fps含む）。 |
| T03 | Track追加・削除・名前・Lock・Enable | 字幕・テロップ用V2/V3 track確保 | 検証済みA | journal 19:35:59 検証済みA: track 2→3→2追加/削除、名称「sag_telop」設定、lock true・enabled falseを全てreadback。 |
| T04 | Clip/RangeのCopy・Move・Duplicate | まとめ切り抜き（wishlist #2）の切貼り | 検証済みA | journal 19:46:24 検証済みA（API route: append+delete primitivesによるcopy/move/duplicate配置をreadback）。interchange走行はゼロ——旧I表記は誤り。vendor trap（issue #74）はAPI insert系の別問題。 |
| T05 | Ripple Insert / Timeline Ripple | 詰め編集・挿入 | 検証済みI | journal 2026-09-08T02:16:04 batch A: interchange authoring route（drt.assemble はMCP経路から到達不可・.drtはbinary zipで手組は過去実測failのためFCPXML使用）。3clip timeline→FCPXML 1.10 export→中clip削除＋ギャップ詰めの手組variant→import success（media 2/2 linked/0 offline）→structure読戻し: 2items完全隣接[172800,172824)+[172824,172848)・2nd item source_start=60（3rd clipのsource維持）。ripple意味論がinterchange往復で成立。N02のripple-deleteも同機構。 | journal 2026-09-08T02:17:39 batch A(drt側・別インスタンス): drt.assemble経路も実走（advanced MCP drt actionはnode経由で到達可）— capA_t05_ripple_30fps.drt 37384B→import success(4/4 linked)→読戻し B.start==A.end==86550 隣接・gap close authored通り。source offset 400→500=決定論的×1.25（T08の600→750と同一FPS_SPACE_CORRECTION）。2経路とも検証済みI。
| T06 | Overwrite・Lift・Range置換 | 区間差替え | 検証済みA | journal 19:42:05 検証済みA: overwrite/lift実行後のitem配置をreadback。 |
| T07 | Blade/Razor/Split | ジャンプカット（TikTok系の高頻度技法） | 検証済みI | journal 19:48:09 検証済みI: drt.assembleでsplit構成を実authoring+import、split_record_exact=true / source_halves_distinct=true。※drp.split_clipは未試行。 |
| T08 | Trim/Slip/Slide/Roll | 間詰め・尺調整 | 検証済みI | journal 19:48:09 検証済みI: drt.assembleでtrim/slip構成を実authoring、rec 100f exact・slip src 600→750の決定論的変換をreadback。 |
| T09 | Transitionを新規・再構築Timelineへauthoring | wipe/dip/glitch等（cross-dissolve以外全部） | 検証済みI（cross-dissolveのみ） | journal 2026-09-08T02:17:37 batch A: FCPXML手組 `<transition name="Cross Dissolve" duration=12/24s>`→import success→structure読戻しで遷移実在確認（クロスディゾルブ item 12fがカット点に出現、clip A source 0-30→0-45に伸長=遷移分消費）。**タイプ忠実性の限界を測定**: name="Dip to Color Dissolve"もimportされるがクロスディゾルブに強制変換読戻し——wipe/dip/glitch等の非cross-dissolveタイプは本経路では型が保持されず未検証のまま。 |
| T10 | Transitionを開いているTimelineで追加・変更 | 同上（既存Timelineへの追加） | 未検証 | journal 2026-09-08T02:17:37 batch A再確認: timeline toolのfull action列挙（Unknown-actionエラー応答、2.210.0）にtransition追加/変更opは存在しない（insert_generator/title/ofx/fusion等のみ）。v4.3 transition-path probe failedの前回実測と整合。CU経路は未試行（本batchはAPI-only）。 |
| T11 | 既存Transitionの検出・QC・削除 | 既存遷移の確認・除去 | 検証済みA | journal 2026-09-08T02:17:37 batch A: 検出=probe_timeline_structure/get_itemsが遷移をitemとして露出（クロスディゾルブ・12f・media_pool_item null・kind=generator）。削除=timeline.delete_clips([遷移item id], ripple=false) success→get_items読戻しで2clip無傷・遷移消失（capA_t09b_dip上）。QC（属性読みの詳細）は未検証。 |
| T12 | 一定速度Retime | スロー・倍速（速度演出の基本） | 検証済みC / 検証済みI | C: `private/runtime/sol-input-mech-20260906/summary_report.txt`（item6_run5b_VERIFIED_WORKFLOW_SUCCESS: 25%をverified-button workflow＋render cadence SSIMで確認）。I: 同（item6_run1b: .drt経由50%をrender cadenceで確認。ただし変更ボタン押下の帰属補正 CORRECTION_item6_run1b_attribution あり） |
| T13 | Reverse | 逆再生演出 | 危険(.drt)/未検証 | .drt経路: `private/runtime/sol-input-mech-20260906/summary_report.txt`（RECORD_reverse_crash_verbatim: reverse（cuts[].reverse）の.drt importはResolve 21.0.4.5でクラッシュする（実測1回）。再現確認は費用対効果から未実施）。GUI経路: 2026-09-07 両ラウンドで到達点更新も未達成。第1ラウンド（15steps）: speed dialogのreverse checkboxをverified pressで押下したがAX読取はval=0のまま（journal 04:14:10）。第2ラウンド（4th agent）: Inspector/速度ダイアログの逆再生checkboxをAXPressで**val=1まで到達・確認**（初）→「変更」押下でダイアログは閉じるがrenderは一切不変（5記録点を全ソースフレーム照合してマッピング導出: src≈1.25×kの125%順方向のまま、RetimeProcess=0のまま）＝checked状態はclipの再生方向にコミットされず未検証維持。逆再生の読み戻しはduration/source extentが判定不能（既存125%clipと混同注意）で、render順序照合が唯一の確定手段（journal 05:05-05:35） |
| T14 | 可変Speed Ramp | スピードランプ演出 | C経路未試行 | 2026-09-08表記正直化（suda承認）: 未検証→C経路未試行（API側の否定/不発は実測済み、画面操作経路が未試行のため）。journal 2026-09-08 batch A（2つの具体的否定）: (1) API側に速度・ramp書込経路なし——set_retimeはprocess/motion_estimationのみ、get_property(Speed)=null。(2) interchange側: FCPXML `<time-map>` 50%速度clip（timeline 1s→source 0..2s）をimport→エラーなし但しstructure読戻しでsource 0-30のまま= timeMapは無視されretime不適用。API-only面からは到達不能。freeze suspended note（summary_report.txt RECORD_reverse_crash_verbatim）は変わらず。 |
| T15 | 既存clipのRetime対話編集 | ramp・easingの手直し | 検証済みA（process・品質設定のみ） | journal 2026-09-08 batch A: set_retime(process/motion_estimation) 書込→get_retime読戻し一致（N11と同証拠）。但しramp・curve・easingの手直しはset_retimeの守備外（速度値書込経路なし、get_property(Speed)=null）——その部分は未検証のまま。CU routeは本batch範囲外。 |
| T16 | Stabilize / Smart Reframe | 手ブレ・縦型reframe | 検証済みA | journal 2026-09-07 10:24:42 QW3 batch（宣言⑲）検証済みA: stabilize(item success, archive v8)前後で同frame 86500を単.frame mp4 render→ffmpeg 480p gray比較: mean_abs_diff 2.076・max 144・3.58%画素>10lev（before 24047B/after 23660B）。※timeline_frame captureは本機localeで「完了」vs Complete判定不整合のためRENDER_FAILED誤爆（Plan Bの明示render jobで実証）。Smart Reframe自体は未検証 |
| T17 | Compound / Fusion Clip作成 | 複合演出の1object化 | 検証済みA | journal 19:49:48 検証済みA: Fusion Clip 1作成をitem一覧+spanでreadback（Compound Clipも同様に名称+span readback）。 |

### F. Transform・Edit Effects・Fusion・Magic Mask

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| F01 | Inspector Transform/Crop/Composite | ズーム・位置・回転・クロップ・不透明度（パンチインの基本） | 検証済みA | `private/runtime/sol-matrix-20260906/summary.md`（#1 Zoom 1.0→1.35／#2 Position／#3 Rotation 5.0／#4 Crop 64／#5 Opacity 80、いずれもget_*読戻し）＋mcp-fit.json clip-transform-punch-in probe |
| F02 | propertyのkeyframe | **スムーズなアニメatedズーム**（Transform zoom静止画はAだが動きはここ） | 検証済みC | 検証済み（2026-09-07 C-route: Inspector ビデオタブのdiamondで86400:1.0→86849:1.5。render A/B: a=baseline同一(PSNR inf)/b=980,942px差分/c=1,217,639px差分の単調プログレッション、視覚確認済み。タブ切替はhover-dwell 1.5s必須（即時click・AXPress・AXSetValは全部不発）。API add_keyframeはNoneType破損、Fusion compは21.0.4でrender inert）※実証は直線(Linear)のみ——イージングはN26として未検証（Fable指摘2） |
| F03 | Fusion Comp追加・Import/Export・切替 | Text+/エフェクトの土台 | 検証済みA | mcp-fit.json fusion-template-insertion（insert＋comp count=1読戻し、accepted）＋matrix #8（insert_fusion_title→set_text_plus読戻し） |
| F04 | Fusion node追加・削除・接続 | モーション系の下地（Blur/Transform/Merge/Mask） | 検証済みA | fusion_comp API（add_comp→add_tool→connect→set_input、スコープは timeline_item={track_type,track_index,item_index} ネスト必須）でBlur構成を作成→render A/Bで実ブラーを確認（全ピクセル差分、平均保存・分散減 = 本物のブラー）。削除・再接続も成功。注意: 2026-09-06には同一API経路でrender失敗（Fusionコンポジション処理エラー）を計測済み — 条件不明だが普遍ではない |
| F05 | Fusion parameter・animation | RPG風地点表示の速い出入り（style ③）等 | 検証済みA | fusion_comp add_keyframeでXBlurSize 0→40アニメーションを作成、render A/Bで2地点の出力差异（blur0: std25.7 / blur40: std21.4）を確認＝アニメーションはレンダリングに反映される。**要件事項: キーフレーム時刻はコンポローカル時間（クリップ先頭=0）必須。タイムラインレコードフレームを渡すと範囲外で黙って無視される（2026-09-06の「render不反映」計測はこの時間基準違いが原因）** |
| F06 | Edit ResolveFX/OpenFXをclipへ追加 | glitch等の質感エフェクト付与 | 未検証 | batch C実測: EffectsライブラリからのResolveFXドラッグを計画したが、双子インスタンスがページをDeliverに切り替え続けたため未試行のまま時間切れ。run_inline経由の1frame render経路は動作実証済み（N16/N31/N35の画素証明）— render A/BによるF06効果証明の道は開いたまま〔旧記載:  APIにfx追加actionなし（確認済み）。C経路: エフェクトライブラリを開いてResolveFXドラッグを3方式で試行したが合成イベントではドラッグ&ドロップが登録されず（render A/Bで効果なしを確認）。手動操作は可能と推奨されない理由なし — 人手またはUI自動化の別手段で再挑戦余地あり〕 |
| F07 | Edit FXのInspector parameter調整 | 同上の調整 | 未検証 | F06に依存（追加できたResolveFXが作業タイムラインに存在せず未検証）。〔batch C 2026-09-08 capC_19519: F06未達のためInspector調整・render A/Bとも未実行。InspectorのAX列挙自体は可能（Audio tabのフィールドは視認済み）〕 |
| F08 | OFX Generator挿入 | 独立Generator clip | 不可 | 本機にOFX generatorプラグイン未インストール（/Library/OFX・~/Library/OFXとも不在を実測）。API挿入は失敗、ドラッグ対象も存在しない。独立Generatorはnative generator（insert_generator成功済み）で代替可能 |
| F09 | Color node内OFXをoffline編集 | Color内エフェクトの構造編集 | 未検証 | journal 2026-09-08 batch A: offline drx authoring経路がMCP側に到達不可（dctl=FCTL用・group_settings=Fusion .setting用でOFX .drx対象外）——honest stay。 |
| F10 | Magic Mask | 背景差替え（wishlist #4）・被写体分離（style ①モノクロ演出） | C経路未試行 | batch C: 合成素材（testsrc2/移動矩形）に人物がおらず、Magic Maskのsubject strokeクリックが意味を持たないため未試行。実素材+HITL前提の行として維持〔旧記載:  API実測: create_magic_mask→needs_hitl=True（subject clickはAPIから生成不能、journal 2step）。Color pageで人間が被写体をクリックする経路は未試行——U06/U09と同型（人間操作前提なら到達可能性が残る）。〕 |

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
| S08 | Subtitle track全体のstyle | 検証済みA | 検証済みC | 【2026-09-08 suda本人確認で確定】トラック一括スタイルのサイズ欄はGUIで変更可能。操作法: Inspectorビデオタブ＞トラック＞サイズの数値欄は**ダブルクリックで編集状態に入り数字入力**（単クリックでは入力不可——自動操作3回失敗の原因）。値変更は自動でも実証済み（58→72→60、画面証拠あり）、視覚確認はsuda本人が60→200で実施（字幕が明瞭に拡大）。前史: 選択方法は色解析クリック+Inspectorヘッダー「Subtitle - 字幕」で確立、S09ハザード（字幕へのAPI書込）は全経路で回避。batch C: 時間切れで未達（字幕トラック作成まで届かず）〔旧記載:  未検証（Advanced project_db。Resolve完全終了必須）〕2026-09-08 batch D（共通操作quick・GUI/CU経路・双子競合下）: ベース生成は成立—API timeline add_track(字幕)成功＋CreateSubtitlesFromAudio=True（旧S03不発を覆る |
| S09 | Native字幕ごとのstyle/位置調整 | 1件だけの強調 styling | 検証済みC（GUI限定） | 【2026-09-08 capJ_29468 double-click set】キューごとの強調をGUIで達成: cue2を選択（タイムライン色解析クリック(550,615)→ヘッダー「Subtitle - 字幕」）→ Inspectorビデオタブ＞キャプション＞「キャプションをカスタマイズ」チェックボックスON → キュー専用スタイル欄（フォント/フォントフェイス/カラー/サイズ/大文字小文字/配置/位置X-Y）が出現 → **サイズの数値欄をダブルクリック（キャレット+赤リング=編集状態）→ 120入力+return**。値はAXで200→120確認、**選択解除→再選択→キャプションタブ再確認でも checkbox=1 + サイズ=120 が持続**。視覚証明: cue1「(beep)」@200 のグリフ帯42px vs cue2「Alpha Bravo…」@120 の27px（比0.64≈120/200、PIL白画素測定、screenshots/capJ_cue1_after.png・capJ_cue2_after.png）。UIクセ: テキストエリアへのコミットクリック後チェックボックスが一時 val=0 表示になり、再トグルで stored 120 が現れた（値自体は常時持続）。キュー1はカスタマイズOFFのまま（トラック既定）。**危険（変更なし）: 字幕アイテムへのscripting-APIプロパティ書込（SetProperty。inline・MCP server両経路）はResolve 21.0.4.5をハング→クラッシュさせる（3/3再現、読取は安全）——本経路はGUIのみで書込禁止ハザードを回避**。なおDeliverレンダリングは字幕を焼き込まない（証明はビューア画面測定で実施）。【2026-09-08 capK_47168 再検（dead-marker takeover後）】**値の持続を死んだセッションを跨いで再確認**: capJ死亡後のGUI再接続で checkbox=ON + サイズ=120 が残存、120+returnを再コミット（ダブルクリック編集状態=選択+キャレット+赤リングを再現）。トラックサイズを200→60にリセット（S08手法）した後の手前再測定: cue2グリフ帯33px（PIL白画素行プロファイル）≈capJの27px帯であり、**もしトラック60が適用されているなら約14pxになるはずなので、per-cue 120レンダリングが生きていることと整合**。注意: 同一セッション内の60pxベースライン(queue1@60)はバー背景+ビューア白線で汚染し計数不能——33pxの解釈はcapJ対比による間接証明にとどめる。UIクセ追記: ビューアでキャプションをダブルクリックするとテキスト編集オーバーレイが立ち上がり、**その間ビューアの描画が固まる**（ESCでは解除不可、ビューア空押しクリックで解除） |
| S10 | 字幕付き納品（Burn-in/embedded/sidecar） | プラットフォーム別字幕納品 | 検証済みA | journal 20:56:49 検証済みA: burn-in画素差分3638px+TTML sidecar納品を実証。embedded caption modeは未試行。 |

### C. Color・Grade・Scope

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| C01 | LUT/CDL/DRXを既存clipへ適用 | LUT・film-look・saturation pop適用 | 検証済みI | summary_report.txt CDL_DRX_INTERCHANGE_ROUTE（drx.generate lift→safe_apply_drx→render変化 2073597/2073600画素。**値校正の注意**: drx lift 0.05はGUI lift 0.05の+21.94ではなく原画近傍に着地。vendor CALIBRATION-STATUS/DRX-VALUE-SCALING未校正）＋mcp-fit color-grade-preset-drx accepted |
| C02 | Grade Copy・Version・Restore | cut間look統一 | 検証済みA | グレード適用（25万px差分）→別クリップへCopyGrades（29万px差分＝カット間ルック統一）→バージョン保存・復元（identityとgradedを行き来しrender両方向で実証）すべてAPI＋render A/Bで確認。注意: safe_set_cdl/safe_copy_gradeラッパーは一部動作不良（生APIとinline CopyGradesを使用）。バージョンAPIのtypeは文字列でなく整数（0=local） |
| C03 | Gallery Still・LUT Export | look保存・持越し | 検証済みA / 未検証 | Gallery Still取得・書き出しは実証（sweep_still_1.1.1.drx+png生成、journal 20:05:10）。LUT書き出しは失敗実測（lut_exists=false・0 bytes）——LUT Export分は未検証（失敗記録あり）。 |
| C04 | Primaries/Curvesを画を見ながら調整 | lift等（強調区間の背景モノクロ/黒 — style ①複合演出の映像側） | 検証済みC | summary_report.txt item11_REMEASURED（Color page lift数値field→画素diff 2073597/2073600変化・平均輝度35.48→57.42。matrix午前の0画素は座標miss artifactと確定） |
| C05 | Primaries/Curves/Node treeをoffline authoring | 決定済みgradeの構造生成 | 検証済みI | C01と同一証拠（generate＋apply実証済み。値スケール校正が残作業） |
| C06 | Power Window/Qualifier/HDR/Blur/Key | 被写体だけ残し背景モノクロ（style ①）・背景ぼかし（wishlist #4系） | 検証済みC / 未検証 | **Power Window=検証済みC**: circle Window追加→彩度0 grade、render A/Bで実証（journal 00:32:44）。**Qualifier/HDR/Blur/Key=未検証**——グリーンスクリーン合成（N08）はまさにQualifier/Key側で未試行。本行を実証済みと誤読しないこと（Fable指摘4）。実測: gallery stills PNGはgradeの視覚証拠にならない（0px差）— grade証明はrender A/Bのみ |
| C07 | Tracker / Color Warper | モーショントラッカー正対の地点表示（style ③） | 検証済みC / 未検証 | **Tracker=検証済みC**: 追従ウィンドウのフレーム毎変位を実renderで実証（純追跡差分100/99px vs ビット一致ゼロ床、sat再飽和クラスタmid→end進行、journal 01:56:11）。**Color Warper=未検証**（グリッドドラッグ未実証——根拠の併記明記はFable指摘4）。track開始クリックがsilent-failする例あり（playhead進行で要確認）。素材がほぼ無彩色のため効果は小さめ |
| C08 | Shot/Skin/WB/Reference match | 複数cutの色統一 | 検証済みI / 未検証 | offline drx match tools実走（journal 21:29:44）: shot/WB gradeCount=2・drx出力あり。ただしskin_gradeCount=0（肌マッチ生成ゼロ）・match品質のrender検証なし——skin分は未検証。 |
| C09 | Scope/Gamut/Legal QC | 品質測定 | 検証済みI | journal 20:15:46: signalstats実測（YMIN=11/YMAX=229）。※経路はffmpeg offline測定で.drt/.drx不使用——I定義との適合は要確認（Opus判断待ち）。 |
| C10 | Color Group | 長尺の多数カットをGroupで一括ルック管理（トーク常設カット等） | 検証済みA | journal 2026-09-07 QW batch（宣言⑰）検証済みA: add_color_group('qwgroup')→get_color_groups読戻し→timeline item（croute_450f V1 item1）へassign→get_color_group完全一致（disposable、API-only）。QW3 batch（宣言⑲）10:22:09で拡張読戻し: color_group.get_pre_clip_graph('qwgroup')={available:true,num_nodes:1}・get_post_clip_graph同様={available:true,num_nodes:1}（Group nodeのpre/post clip graphもAPI読取可）。 |

### U. Audio・Fairlight

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| U01 | Audio構造・mapping・level調査 | mix前の測定 | 検証済みA | 読取専用機能——audio_mapping_report等で実データ読戻し検証（journal 19:54:23 mapping_report/probe_item/probe_track）。write操作が存在しない機能群のため「write+readback」定義は適用外（読み取り検証済みとして明記）。 |
| U02 | Voice Isolation | 声の分離 | 検証済みA | matrix summary.md #10（{false,0}→{true,70}読戻し）＋mcp-fit voice-isolation accepted |
| U03 | Fairlight Preset適用 | 定型mix再適用 | 検証済みA（適用応答）・効果readback面なし | journal 2026-09-08T02:23:25 capA再試行: catalogに'dialogue-chain'が存在（前提データ不在が解消）→apply_fairlight_preset success=True（141ms）。suda裁定『preset保存されればAPI適用可』の条件成立。適用効果の独立readback面はAPIに無し（効果確認は未検証のまま）。 |
| U04 | Clip/Track Volume・Pan個別調整 | 音量調整（-6dB等） | 検証済みC | summary_report.txt item7_REMEASURED（Inspector volume field→render -6.000000dB exact。keyboard routeはinert確定） |
| U05 | EQ/Compressor/Automation/FairlightFX | **エコー**（style ①強調セリフの音声側）・声変調 | 未検証 | batch C実測: FairlightメニューバーにVocal Channel/FairlightFX無し（ブラウザドラッグ専用）— ドラッグ未達〔旧記載:  未検証（CU route。MCP手段の有無自体未確認 — style-vocab B①）。※束ね行: EQ/Compressor/Automation/FairlightFX——**エコー（style ①強調セリフの音声側）が核心**。部分検証時は検証部分を根拠欄に明記し、行全体を検証済みにしないこと（Fable指摘4）〕 |
| U06 | AI Audio Assistant | one-click mix | 検証済みI | batch C実測: タイムラインclipコンテキストメニューに『オーディオアシスタント…』(enabled=1)をAX採取 + メニューバー クリップ>AIツール>{ダイアログマッチャー,ボイスコンバート…} を列挙。パネルを開く押下は未実施（clip選択が競合下で不安定）〔旧記載:  スクリプト経路なし（vendor issue #128のまま再確認）。GUI: Fairlight上にAIアシスタントの直接ボタンは見つからず、タイムライン>AIツール submenu は存在するが合成入力では展開失敗（3回）。人手UIなら到達可能と推定〕 |
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
| R04 | Deliverページ固有設定をGUI操作 | APIにないcheckbox等 | 検証済みI | batch C実測: Deliverページ 詳細設定 アコーディオンをAX押下で展開（val 0→1観測=AXトグル自体は機能）+チェックボックス列挙（ビデオの書き出しval=1, サブブラックとスーパーホワイトを維持val=0, 縦型の解像度を使用val=0等）。ただしチェックボックスのトグルはAXPress・座標クリック共に不発（レンダープリセット未ロード時は無効化される疑い）。各クリップを個別にレンダリングはさらに奥で未到達。render.get_settingsは本buildでエラー（unknown+要求）〔旧記載:  チェックボックスの所在とAX読取（値・座標）は確認（ネットワーク最適化/チャプター生成/縦型解像度）。ただし合成クリックでは1つもトグルできず（4試行、ボックス領域のピクセル差分0）— 本セッションの自動操作は不調。人手操作は当然可能。なおrender.get_settingsが空dictを返すbuildのため、GUIトグルのAPI読み戻し検証も不可〕 |
| R05 | 納品QC・Compliance | 規格照合 | 検証済みI | journal 21:28:09 検証済みI: V5+U11証拠の合成でper-field pass/fail判定。※offline経路（単独のinterchange走行ではない——I定義との適合は要確認）。 |
| R06 | 字幕出力検証 | burn-in/embedded/sidecar確認 | 検証済みA | journal 20:56:49 検証済みA: burn-in画素差分3638px+sidecar実証（S10と同一証拠）。 |

### Q. 解析・編集判断（TikTok系に直結するもののみ）

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| Q01 | Timeline Marker・review annotation | YouTubeチャプター生成（R04のチャプターcheckboxと直結）・レビュー往復・自然言語修正指示の目印 | 検証済みA | journal 2026-09-07 QW2 batch（宣言⑱）: timeline_markers full CRUD on disposable croute_450f — add(frame0/Blue/qw2/test/qw2-cd)→get_all完全一致→get_by_custom_data一致→delete_at_frame→get_all空。export_review_report title「Review Annotation Report」3 scopes（timeline/item/pool、annotation 0）。API-only。timeline_item/media_pool_item marker面は未試行（本行のtimeline marker面のみ実証）。 |
| Q02 | Media解析・文字起こし・shot分析 | トークcontentの核——文字起こしが編集判断・字幕・カット候補の起点 | 未検証 | QW3 batch（宣言⑲）10:22:19 環境記録: media_analysis.capabilities()で本機はtranscription backend全滅（whisper_cli/whisper_cpp/mlx_whisper/http_transcriptionいずれもavailable=false・providers=[]）——Q06と同じブロッカーを本機で確定。vision(host_chat_paths)とffmpeg/ffprobeはavailable。analyze実行は未実施のため未検証のまま |
| Q03 | Scene Cut Detection | cut検出（jump-cut補助） | 未検証 | QW3 batch（宣言⑲）10:21:29 読戻し完結: detect_scene_cuts(background) job d4139ae1 done success=Trueだが、markers get_all前後とも0件でmarker増分ゼロ（単一素材timeline croute_450fでは検出結果がmarkerとして現れない実測）。API受理以上の効果なし＝未検証維持。マルチカット素材での再検証は未済。※2026-09-08T02:28:32 capA（2.210.0）: multi-clip timeline（croute_450f 6clip）で再実測——detect 3612ms実行（前回instant no-opから変化・version v11 archivingあり）但しmarkers増分0、成果なしは同一＝未検証維持 |
| Q06 | Editorial plan・Selects・Silence edit案 | 冒頭10秒まとめ（wishlist #2）の判断材料 | 検証済みA（silence-ripple plan）/ 未検証（selects）/ execute不達 | journal 2026-09-08 batch A: **plan_silence_ripple 実出力成功**（plan 74a553693954: 10 lifts・推定8.0s除去・item毎に較正されたthreshold(-31.97/-34.99/-31.23dB)・36 keep ranges・未較正itemは理由付きskip・qw4は無音声でskip・handle reportも正直に未検証と明示）——波形解析はtranscription backend不要で動作。**plan_selects は解析DB空で不達**（"No analyzed clips in the DB"）。**execute_silence_ripple は組立失敗**（"missing timeline item at index 35"=音声を持たないqw4 video rangeへの音声ミラー不可、部分的variantを残してエラー→variant削除・元timeline無傷）。 |
| Q08 | Grade/mix反復案 | カット間ルック揃え・ラウドネス目標の測定→候補→再測定loop | 未検証 | QW4 batch（宣言㉑）10:49:16: media_analysis grade_loop（clip ad24d461）を最小形で呼出→server側「No module named 'numpy'」verbatimエラーでloop plan不返却。環境記録のみ＝未検証維持（索引記載のgrade_loop/mix_planは自動適用を意味しない）。**2026-09-08 batch A 再検（2.210.0）**: disposable clip 81a6d3b7で同一verbatim エラー「No module named 'numpy'」——ブロッカー不変。 |


### O. 実行管理（guide参照）

| 機能ID | 機能名 | TikTok系/Vlog系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| O01 | MCP内の編集・Color・Audio guideを検索 | agentがcraft操作前に手順と落とし穴を読む自己参照（安全な判断の前提） | 検証済みA | journal 2026-09-07 QW batch（宣言⑰）検証済みA: topics(36件)→get(resolve-session全文)→search('grading' 5hits)→capabilities(36 topics/62 aliases)、全段非空を読戻し。読取専用機能のため読戻し自体が能力の実証（U01先例）。 |

### N. レビューで追加発見（2026-09-07監査——suda索引に不在、高頻度のみ抽出）

機能IDはレビューで新設（N接頭辞）。すべて未試行。低・中頻度の残り約60項目は本レビュー報告参照。

| 機能ID | 機能名 | TikTok系での用途 | 現在の状態 | 根拠 |
|---|---|---|---|---|
| N01 | スナッピング on/off | 人間編集の基本トグル（自動化は座標指定のため影響小） | 検証済みA | batch C GUI実測（journal 2026-09-08 03:2x capC_19519）: メニューバー タイムライン>スナップ のAXMenuItemMarkChar読取でトグル遷移を実証（✓→空→✓、復帰確認済み）。AX状態読取がトグル状態の正で機能する〔旧記載:  2026-09-07レビュー: 編集基本監査で完全欠落と判定〕 |
| N02 | Ripple delete・ギャップ詰め | 間詰めpacingの最頻出操作（T05/T06が部分対応だが明示行なし）。長尺トークのデッドエア除去が最大用途 | 検証済みA | 〔capE_82983マージ 2026-09-08 09:5x〕timeline.delete_clipsのripple対比を読返し数値で完遂: 同一head clip削除でripple=False→後続clip [216090,216180) 不変（ギャップ残留）／ripple=True（confirm_token 2-call gate）→後続clipが[216090,216180)→[216001,216090)へ左移動・V1 2→1item。ripple意味論のAPI面確定。GUIキーはbatch D記録のforwarddeleteをcapEでもdeleteで再試行し不発（キー名はforwarddeleteが正）。〔batch D 2026-09-08（共通操作quick・GUI/CU経路）: metacuaのdeleteキー名は不発、forwarddeleteが有効キー名。V1 clip2を選択してforwarddelete→V1+A1が2→1item、残clip1 [108000,108090)不変を読返し実測。ギャップ残留vs ripple短縮の対比は双子のproject切替で汚染され未完——capEが完遂〕 |
| N03 | Linked selection・音声分離(detach) | 音だけ差し替え・詰めの常見ワークフロー | 検証済みC | batch C実測: メニューバー タイムライン>リンク選択 のmark読取でトグル実証（空→✓→空→✓復帰）。リンク状態のAPI読取面を確立（timeline_item get_linked_items がリンク対を返す）。ただしclip右クリックのコンテキストメニューがCU操作で開かず（クリック自体は選択として成立=Inspector追従確認）、detach（リンク解除）のGUI実行は未達。コンテキストメニューの構造AX採取済み（クリップをリンク/ビデオから同期オーディオを分離等）〔旧記載:  同上。journalにproxy unlink(M06)とset_clips_linked API言示の形跡あり——clipリンク解除(detach)自体は未試行（Fable指摘2の追記）〕 2026-09-08 batch E追試(capE_95913): 右クリック>クリップをリンク(val=1) AXPress→GetLinkedItems [capE_95913_a.mp4]→[] の読戻し実測=GUI音声分離(detach)成立、batch Cの未達を解消。右クリックメニューに『分離』ラベル項目は存在せず（axfind全走査0件）——リンク切替が分離の実体。A1配置は不変（埋め込み音声）。|
| N04 | アジャストメントクリップ | 全カット一括ルック（style ①一括モノクロ等）。長尺エピソード全体のルック統一・公開前一括補正 | 未検証 | 2026-09-07レビュー: color監査で完全欠落 |
| N05 | フリーズフレーム | TikTokズーム演出の部品（T14根拠noteにsuspended言及のみ） | 未検証 | 編集基本監査。〔capE_82983 2026-09-08〕タイムラインメニュー全走査で「フリーズ」項目は存在せず（実測ラベル: トランジションを追加/ビデオのみのトランジションを追加/オーディオのみのトランジションを追加等）。クリップコンテキスト経路は未試行のまま |
| N06 | ソースIn/Outマーク | 素材区間指定の基本 | 検証済みA（API面）＋GUI未試行 | 〔capC_18173マージ 2026-09-08 03:20〕run_inline: pool clip SetMarkInOut→get_mark_in_out一致。GUIキー(I/O)操作は未試行と明記 |
| N07 | セーフエリア/グリッドオーバーレイ | 9:16縦型テロップ安全域の確認。16:9でも下部テロップ/字幕の被り確認に使用 | 検証済みI | batch C実測: 表示メニュー全列挙（74項目）。表示>セーフエリア>オン>{デフォルト(✓),1.33,1.77,1.85,2.35,2.39,2.40,フレームの外枠,アクション,タイトル,センター} とオーバーレイ能力自体は存在実証。ただしビジュアル確認未達（試行時のビューアが双子インスタンスの空タイムラインで黒_frame、ガイド描画不可）〔旧記載:  同上〕 |
| N08 | グリーンスクリーンキーイング（Delta/Ultra/3D/Chroma） | 背景差替え（wishlist #4）——F10不可の唯一の現実的代替経路 | 検証済みA（node authoring+readback）＋render証明（capB_6277で達成／capB_5877は失敗を併記） | batch B実測（journal 2026-09-08 02:4x〜02:5x capB, test-media）: 自作グリーンスクリーン素材（0x00B140+drawbox移動矩形）→Fusion comp作成→AddTool('DeltaKeyer')→ConnectTo配線（MediaIn1→DeltaKeyer1→BrightnessContrast1(0.2)→MediaOut1）→GetToolList読戻し一致＝**API経由のキーヤーnode構築・配線・読戻しは検証済みA（render未証明と明記）**。render A/B試行は21.0.4.5で**失敗**: JobStatus=失敗「01:00:00:13のFusionコンポジションを処理できませんでした」＋1frameスタブ（F02/F04の「inert出力」より強い再現——レンダラ自体がcompを処理拒否）。timeline_frame capture/in-script AddRenderJobも空返し。render証明は未達のまま。**〔capB_6277マージ 2026-09-08〕同buildでrender証明を達成**: 緑背景+静止赤矩形素材→UltraKeyer/DeltaKeyer追加（safe_add_tool）→MediaIn1→UK→MediaOut1→キーカラー数値設定（Background 0/1/0）→1frame renderで**赤矩形維持(255,0,0, red_frac=0.061)+緑消失(green_frac=0.0)+黒背景0.937＝正しいキーがrender実証**。F02/F04「comp不render」は本経路（clip埋め込みcomp）では不成立。注意: キーカラーはAPI数値設定のみ（UIスポイト到達不可）。〔競合注記: 2インスタンス（capB_6277/capB_5877）が同一Resolve上で並走し互いのcurrent projectを奪い合った——5877側のrender失敗は競合による配線破壊/レンダ競合の混入が疑われる。journal CONFLICT_MARKER参照〕 |
| N09 | 映像ノイズリダクション（temporal/spatial） | 暗所スマホ素材の必須級 | 未検証 | batch B実測（journal 2026-09-08 capB, test-media）: ノイズ素材（ffmpeg noise=alls=40）配置済み。clipへのResolveFX/OFX適用API面は存在しない（timeline item method列挙にFX系ゼロ——Stabilize/SetCDL/SetLUT等のみ）。timeline面はInsertOFXGeneratorIntoTimeline（generator専用）のみ。Fusion経路はtemporal NR在庫tool無し＋本batchでcomp render失敗を同時実測（N08参照）。F06依存（N51と同型）は維持。〔capB_6277マージ: grade_capabilitiesの全item_methodsを調査しOFX/ResolveFX挿入面ゼロを再確認（capB_6277_noisy.mp4, noise=alls=45素材）。N08のcomp render証明はtemporal NRには使えない——Fusion库存にtemporal NR toolが無いため〕 |
| N10 | 顔補正/Face Refinement・顔トラッキング | 顔出しクリエイターの定番beauty | 未検証 | 同上 |
| N11 | Retime品質設定（Optical Flow/Speed Warp） | スローモーション品質（T12/T15は速度値のみで品質モード未扱い）。旅行・アクション素材のスローでも同様 | 検証済みA | journal 2026-09-08T02:18 batch A: set_retime(process='optical_flow', motion_estimation=2)→get_retime {process:3, motion_estimation:2} 完全一致、2回目me=5→読戻し5。speed_warp文字列は「Invalid process. Use: project, nearest, frame_blend, optical_flow or integer 0-3」で拒否（Speed Warpは本API面に無し）。disposable clip、API-only。 |
| N12 | カラーマネジメント（RCM/ACES） | 素材混在時の色一貫性の土台 | 検証済みA | journal 2026-09-08T02:21:18 capA（2.210.0再検）: get colorScienceMode=davinciYRGB→set('davinciYRGBColorManaged') success→読戻し一致→revert success。旧buildでの書込拒否（N49の10:49:16実測）は本buildで解消。disposable project、API-only。 |
| N13 | Text+深度スタイリング（縁取り/グラデ/カーニング/行間） | 太字字幕の生命線——S01は本文+Size+色のみ。長尺テロップ2階層の縁取り/可読性に同型＋ふりがな（ルビ）は未踏査 | 検証済みI | batch C実測: Text+ comp入力のread/writeを実証（StyledText書換+読戻一致、UseFontKerning 1→0→1）。縁取り（Outline/shading要素）はLuaバインディングでShadingElementsの要素アクセスが失敗し未達。Inspector GUI操作は双子競合で座標が不定化し断念〔旧記載:  2026-09-07レビュー: titles監査でCRITICAL判定〕 |
| N14 | 絵文字/ステッカー/グラフィック素材 | TikTok字幕の頻出装飾（TikTok先行——vlogでは地点ピン/矢印等の軽用に留まる） | 検証済みC | batch C実測: StyledTextに『絵文字テスト 🎬🔥📷』を設定→1frame render→クロップ解析で**絵文字3種がカラーグリフで描画されることを実証**。日本語本文はデフォルトテンプレートフォントでTOFU（欠落グリフ）— JP字幕には日本語フォント指定が必須という実測知見（パイプライン失敗ではなくフォント設定事項）〔旧記載:  同上〕 |
| N15 | テキスト背景プレート（角丸ボックス・色帯） | 読みやすさの要 | 未検証 | batch C実測: comp入力 Background=1 はテンプレ既定値で、1でも背景プレートは描画されない（1→0→1フリップは入力レベルで実証）。実際の背景プレートはShadingElementsの要素単位イネーブルでありLuaから到達できず。Inspector GUI経路も未達〔旧記載:  同上〕 |
| N16 | ロゴ/ウォーターマーク常時overlay | 収益面で常時使用（M01+T01+F02の統合workflow行なし） | 検証済みC | batch C実測（capC_19519）: PIL生成ロゴPNG(128x64)をpositioned appendでV2配置→1frame render→コーナー領域の画素解析でsteelblue/orangeを検出=overlay render実証。〔capC_18173マージ: placement+transform+readback の部分検証済みA〕 |
| N17 | Fusionタイトルテンプレート適用 | 既成アニメ付きタイトル（F03/F04は素node構築のみ） | 検証済みA | batch C実測: insert_fusion_title('Lower Third')→Fusion comp存在+ツールグラフ読取（comp_count=1, tools=[FlipUp_LowerThird,Text1,Follower...,KeyframeStretcher,MediaOut1]）。テンプレートが実体のcompを持つことをAPI読取で実証。Effectsライブラリからのドラッグは未試行（競合で時間切れ）。注意: track_index指定は無視され再生ヘッド位置のV1に挿入された〔旧記載:  同上〕 |
| N18 | 音声クロスフェード/Jカット/Lカット | 声コンテンツの継ぎ目処理の基本（T09/T10は映像のみ） | 未検証 | batch C実測: V1ペア選択+Cmd+Tを試行したが item position 変化なし（A1/V1 get_items 前後一致）。トランジション存在のAPI読取面も不明。双子競合下でペア選択の成立保証がなく未達。Fairlightメニューにバッチフェード系は存在〔旧記載:  2026-09-07レビュー: audio監査でCRITICAL判定〕2026-09-08 batch D（共通操作quick・GUI/CU経路・双子競合下）: A1ペアを選択（settle click+shift cgclick）してCmd+T→item position不変（batch C再現）。Fairlight>選択をクロスフェードは選択直後のメニューダンプでも enabled=0——CUクリックのタイムライン選択がメニュー有効化に反映されない実測。〔capE_82983マージ 2026-09-08〕A1単選択+Cmd+T→item不変（C/D再現・3度目）、クリップ>トランジションを追加 pressもitem不変。ハードカット境界の数値証拠を取得: 境界前後100ms窓で e440=0.116→0.0 / e880=0.058→0.177 の階段状切替（max_jump 0.0212）=クロスフェード無しの対比データ。平滑遷移は不成立 |
| N19 | クリップ端フェードハンドル | BGM/SFX出入りフェード | 未検証 | batch C実測: 端フェードハンドルのドラッグ未達（同上の競合）。メニューバーにFairlight>バッチフェード設定…/バッチフェードを適用 を確認（フェード能力の存在は確認、edit面のハンドル操作は未達）〔旧記載:  同上〕2026-09-08 batch D（共通操作quick・GUI/CU経路・双子競合下）: Fairlightメニューバーに再生ヘッドまでフェードイン/再生ヘッドからフェードアウト/選択をクロスフェード/バッチフェード設定…/バッチフェードを適用 をAX実確認。AXPress成功(exit0)後もフェード曲線は画面に現れず＋音声レンダ数値読返えが2方式とも不達（wav/lpcmはAddRenderJobが240秒超滞留後キュー空・出力ゼロ、mp4/H.264はSetRenderSettings=False、GetRenderSettingsはバインディングNone）。実施中に双子がcurrent projectを奪いstate破棄。〔capE_82983マージ 2026-09-08 単独インスタンス再検〕A1 clip1選択→Fairlight>バッチフェード設定… press→バッチフェードを適用 pressまで到達（menu PRESSED記録）。render A/B: apply前後で先頭0.5sのRMS完全一致（0.12648,0.12511,0.12471,…=同一値）＝フェード未適用。設定ダイアログのAX可視項目ゼロ（axdumpにdialog項目なし）＝ダイアログ自体が開かず適用が無効の実測。mp4 render経路はcapEで正常動作（922KB/45f）——batch Dのrender不達は双子競合起因の可能性 |
| N20 | 波形/タイムコード自動同期 | 別録りwavと映像の同期の必須工程 | 検証済みA（リンク生成）/ 要注意（別ペアではno-op疑い） | 2系統の実測が併存: (1) journal 2026-09-08 batch A: auto_sync_audio([capA_src.mp4(映像のみ), capA_tone.wav(ffmpeg生成2s)])→success＋読戻し「Synced Audio」=capA_tone.wav・pool typeがビデオ→ビデオ+オーディオに反転=同期リンク実生成のreadback。(2) 同日02:27:02別ペア実測: auto_sync_audio(edit-source+speech wav)→success=True但し7ms・poolに新規sync clip出現なし=no-op疑い。link property書込は実在するが「新規sync clip生成」は来ない可能性——整列精度は未計測。運用ではSynced Audio property読戻しで必ず確認すること。 |
| N21 | 音声ノイズリダクション・Dialogue Leveler | 部屋ノイズ・声量ムラ（U02 Voice Isolationは別機能） | 未検証 | batch B実測（journal 2026-09-08 capB, test-media）: ノイズ音声（sine+anoisesrc+tremolo）をaudio trackへ配置。audio timeline itemのmethod列挙でfx/ofx/plug/fairlight/effect系＝**ゼロ**（FairlightFX挿入API面不在）。timeline面もInsertOFXGeneratorIntoTimelineのみ。Voice Isolation読取/設定（{isEnabled,amount}）は可＝U02別機能として生きている。Dialogue Leveler/De-noiser挿入はAPI不可——batch C（CU）持ち越しの境界を記録。〔capB_6277マージ: fairlight_boundary_reportで再確認。fairlight_presetsに「dialogue-chain」が存在しApplyFairlightPresetToCurrentTimelineはAPI可——preset適用経路のみ理論上API到達可だがpreset著作はCU側〕 |
| N22 | clip color/flag・bin整理・メディアプール検索 | レビュー・素材整理の基盤。長尺1本の素材量（複数日・複数カード）で頻度上昇 | 検証済みA（bin整理・検索）/ flagは既実測のまま | journal 2026-09-08T02:27:43 capA: organize_clips dry_run→move(moved=1)→get_clips(EP_test)読戻し一致→Master復帰。検索: timeline.clip_where(name_contains)match1/1＋folder.get_clips列挙。clip flagはM05のAPI不発実測を引き継ぎ（再検スキップ）。 |
| N23 | タイムライン複製/snapshot退避 | 破壊的操作前の保険 | 検証済みA | journal 2026-09-07 QW2 batch（宣言⑱）: timeline.duplicate(croute_450f→croute_450f_qwdup, id d8b80d9c) list 17→18→delete_timelines（confirm token・preview名一致確認・自作dupのみ削除）→17に復帰。currentをcroute_450fに戻し済み。API-only。 |
| N24 | XML/AAF/EDL interchange往復 | 他NLE・長期保管（T02は.drt/.drpのみ） | 検証済みI（FCPXML往復・2インスタンス別素材で再現） | journal 2026-09-08 batch A（2実行）: (1) export_timeline_checked(capA_t05b→FCPXML 1.10, 2605B)→import→検証（02:1x台）。(2) 02:31:10 croute_450f対象: export 11666B→import success(media 12/12 linked)→構造比較 video 9/9・audio 3/3 span+name完全一致。注: compare_timelinesはleft明示指定が効かず（name解決で自己比較になる制約）＝直接span突き合わせで実施。 |
| N25 | In/Out範囲render・VBR/CBR品質（bitrate）・音声format/ch指定・複数timeline一括render | YouTube横型master＋TikTok縦型の両納品で毎回の操作——16:9 masterの品質・コーデック深度を含む（R行に存在せず） | 検証済みA / 未検証 | journal 2026-09-07 10:47:28 QW4 batch（宣言㉑）複合: **検証済みA面**＝prepare_render_job最小形（marks 86401-86473＝72f、target qw4/）でjob_id 1720a71c生成settings_success=true→probe_render_settingsのjob listでJob7読戻し（croute_450f/1920x1080/24fps/86401-86473/qw4_n25c.wav、QW3 N42残置のAudio Only preset継承を確認＝inherited-stateのlive証拠）→delete_job掃除済み。**未検証面**＝FormatWidth/FormatHeight/EncodingProfile=High束はvalidate_render_settings静的validでもlive書込でsuccess=FALSE settings_success=FALSE（job_idなし、相対/絶対dir・timeline内marks両形で再現）。safe_set_render_settings(EncodingProfile=High)もdiff.coerced_or_missing{requested:High,applied:null}で拒否。render実行自体・複数timeline一括・音声ch指定は未試行 |
| N26 | キーフレームのイージング（Ease In/Out/Bezier） | 「スムーズなズーム」等の実務はイージング前提——F02の実証は直線(Linear)のみ | C経路未試行 | 2026-09-08表記正直化（suda承認）: 未検証→C経路未試行（API側の否定/不発は実測済み、画面操作経路が未試行のため）。QW3 batch（宣言⑲）10:22:00: timeline_item get_keyframes('ZoomX')/add_keyframeがサーバ側で「'NoneType' object is not callable」エラー（同一itemのget_transformは成功＝keyframe経路のみ不達）。書込自体が不可のため未検証維持。set_keyframe_interpolation到達前の段で停止。※2026-09-08T02:22:46 capA（2.210.0再検）: 同一エラー再現（add_keyframe/get_keyframes両方'NoneType' object is not callable）——v2.210.0でも変化なし。※2026-09-08 capJ_29468 double-click set: F02 diamond patternをcapFで再実行——Inspector ズーム行ダイヤモンド(1443,191)click@playheadA(X=1.000)→playhead移動→ZoomX数値欄(1231,191)ダブルクリック+1.5+return（AX '1.5'確認）→ダイヤモンドclick@playheadBで2点配置は再現。**ただしInspectorダイヤモンドの右クリックはコンテキストメニュー非表示（実測ネガティブ、capJ_n26_rightclick.png）**——補間設定面はInspectorダイヤモンドには存在しない模様。タイムラインクリップ右クリック→キーフレームエディタ経路はメニュー不発のまま時間切れ（settle不足）、補間メニューのラベルは読めていない。イージング設定は引き続き未試行。【2026-09-08 capK_47168 再訪（marker takeover後）】独立再実測: ダイヤモンドは単なるキーマークではなく**自動キーフレームトグル**であり赤色=ON（誤クリックで発見）。ON状態でZoomX数値欄ダブルクリック+1.5+return→kf@01:00:03:00=1.5が自動生成（中間点01:00:02:00のフィールド読取1.500=最初のkfより左は定値——**再生ヘッド位置でのフィールド読取はキーフレーム存在/補間値の定量的読出法として成立**）。2点目の値セット(1.0)はcgclickタイミングで編集状態に入らず不発。右クリック メニューは再試行せずcapJのネガティブを維持。【2026-09-08 capL_501（capF_29468プロジェクト再利用・同一インスタンスlive）】**クリップ右クリック経路の否定を実測で確定**: V1クリップ本体(370,688)右クリックのコンテキストメニュー全ラベル実測（新規複合クリップ/新規Fusionクリップ/レンダリングして置き換え/コピー/カット/リップルカット/選択を削除/リンク削除/クリップ有効化無効化/クリップの長さを変更/クリップの速度を変更/タイムムコントロール/Fusionで開く/Fusionコンポジションをリセット/リファレンスコンポジションを作成/オーディオレベルをノーマライズ/ボイスアイソレート/クリップカラー/履歴メディアを生成/レンダーキャッシュ×2/コンフォームロック×2/メディアプール内で検索/クリップ属性/複製フレーム/デイウェイブター/音楽のビートを表示/クリップをリンク）——**「クリップのキーフレームエディタを表示」項目はコンテキストメニューに存在しない**（capJの「メニュー不発」はsettle不足ではなく項目自体が無いことが確定）。メニューバー「クリップ」も全ラベル実測で同項目無し。タイムラインツールバー(407,543)のアイコンはキーフレームエディタではなく編集モードトグル（押下→即解除、実害なし）。keyframe生成の再試行: auto-key ON下でのZoomX欄ダブルクリック+1.5+returnの2点目はcapKと同一の不発を再現、API set_propertyはstatic値のみ（kf生成なし——quarter点01:00:00:20のplayhead読取が1.0のまま=kf不在の定量判定）、行ダイヤモンドclick@playheadでもkf生成を確認できず。**補間メニュー（リニア/平滑/ベジェ/Ease）は未到達——エディタoverlayの開く位置自体が未特定**（正規経路はタイムライン左端ツールバーの専用トグルかショートカットの可能性、次回具体的に調査）。N26は「C経路未試行」維持、ただし到達経路の探索範囲が実測で狭まった（クリップ右クリック/メニューバークリップは否定）【2026-09-08 capL_99701（dead-twin capL_501確認後にmarker takeover・独立再実測）】capL_501の否定を**別インスタンスで再現**: V1ビデオレーン右クリック(519,729)→settle 1.5sでコンテキストメニュー確実に開く（18項目全ラベル実測: カット/リップルカット/選択を削除/リップル削除/クリップを有効化・無効化/クリップの長さを変更…/クリップの速度を変更…/オーディオレベルをノーマライズ…/ボイスコンバート…/クリップカラー＞/オーディオエフェクトをキャッシュ/コンフォームロック有効/メディアプールのクリップにコンフォームロック/メディアプール内で検索/クリップ属性…/複製フレーム＞/各オーディオチャンネルを表示/音楽のビートを表示）——**キーフレームエディタ項目なし再確定**（注意: AVリンククリップでaudio項目が混在するメニューだった=videoレーン座標729でもリンクメニューの可能性、video-only clipでの差異は未検証）。UIクセ新規実測: ZoomX数値欄の編集コミットは**returnキーでは不発**（赤リングが消えず2回押下でも不発）、**TABで確実にコミット**。API get_property(ZoomX)はplayhead非依存のstatic読取（+5/+20/+65全部1.0）=**再生ヘッド依存の定量読取はGUIフィールド一択（capK method再確認）**。ツールバー経路: トラックヘッダー列(x0-400)にカーブ/ダイヤモンドアイコン無しを実測——正規経路はタイムライン左端ツールバーの専用トグルかショートカットの可能性残存。キー2点のクリーン再構築は値セット不発（capK既知クセ再現）のため完遂できず、**補間メニュー・overlay・easing証明（quarter frame 1.125対比）は未到達のまま**。deliverable DB sha256 c0f836ba…前後byte同一（stat-only確認）【2026-09-08T13:34+0900 capM RESEARCH_FINDING（GUI触る前のmanual抽出・/tmp/resolve_manual.txt＝Resolve 21 manual）】正規の人間手順は2経路のみ文書化: (1)Inspector経路＝"Move the playhead to a frame with a keyframe using the next/previous keyframe controls, then right-click the orange keyframe button and choose Ease In, Ease Out, or Ease In and Out"／Linearに戻す同手順（Ch.60 Keyframing Effects p.1290）。**右クリック対象はInspectorの橙ダイヤモンド（ズーム行右端ボタン）でありクリップ本体ではない**——capJ/Lの「クリップ右クリックに項目なし」はmanual通りの当然結果（manualはクリップ右クリック経路を一切記載せず）。(2)Curve Editor経路＝"Select one or more keyframes, then choose Ease In, Ease Out, or Ease In and Out from the toolbar"＋4 Bezier補間ボタン（Linear/Ease in/Ease in and Out/Ease out/Step in/Step out、p.1301-1302）。Keyframe Editor自体の開き方＝"click on the Keyframes tab in the upper left" or "Show Keyframe Tray icon on the main timeline toolbar" or クリップ名バーのCurve/Keyframeボタン（p.1294,1298）。参考: audio volume overlayのみ"right-click one of the selected keyframes and choose Ease In, Ease Out, or Ease In and Out"（p.1142-1143・audioカーブ上のダイヤモンド直右クリック）。よって本runの検証対象は(1)Inspector橙ダイヤモンド右クリック（playheadをkeyframe上に厳密停止させた状態）を第一候補とする。capJの「Inspectorダイヤモンド右クリックでメニュー非表示」ネガティブはplayhead非停止or非橙状態の可能性が残るため再試行する。【2026-09-08T13:34-13:49 capM_33096（15min hard cap・GUI実測）】STEP1部分成功: playhead 01:00:00:05（MCP seek確認）でInspector ズーム行ダイヤモンド(1417,190)click→赤点灯を確認（capM_33096_zoomrow3.png・ZoomX=1.000）＝+5キー存在確定。ただし+65（01:00:02:17・MCP seek確認）の2点目値セットは2回連続不発: ZoomX欄ダブルクリック(1185,192)→cmd+a→"1.5"タイプ→TABでmacOS予測変換ポップアップ（1.5/一.五）が残留し確定を横取り、フォーカス移動で編集破棄→1.000に復帰（zoomrow5/6.png）。Return→TAB順の再投入も位置Xへフォーカス移動のみでZoomX=1.000のまま（zoomrow7.png）。新規UIクセ: 数値欄はIME/予測変換が確定を妨害する（TABは候補選択に消費される）。STEP2未達: クリップoverlay上のダイヤモンド特定は未実施（キーフレームトレイ未開・V1 tweet表示に可視ダイヤなし）。STEP3条件不成立: +5へMCP復帰→橙ダイヤ想定(1417,190)を右クリック→全画面にメニューなし（capM_33096_rclick.png）。ただし選択状態が複数クリップ（V1+A1+字幕beep・Inspector表記「複数クリップ」）でダイヤは灰色＝manual前提（playhead停止＋橙ダイヤ＋単独クリップ）を満たさず**無効試行**。capJのネガティブと同形だが橙条件未充足のため「橙ダイヤ右クリックでメニュー無し」は依然未確定。STEP4未実施（2値不在・ベースライン未撮）。次runの最短手順: 全選択解除→V1単独→橙確認→右クリック（+IME OFFで2点目1.5再投入後にEase選択→+20/+8 proof）。N26はC経路未試行維持。deliverable/private無改変（git statusでprivate差分なし確認・stat-only）。【2026-09-08T14:13-14:26 capN_NN（12min hard cap・LAST RUN・partial）】STEP1完: DB stat-only一致（c0f836ba/13,852,672B/2026-09-05 22:22:25）。STEP2完: Cmd+Shift+A全選択解除→赤枠消去を確認→V1クリップ(340,660)単独クリック→Inspector表題capF_29468_a.mp4単独・playhead 01:00:00:05維持・ズーム行ダイヤモンド実測RGB(254,16,18)=赤点灯・ZoomX=1.000（kf#1存在再確定、capMと一致）。座標系確定: metacua-go click空間＝shot画素そのまま（ダイヤ実測(1412,196)≒既知(1416,187)）。STEP3未達: タイムコード欄クリック×3不発（編集モードに入らず）→type-to-seek代替「01000217」+returnも不発（playhead 01:00:00:05のまま）。副害: 空き輸郭クリック(900,640)が字幕1選択を招く→即Cmd+Shift+Aで clean state復帰（Inspector再び単独表題、数値無改変）。1.5ペースト・橙ダイヤ右クリック・メニューラベル・proofは未試行。次run最短: playhead移動はMCP seekか右矢印キー送りを先に解決すること（GUI欄クリックは不可）。N26はC経路未試行維持。deliverable DB終了時再確認一致（c0f836ba・mtime不変）。 【2026-09-08 capO_9342 final micro (MCP seek fix適用)】playhead移動はMCP seekで全6回正確に成功（GUIタイムコード欄クリック不発の修正確定）。マニュアル前提充足下（プレイヘッド+kf厳密停止・単独クリップ選択・ダイヤモンド赤点灯@+5）でInspector橙ダイヤ右クリックを3座標試行→コンテキストメニュー0ラベル×3（capJネガティブを前提充足下で拡張確定）。API set_keyframe_interpolationも例外不発。2点目値セット(1.5@+65)はtype+TABでフィールドに着地するが**キーフレームとして不持続**（+20フィールド読取1.000・+65ダイヤ灰色=auto-key OFF がcapK再現不成功の原因）。イージングnumeric proofは不取得。N26は「C経路試行済み・不発」へ更新、残経路はKeyframe Editor(Curve)正規トグルの特定のみ。deliverable DB sha/bytes/mtime終始一致。 |
| N27 | .drfxテンプレートパックの導入・使用 | TikTok系エフェクト多用の実態は購入テンプレ運用が大半——導入と適用の両面。vlogもタイトル/LUTテンプレ運用は同型 | 未検証 | Fable指摘2: 索引・地図とも行なし（N17は内蔵テンプレ適用で別物） |
| N28 | サムネイル用静止画書き出し | YouTube運用で毎本必要 | 検証済みA | journal 2026-09-07 10:48:12 QW4 batch（宣言㉑）検証済みA: croute_450fでset_current_timecode 01:00:04:04（abs f86500）→open_page color（API）→export_frame_as_still(qw4_still.png) success→open_page edit復帰。file(1)＝PNG 1920x1080 8-bit RGB 6,231,977B・stdlib IHDR読取1920x1080一致（本機にPIL不在のためfile+IHDRで実証）。Fable指摘2の行なし解消・C03（ルック保存）とは別物 |
| N29 | 16:9→9:16背景ぼかしパディング | 縦型転換の定番レシピ | 未検証 | Fable指摘2: V節は解像度設定のみでこのレイアウト操作の行なし |
| N30 | BGM/SEを指定トラック・指定位置へ配置 | 音声素材のアセンブリ配置（T01は映像・写真の構築） | 検証済みA | journal 2026-09-08T02:26:25 capA: ffmpeg生成tone(2s)→safe_import→create_timeline_from_clips(positioned: audio track1, record_frame指定)→get_items読戻し [86700,86760) 完全一致。注: v2.210.0でtimeline.append_to_timelineはaction列挙から不在（配置はripple_insert/positioned作成経路）。 |
| N31 | 地点・店舗情報カードの合成（地名＋地図/映像＋テキストの複合構成） | style ③地点表示の本体・グルメカード | 検証済みC | batch C実測（capC_19519）: PILで地名入りカードPNG生成→V2 positioned append(48f)→1frame render→中央領域white_ratio=0.84でカード本体の描画を実証。〔capC_18173マージ: placement+readback の部分検証済みA〕 |
| N32 | Text+テロップの発話同期（喋りに合わせた出し引き） | style ①静的テロップ2階層の最頻出操作 | 未検証 | batch C: 未実行（Text+の配置自体はN13で実証済み。発話区間への出し引きはitem start/end操作でAPI可能なはずだが時間切れ）〔旧記載:  vlog再審: S03/S07は字幕トラック側のみ——Text+側の工程行なし〕 |
| N33 | テロップ読了速度に基づく表示duration規約 | 長尺で文字量→最低表示秒の計算が毎本 | 未検証 | vlog再審: 行も規約もなし（編集判断層を含む） |
| N34 | B-roll重ねworkflow（ナレーション上への実写挿入） | トークの切れ味を決める定番 | 検証済みA | journal 2026-09-07 10:47:56 QW4 batch（宣言㉑）検証済みA: croute_450f V1＝6 items（86400-86851）→video track 1本のみのためadd_trackでV2追加（archived v9）→media_pool.append_to_timeline positioned（qw_seq clip b49d65fb src0-24・rec相対100・track2）→V2読戻し1 item（qw_seq_[001-030].png 86500-86524 dur24＝V1-item1帯86401-86634内に正確配置）→delete_clips→V2空[]再読戻し。配置＋撤去の両方向write+readback。API-only |
| N35 | エンドカード・エンドスクリーン安全域（末尾UI避け・空き確保） | YouTube毎本 | 検証済みC | batch C実測（capC_19519）: エンドカードPNGをタイムライン末尾に配置→最終frame render→dark_ratio=0.99+白文字0.007で実証。安全域チェックはN07オーバーレイ未達のため未実施。〔capC_18173マージ: placement+readback の部分検証済みA〕 |
| N36 | 室内反響除去（de-reverb） | 部屋録り声のクリーン化 | 未検証 | batch C実測: Fairlightメニューバー全列挙でDe-Reverb無し（25項目に不含）。FairlightFXはEffectsブラウザのドラッグ（ページレベルGUI）で未達。InspectorのAI Voice Isolation欄は実在を視認（スクリーンショット）〔旧記載:  vlog再審: U05「エコー」は演出として足す側で逆方向——行なし〕 |
| N37 | 環境音・ルームトーン敷き | カット継ぎの聴感自然化 | 検証済みI | batch C実測（capC_19519）: ルームトーンwav(anoisesrc pink)をA2 positioned append→get_items読戻し一致（108000-108144）。レベル設定・duration trimの部分は未達。〔capC_18173マージ: placement+readback の部分検証済みA〕 |
| N38 | ピッチ保持の速度変更（声） | 早回し・スロー時に声の高さを保つ | 未検証 | vlog再審: T12〜T15/N11は映像側のみ。2026-09-08T02:19:35 capA: get_property('RetimingProcess')→null等pitch保持のAPI面不存在を再実測（set/get_retimeにもpitch系フィールド無し。CU/dialog経路は未試行） |
| N39 | 音声スクラブ・波形編集 | 語頭正確カット | C経路未試行 | 2026-09-08表記正直化（suda承認）: 未検証→C経路未試行（API側の否定/不発は実測済み、画面操作経路が未試行のため）。vlog再審: 行なし（U10はファイル加工） |
| N40 | チャンネル構成の書込（mono→stereo等） | カメラ音声+外部録音の混在処理 | 未検証 | QW4 batch（宣言㉑）10:49:16: probe_audio_item（croute_450f audio A1 item0）でmapping読取＝embedded 2ch・track1 ch[1,2] stereo unmuted・Volume/Pan null・voice_isolation off/0。safe_set_audio_properties(Pan=0)はdry ok→本実行success=FALSE（write=false/readback=null/restore=false、無変異）。書込経路不達のため未検証維持。※2026-09-08T02:23:15 capA（2.210.0再検）: 同一結果再現（Pan/Volume write=false x2・SetChannelMapping系method不在・mapping読取は可）。変化なし |
| N41 | 音楽ビート検出・ビート刻みカット | モンタージュ・切り替えの音楽合わせ | 未検証 | batch C実測: Fairlightメニューバーにビート検出系の項目は存在しない（全25項目列挙で確認=このbuildのメニュー面では機能不在）〔旧記載:  vlog再審: U09に「音楽sync」の語のみ・beat検出行なし〕 |
| N42 | 音声のみ書き出し（timeline→音声ファイル） | ポッドキャスト再利用 | 検証済みA | journal 2026-09-07 10:30:28 QW3 batch（宣言⑲）検証済みA: prepare_render_job(from_preset="Audio Only", marks 86401-86448)→render→qw3_audio2.wav 577,588B＝ffprobe pcm_s24le/48000Hz/2ch/2.000s。注意: format wavはset_format_and_codecでは拒否（available_codecs={}）・prepare_render_jobでもpresetpin無しだとmp4/H264を黙って継承する——**from_preset="Audio Only"必須**の実測 |
| N43 | VFR・回転フラグ付き素材の実素材確認（16:9 master文脈） | スマホ4K素材の毎本通る道 | 検証済みA（readback面）＋cadence render証明（capB_6277で達成） | batch B実測（journal 2026-09-08 capB, test-media・30fps project）: **VFR**: ffmpeg concat（24fps+60fps seg, 出力-r無し）→ffprobe r=24/1 avg=2616/121でVFR確認→Resolve GetClipProperty(FPS)は**単一の24.0のみ**（avg/r区別のAPI露出なし）。duration読取121f@24=5.0417sはcontainer durationと一致。**回転フラグ**: ffmpeg -noautorotate -display_rotation 90（注意: 無しでは実回転され_flagsが消える——ffmpeg 9のtrap）→coded 320x240+Display Matrix rotation=90→ResolveのResolution読取は**240x320（表示寸法に折込済み）**、transform読取はRotationAngle=0/FlipX/Y=false＝**回転フラグを示すAPI field無し**。配置異常: 回転素材のAppendToTimelineは指定外のin-pointを取る（明示0..60でもsrc 15..59で配置・2回再現）。conform後のcadence render検証は未実施（render経路は本batch N08でcomp以外も空返し wedge実測）。**〔capB_6277マージ 2026-09-08〕cadence render検証を達成（30+15fps VFR素材）**: 6s/180f render→隣接frame diff解析で**15fps区間の51%が同一frame（dup保持＝VFR cadence維持）**、30fps区間は0%同一＝ResolveはVFRを正しくconform。回転素材はportrait 360x640表示寸法のまま16:9 frame内に収めてrender（APIには角度非露出のまま）。tool bug: timeline_frame.captureは日本語localeでJobStatus=完了を誤判定RENDER_FAILED（実レンダ完了）——直接render jobで回避 |
| N44 | 混在解像度/fpsの1タイムライン扱い | 複数カメラ+スマホ混在のスケーリング | 検証済みA | journal 2026-09-07 10:48:51 QW4 batch（宣言㉑）検証済みA（partial）: ffmpeg testsrc2 1280x720@60 2s→qw4_720p60.mp4 3,237,748B→safe_import dry+本import（id ad24d461・Duration 00:00:02:00）→probeでResolution 1280x720・FPS 60.0・Frames 120・Start0/End119→croute_450f V1末尾空きにpositioned配置（src0-120・rec相対451・track1）→読戻しV1[6]＝86851-86899 dur48（120src@60→48tl@24＝時間換算配置）・get_transform ZoomX/Y 1.0（自動スケールなし）。配置＋読取の実証まで。log変換等の画質判断は範囲外 |
| N45 | タイムラプス連番画像のimport（StartIndex/EndIndex） | 連写真からの場面作り | 検証済みA | journal 2026-09-07 QW2 batch（宣言⑱）: ffmpeg testsrc 64x64 PNG 30枚（qw_seq_001〜030）→safe_import_sequence dry_run成功→本import成功（qw_seq_[001-030].png, id b49d65fb）。probe: Frames=30/FPS24/Duration 00:00:01:06/Online。disposable poolに残置。API-only。 |
| N46 | clip尺・タイムコード読取（logging） | 素材確認・selectsの土台 | 検証済みA | journal 2026-09-07 QW batch（宣言⑰）検証済みA: probe_clip_propertiesで実値読取（edit-source.mov: 8467f/00:04:42:07/StartTC 19:41:50:04/3840x2160/H.264/FPS30、wav: 00:00:05:08/Wave/48000）。読取専用機能のため読戻し自体が能力の実証（U01先例）。 |
| N47 | レンズ補正（fisheye・GoPro系） | アクションカム素材 | 検証済みA（Fusion LensDistort authoring+readback）＋render証明（capB_6277で達成） | batch B実測（journal 2026-09-08 capB, test-media）: Fusion comp経由でAddTool('LensDistort')→MediaIn1→LensDistort1→MediaOut1配線→GetToolList読戻し（RegID=LensDistort・Distort param読取可）＝**node構築・読戻しは検証済みA（render未証明と明記）**。ResolveFX側のレンズ補正はAPI面無し（N09と同型）。render証明はN08で同時実測のcomp render失敗（「Fusionコンポジションを処理できませんでした」）により未達。**〔capB_6277マージ 2026-09-08〕render証明を達成**: LensDistort（DEClassicLDModel.Distortion=0.5）→1frame renderで**赤矩形の面積14000→19052px・重心x 179.0→158.3移動（樽型膨張）**、source diff 2.97＝実歪みがrender実証。教訓: 歪み検出には前景要素必須（純緑素材では歪みがdiff 0.0で不可視） |
| N48 | .cube LUTファイルの適用 | 市販LUTパック運用の根幹 | 検証済みA | journal 2026-09-07 QW batch（宣言⑰）検証済みA: 自作identity .cube（LUT_3D_SIZE 2、private/runtime/sol-safeguards-20260906/qw_identity2.cube）→probe_node_graph(2 nodes)→node1へset_lut→get_lut='MCP/qw_identity2.cube'完全一致（disposable timeline item、API-only）。node2は先行sessionのPower Window grade保持のためnode1を選択。render A/Bはidentityのため省略。 |
| N49 | log素材のnormalize（Log→709変換） | log撮りカメラ素材の下処理 | 未検証 | QW4 batch（宣言㉑）10:49:16: get_setting colorScienceMode＝davinciYRGB→set_setting(rcm)はbare success=FALSE（既知API制限級）→再読davinciYRGBのまま無変異・revert不要。書込拒否のため未検証維持。※2026-09-08T02:21 capA（2.210.0）: colorScienceMode書込は本buildでsuccess+読戻し一致を実測（N12参照）——上記拒否実測は旧build時点のもの |
| N50 | 昼→夜の見た目統一 | 撮影時間帯が混ざるロケ素材 | 未検証 | vlog再審: C08は同条件マッチで時間帯変化の行なし |
| N51 | フィルムグレイン付与 | フィルム風ルック仕上げ | 未検証 | vlog再審: N09は逆のノイズ除去——付与側の行なし。journal 2026-09-08 batch A: ResolveFX/OFX追加API無し（F06 drag-fail依存）を明記。Fusion FilmGrain経路は技術的に存在するが21.0.4 render inert実測のため証明不可=スキップ。drt/drx authoringは到達不可。 |
| N52 | LUT強度ミックス（Key Output Gain等） | LUT当ての強さ調整 | 未検証 | journal 2026-09-08 batch A: probe_node_graphのmethod全列挙（Get/SetLUT・CDL・DRX適用等11種）にKey Output Gain/LUT-mix系が皆無——強度ミックスのAPI書込経路なし（CDLのslope/gainは別物）。正直な不在として未検証維持。 |
| N53 | マスター保存形式の選定（ProRes vs H.264等） | 画質・容量・再編集用途の分岐 | 検証済みA | journal 2026-09-07 QW batch（宣言⑰）検証済みA: get_formats(22形式)→get_codecs実読取。回答: ProRes一式（422/HQ/LT/Proxy/4444/XQ）はQuickTime(mov)のみ、mp4はH.264/H.265（＋APV YUV422 10-bit）。読取専用機能のため読戻し自体が能力の実証（U01先例）。※sol独立再確認2026-09-07: get_codecs実測で同一結果（mp4のAPV込み）。 |
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
- 状態語彙の追加・言い換えはしない（7語固定: 検証済みA/C/I・未検証・C経路未試行・不可・危険。束ね機能の部分検証は「検証済みX / 未検証」の併記形式で表現——Fable指摘4で確立）
- 原文（sudaさん指示・crash verbatim）は一字一句を優先する
- 本ファイルはdocs配下でバージョン管理される（会話履歴に依存しない）
