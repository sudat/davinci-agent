# v2.207.0 切替提案（Phase 2 用 — ピンはまだ切り替えていない）

> **実行記録（2026-09-05 追記）**: 本書の手順は Phase 2 で実行済み（commit `bd8ed39`、
> live 検証込み）。ただし**「ディスクの切り替え」と「実プロセスの切替」は別物**:
> P1 の worktree 昇格後も、**切替前に起動していた MCP サーバ・プロセスは旧コード
> (v2.98.3) を載せたまま動き続ける**（Python は import 済みモジュールをキャッシュする
> ため）。製品クライアント（`McpClient.from_pin`）は接続ごとに新規プロセスを起動する
> ため即座に v2.207.0 を実行するが、**セッション起動時に張られた MCP 接続
> （`.mcp.json` 由来の長寿命プロセスを含む）はサーバ再起動＝セッション再起動まで
> 旧コードのまま**。切替完了の定義は「次回セッション起動時に v2.207.0 が実際に走る
> こと」までを含む。ディスクを差し替えただけで完了と誤解しないこと。

対象: `video-pipeline/config/toolchains/davinci-resolve-mcp.pin.json`（以下「pin」）、
`video-pipeline/capabilities/mcp-coverage/`（inventory / dispositions / manifest）、
`video-pipeline/services/toolchain/mcp_fit.py`。

本書は Phase 1（オフライン）の成果物であり、**pin の切り替え・チェックアウト更新・
.mcp.json 変更は一切行っていない**。以下をそのまま実行すれば Phase 2（live）の切替は
機械的な編集になる。

## 前提（Phase 1 で済んでいること）

- tag `v2.207.0`（`5a1db6776fbe76098a706a41811ca787dfbd9990`）の worktree が
  `private/vendor/davinci-resolve-mcp-v2.207.0` にある（venv 無し・`.mcp.json` 未接続）
- 現行 `private/vendor/davinci-resolve-mcp/` は `132e134`（v2.98.3）・クリーンのまま
- 表面差分・フェイルクローズ影響・v2.207.0 baseline 記録を
  `capabilities/v4.4/mcp-2.207.0/` と `capabilities/v4.4/baseline/mcp-2.207.0/` に格納済み

## P1 — チェックアウトの切替（live）

worktree を昇格させる手順（旧チェックアウトは v2.98.3 のまま温存し、いつでも戻せる）:

```bash
cd private/vendor
mv davinci-resolve-mcp davinci-resolve-mcp-v2.98.3-retired
git -C davinci-resolve-mcp-v2.207.0 checkout --detach 5a1db6776fbe76098a706a41811ca787dfbd9990  # 念のため再固定
mv davinci-resolve-mcp-v2.207.0 davinci-resolve-mcp
git -C davinci-resolve-mcp worktree prune   # worktree 登録の後始末（davinci-resolve-mcp 側で実行）
```

venv は新チェックアウト配下に作り直す（pin の `venv_python` は
`.../davinci-resolve-mcp/venv/bin/python` を指すためパスは不変）:

```bash
cd private/vendor/davinci-resolve-mcp
python3.12 -m venv venv && venv/bin/pip install -r requirements.txt
DAVINCI_RESOLVE_MCP_UPDATE_CHECK=0 venv/bin/python install.py --update-policy never   # update-check.json を永続化
```

注意: `resolve-advanced`（Node）は `package-lock.json` があるため
`npm ci` を `resolve-advanced/` で実行する（Node >= 20.9 であることを mcp-doctor の
新チェック `node_version` で確認できる）。旧 venv・旧 `logs/update-check.json` は
新チェックアウトに引き継がれないため、上記 install.py 実行が必須（doctor の
`update_check` セクションは `update_mode=never` の永続化を検査する）。

## P2 — pin と期待値の編集（同一変更セットで）

`video-pipeline/config/toolchains/davinci-resolve-mcp.pin.json`:

```json
"commit": "5a1db6776fbe76098a706a41811ca787dfbd9990",
"pinned_date": "<切替日時 ISO8601>"
```

