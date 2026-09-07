# davinci-resolve-mcp v2.207.0 → v2.210.0 オフライン表面差分（Phase 1）

- 作成: 2026-09-07（実機宣言㉒ / suda 指示 pin 更新。オフライン専用 — Resolve も MCP サーバ起動も使わない）
- 方法: `services.toolchain.mcp_vendor_surface.parse_vendor_surface` を現行 v2.207.0
  チェックアウト（`private/vendor/davinci-resolve-mcp`、`5a1db67`）と v2.210.0
  worktree（`private/vendor/davinci-resolve-mcp-v2.210.0`、tag `v2.210.0` =
  `4c42f429298ec63fd793e7a9d16bd7994d16f307`）に実行。advanced（Node）面は
  git path-scope diff、包絡（`_operation`）は `src/utils/operation_result.py` の
  バイト一致で判定
- 機械可読成果物: `surface-diff.json`（本ディレクトリ）、`fail-closed-impact.json`（同）、
  `../../baseline/mcp-2.210.0/vendor-surface.json`（v2.210.0 の全 surface 記録）
- 対象コミット range: 11 commit（v2.208.0 実行ライフサイクル/事前リスク検査/フック、
  v2.208.1 edit-engine variant 集計修正、v2.209.x–2.210.0 safe operations policy
  （PR #189: 全 destructive action のリスク評価））

## 結論（要約）

**差分は「追加 2 action」と内部実装の再編のみ。削除・リネーム・安全クラス変更・
confirm-token 対象変更はゼロ。`_operation` 包絡の構築コードはバイト一致
（キー追加なし）。** strict 製品モデルが壊れる方向の変更は検出されなかった
（#1 リスク = 「追加フィールドで strict モデルが落ちる」は今回、包絡も
製品が消費する result 形も守られている — 詳細は下記「#1 リスク検証」）。
フェイルクローズする項目は **0 件**。

## 数値のまとめ

| 面 | v2.207.0 | v2.210.0 | 差分 |
|---|---|---|---|
| compound ツール数 | 36 | 36 | 変更なし |
| compound アクション総数 | 748 | 750 | +2（削除 0）|
| granular ツール | 353 | 353 | 変更なし（`common.py` は版数文字列のみ）|
| advanced（Node）ツール/アクション | 18 / 154 | 同一 | `resolve-advanced/`・`bin/` に差分ファイルなし |
| kernel カタログ行 | 145 | 145 | 変更なし |
| confirm-token 保護 action | 15 | 15 | 変更なし |
| `_operation` 包絡キー | status/operation/execution_id/verification(+duration_ms/changes/warnings) | 同一 | ビルダー変更 0 |

## Compound（Python サーバ、`src/server.py`）

- 削除ツール 0 / リネーム 0 / 引数変更 0 / 安全クラス変更 0
- **新 action 2 件**（`resolve_control` のトレース観測ファミリー拡張）:
  - `resolve_control.inspect_operation` — 呼び出し前のリスク事前検査（pre-flight）
  - `resolve_control.list_lifecycle_hooks` — 登録済みライフサイクルフック一覧
- `edit_engine.execute_tighten` / `execute_silence_ripple` の `audio_accounting`
  ブロック（v2.208.1）: 集計元が「append 応答」から「variant timeline の再読み取り」に
  変わり、`counts_source` と `placed_item_counts` が増える。**製品コードに
  `audio_accounting` / `placed_item_counts` の消費者は 0 件**（grep 済み）なので
  影響なし

## #1 リスク検証（v2.207.0 切替時の `kind` 事故の再発防止チェック）

1. **包絡（`_operation`）の構築コードはバイト一致**: `src/utils/operation_result.py`
   は両版で同一ファイル。包絡キー（status, operation, execution_id, verification,
   任意で duration_ms/changes/warnings）に追加・削除なし
2. **ただし実行時に包絡へ乗る追加がある**: 新設 `execution_lifecycle` の既定フック
   （5 個、すべて観測のみ）が `after_tool_call` で寄与を返すと
   `_operation.lifecycle` サブ辞書が乗る。仕様上ショートサーキット無し
   （`test_no_default_hook_short_circuits` で固定）。製品側は
   `ops_models.py` が全モデル `extra="ignore"` で受けるため許容、
   `response_normalize._parse_payload` は `_operation` を境界で除去するため
   strict ドメインモデル（`ToolInfo` の `extra="forbid"` は tools/list 専用で
   包絡を含まない）には届かない
3. **result 形の変更は消費者なし**: 上記 audio_accounting のみ。製品の strict モデル
   （`response_normalize`）が解析する面（tools/list、media_analysis 標準レポート等）
   に変更差分なし。granular / advanced は完全不変
4. **tools/list の inputSchema**: compound ツールの入力は `(action, params)` 包絡の
   ままで不変 → 既存 36 ツールの `input_schema_sha256` は変わらない見込み。
   Phase 2 の live inventory で最終確認する

## フェイルクローズ影響（詳細は `fail-closed-impact.json`）

| 検査対象 | 件数 | v2.210.0 で失敗するもの |
|---|---|---|
| SURFACE_OPERATION_BINDINGS（surface→operation）| 66 | 0 件 |
| OPS_READ_OPERATION_BINDINGS（operation→McpOps）| 11 | 0 件 |
| dispositions 行 | 1026 | 0 件（孤立なし）|

Phase 2 で必要になる追加作業: 新規 2 operation
（`resolve_control.inspect_operation` / `resolve_control.list_lifecycle_hooks`）への
処遇行追加（推奨: category=read, route=deferred — 実行トレース系は v2.207.0 時に
deferred 扱いとしたのと同じ判断）と、inventory の再生成。既存行の修正は不要。

## その他の recording（運用への軽微な影響）

- `destructive.audit_log` 既定 True → 高リスク destructive action 実行時に clone 配下
  `logs/security-audit.jsonl` への追記が始まる（confirm_token は編集済み）。
  製品の単一 Writer 規律には抵触しない（vendor clone 内の観測ログ）
- `destructive.safe_mode` 既定 False → 既定動作は v2.207.0 と同一
- `requirements.txt` 変更なし（依存ピン不変）。`package.json` / `package-lock.json` /
  `install.py` は版数文字列のみ
