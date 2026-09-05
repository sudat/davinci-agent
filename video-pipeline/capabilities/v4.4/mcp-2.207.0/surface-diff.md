# davinci-resolve-mcp v2.98.3 → v2.207.0 オフライン表面差分（Phase 1）

- 作成: 2026-09-05（Opus 後続タスク 2026-09-05 06:13Z の Phase 1 / オフライン専用）
- 方法: Resolve も MCP サーバ起動も使わない静的解析。`services.toolchain.mcp_vendor_server.parse_server` / `mcp_vendor_surface.parse_vendor_surface` を両チェックアウトに実行。新バージョンは `git worktree`（`private/vendor/davinci-resolve-mcp-v2.207.0`、tag `v2.207.0` = `5a1db6776fbe76098a706a41811ca787dfbd9990`）。現行チェックアウト（`132e134` = v2.98.3）は一切変更していない
- 機械可読成果物: `surface-diff.json`（本ディレクトリ）、`fail-closed-impact.json`（同）、`../../baseline/mcp-2.207.0/vendor-surface.json`（v2.207.0 の全 surface 記録）

## 結論（要約）

**差分はすべて「追加」であり、削除・リネーム・引数変更はゼロ。** したがって現行の
バインディング（`mcp_dispositions_bindings.py`）と 1001 件の処遇行（dispositions rows）
はすべて v2.207.0 上でも成立し、フェイルクローズ（使えなくなって止まる）する項目は
**0 件**。アップグレード後に「できなくなるもの」はない。

## 数値のまとめ

| 面 | v2.98.3 | v2.207.0 | 差分 |
|---|---|---|---|
| compound ツール数 | 35 | 36 | +1（`knowledge` 新設）|
| compound アクション総数 | 723 | 748 | +25（削除 0）|
| granular ツール（module 単位）| 353 | 353 | 変更なし |
| advanced（Node）ツール数 | 18 | 18 | 変更なし |
| advanced アクション総数 | 150 | 154 | +4（削除 0）|
| kernel カタログ行 | 145 | 145 | 変更なし |
| confirm-token 保護 action | 14 | 15 | +1（`timeline.ripple_insert`＝新アクション）|

## Compound（Python サーバ、`src/server.py`）

- **削除ツール: なし / リネーム: なし / 引数変更: なし。** 全 compound ツールの
  入力スキーマは `(action: str, params: Optional[dict])` の包絡で、この形は両版で完全
  一致する。したがって既存 35 ツールの `tools/list` スキーマ（SHA-256）は変わらない
  見込み（フェーズ 2 の live 再取得で最終確認する）
- **新ツール `knowledge`**（4 action）: `topics` / `get` / `search` / `capabilities`。
  エディトリアル・カラー・オーディオの運用ガイドドキュメント読み取り専用
- 既存ツールへの追加 action（21 件）:

| ツール | 追加 action |
|---|---|
| media_analysis | grade_loop, grade_loop_capabilities, measure_loudness, mix_plan, mix_plan_capabilities |
| media_pool | capture_media_template, get_clip_marks |
| media_pool_item_markers | get_name |
| render | verify_output |
| resolve_control | begin_execution, clear_executions, end_execution, export_execution_report, get_execution, get_execution_trace, list_recent_executions |
| timeline | author_offline, get_clips_linked, get_title_text, offline_fallback_capabilities, ripple_insert |

- 安全クラス（write/destructive/…）の変更: なし。token-gated 追加は
  `timeline.ripple_insert` のみ（それ自体が新 action のため、既存運用への影響なし）

## Advanced（Node サーバ `davinci-resolve-advanced-mcp`）

- ツール構成は不変（18 ツール）。追加 action 4 件のみ:
  - **`drt`**: `assemble`, `assemble_from_interchange`, `assemble_project`
    - `drt.assemble` の `spec` に **`subtitles` / `subtitlesSrt`** フィールドが新設され、
      ネイティブ字幕をオフラインで .drt に書き込める（v2.98.3 には `assemble` 自体が
      存在せず、`subtitles` の出現も 0 件であることを確認済み）
    - ※本 Phase 1 は能力の記録まで。実装・製品組み込みはフェーズ 2 以降の判断
  - **`editorial`**: `verify_roundtrip`

## 検証メモ（Opus 検証事実の再確認）

- `timeline.insert_fusion_title` は v2.207.0 でも `name` のみ受け取る
  （`tl.InsertFusionTitleIntoTimeline(p["name"])`）。track_index は存在しない —
  8月の実測は v2.207.0 でも有効
- `drt.assemble` / `spec.subtitles` / `spec.subtitlesSrt` が v2.207.0 にのみ存在することを
  両版のソース検索で確認（v2.98.3 の drt.mjs/libs に `subtitles` 出現 0 件）

## フェイルクローズ影響（詳細は `fail-closed-impact.json`）

| 検査対象 | 件数 | v2.207.0 で失敗するもの |
|---|---|---|
| SURFACE_OPERATION_BINDINGS（surface→operation）| 66 | 0 件 |
| OPS_READ_OPERATION_BINDINGS（operation→McpOps）| 11 | 0 件 |
| dispositions 行（compound 723 + granular_only 278）| 1001 | 0 件（孤立なし）|

フェーズ 2 で必要になる追加作業: 新規 25 operation（`knowledge.*` 4 件 + 上記 21 件）への
処遇行（dispositions row）追加と、inventory の再生成。既存行の修正は不要。