（`venv_python` はパス文字列が不変なので無編集。他のキーは変更不要。）

`video-pipeline/services/toolchain/mcp_fit.py`:

```python
EXPECTED_PROVIDER_VERSION: Final = "2.207.0"
```

`mcp-inventory` CLI は `EXPECTED_PROVIDER_VERSION` と vendor `VERSION` の一致を検査する
ため、この 2 編集は必ず同じコミットに入れる。

## P3 — inventory / dispositions / manifest の再生成（live）

1. **inventory**: `python -m services.cli.mcp_inventory`（既定の pin/clone/out 先は
   `capabilities/mcp-coverage/`）。live `tools/list` を取り直すため Phase 2 で実施。
   期待値: compound 36 ツール / 748 action、既存 35 ツールの `input_schema_sha256` は
   v2.98.3 と同一のはず（差分が出たら schema-changed として精査）
2. **dispositions**: 新規 25 operation への行追加（既存 1001 行は無修正）。推奨初期値:
   - `knowledge.topics|get|search|capabilities` → category=read, route=deferred(planned)
     （製品からの利用が決まるまで mapped にしない）
   - `media_analysis.grade_loop_capabilities|mix_plan_capabilities` → read
   - `media_analysis.grade_loop|mix_plan|measure_loudness` → 要レビュー（解析系だが
    書き込みを伴うかは Phase 2 の実機確認まで deferred 推奨）
   - `media_pool.capture_media_template|get_clip_marks` → get_clip_marks=read、
     capture_media_template=要レビュー（新規メディア生成の可能性）
   - `media_pool_item_markers.get_name` → read
   - `render.verify_output` → read
   - `resolve_control.begin_execution|clear_executions|end_execution|
     export_execution_report|get_execution|get_execution_trace|list_recent_executions`
     → 実行トレース機能。begin/end は session_control 候補だが SESSION_CONTROL_OPERATIONS
     の審査リストに追加しない限り mapped にできないため deferred 推奨
   - `timeline.author_offline|offline_fallback_capabilities` → offline_fallback_capabilities
     =read、author_offline=要レビュー（ファイル生成）
   - `timeline.get_clips_linked|get_title_text` → read
   - `timeline.ripple_insert` → confirm-token 保護（`_TOKEN_GATED_DESTRUCTIVE_ACTIONS` 追加
     済み）のため category=guarded_mutation が必須
   - seal（`pin_commit` / `inventory_sha256`）と `manifest.json` は inventory 再生成と
     同じ変更セットで更新すること（`mcp_surface_artifacts` が三者不一致を型エラーにする）
3. **mcp-doctor**: 切替後に `python -m services.cli.mcp_doctor` が全セクション ok である
   こと（新 `node_version` チェックを含む）

## P4 — 実施しないこと（Phase 2 でも）

- `drt.assemble` / `spec.subtitles` / `spec.subtitlesSrt` の製品組み込み（ネイティブ字幕
  作画）は本アップグレードの範囲外。能力記録（`baseline/mcp-2.207.0/vendor-surface.json`）
  のみ保持し、組み込み判断は別タスク（CLAUDE.md §1.1: 実素材の失敗から機能追加）
- v2.98.3 の historical 記録（`capabilities/mcp-coverage/` の現行ファイルは切替時の
  上書き生成対象だが、`capabilities/v4.3/**` は不変のまま）

## リスクと対処

| リスク | 影響 | 対処 |
|---|---|---|
| 既存ツールの schema hash が変わる | surface gate が drift で停止 | 差分が出た該当ツールのみ精査。静的解析では署名不変を確認済み |
| venv 再作成後の依存ドリフト | server が起動しない | requirements.txt は pin 済み。doctor の server_alive で検出 |
| worktree 昇格時の git 状態 | vendor repo の branch 状態が乱れる | 昇格前に `git worktree list` を記録し、prune で掃除。旧 v2.98.3 チェックアウトは retired 名で温存 |
