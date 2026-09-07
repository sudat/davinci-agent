# v2.210.0 切替提案（Phase 2 用 — ピンはまだ切り替えていない）

> **実行記録（2026-09-07 追記）**: 本書の手順は Phase 2 で実行済み（live 検証込み）。
> - pin: commit `4c42f429298ec63fd793e7a9d16bd7994d16f307`（tag `v2.210.0` は注釈タグ —
>   pin は commit を刻む。tag オブジェクト `362a4501` ではない）、
>   `EXPECTED_PROVIDER_VERSION=2.210.0`、`PINNED_HANDSHAKE_VERSION=1.29.1`（不変）
> - inventory: 36 compound ツール / 750 action / 1028 operation。**既存 1026 operation の
>   `input_schema_sha256` は v2.207.0 と全件一致（drift 0）**、新規 2 operation は
>   read/deferred 行を追加し reseal。`inspect_operation` は read 型名検証を通すため
>   `READ_ACTION_EXCEPTIONS` に review 済み例外として追加（実装確認済み: リスク分類のみ
>   で対象 action を実行しない）
> - mcp-doctor 全 12 セクション ok（server_alive / node_version v24.19.0 / update_check
>   never を含む）
> - **製品モデル parse 検証**: 22 probes は生呼び出しではなく型付き `McpOps` 表面
>   （`ops_models` 経由）で駆動 — v2.210.0 の `_operation.lifecycle.risk_level` 追加が
>   live で観測され、製品側で許容されることを実測（ops models `extra="ignore"` /
>   response_normalize は `_operation` を境界で除去）
> - probe 結果: **17 accepted / 5 failed**。failed 5 件は v2.207.0 行列と同一の既知失敗
>   （transition-path / audio-property-operation / bgm-track-ducking /
>   edit-engine-selects / alternate-shot-similarity）で、回帰なし
> - 既知の未通過: `test_episode0_freeze_manifest_live` は実素材欠損
>   （`private/reference-episodes/real-01/…MP4`、オペレーター-blocking、V44-0 BLOCKED
>   同一クラス）で失敗が継続 — pin 起因ではない
> - 切替完了の定義: 「次回セッション起動時に v2.210.0 が実際に走ること」までを含む
>   （本セッション起動分の MCP 接続はセッション再起動まで旧プロセスのまま）
> - worktree 教訓: 昇格のリネーム後に `worktree prune` を旧登録のまま実行すると
>   自身の登録を削除する（今回発生・手で修復、journal 参照。prune はリネーム前に
>   実行するか main repo 側から行う）

> **引き継ぎチェックリスト（v2.207.0 時の実害発見より、今回も適用）**:
> probe は生呼び出しだけでなく、**製品モデルで parse するところまで通すこと**。
> strict モデル（extra禁止）は**追加フィールドでも落ちる**。Phase 1 の静的検証で
> 包絡（`_operation`）構築はバイト一致・result 形変更は製品消費者なしを確認済み
> （`surface-diff.md` の「#1 リスク検証」）だが、Phase 2 の live 結果で最終確認する。

対象: `video-pipeline/config/toolchains/davinci-resolve-mcp.pin.json`（以下「pin」）、
`video-pipeline/capabilities/mcp-coverage/`（inventory / dispositions / manifest）、
`video-pipeline/services/toolchain/mcp_fit.py`。

本書は Phase 1（オフライン）の成果物であり、**pin の切り替え・チェックアウト更新・
.mcp.json 変更は一切行っていない**。

## 前提（Phase 1 で済んでいること）

- tag `v2.210.0`（`4c42f429298ec63fd793e7a9d16bd7994d16f307`）の worktree が
  `private/vendor/davinci-resolve-mcp-v2.210.0` にある（クリーン・`.mcp.json` 未接続）
- 現行 `private/vendor/davinci-resolve-mcp/` は `5a1db67`（v2.207.0）のまま
- 表面差分・フェイルクローズ影響・v2.210.0 baseline 記録を
  `capabilities/v4.4/mcp-2.210.0/` と `capabilities/v4.4/baseline/mcp-2.210.0/` に格納済み
- 差分要約: compound 748→750 action（+2、削除 0）、包絡ビルダー バイト一致、
  granular/advanced/kernel 不変、`requirements.txt` 変更なし

## P1 — チェックアウトの切替（live）

worktree を昇格させる手順（旧チェックアウトは v2.207.0 のまま retired 名で温存し、
いつでも戻せる）:

```bash
cd private/vendor
mv davinci-resolve-mcp davinci-resolve-mcp-v2.207.0-retired
mv davinci-resolve-mcp-v2.210.0 davinci-resolve-mcp
git -C davinci-resolve-mcp worktree prune   # worktree 登録の後始末（昇格後の clone 側で実行）
```

venv は新チェックアウト配下に作り直す（pin の `venv_python` は
`.../davinci-resolve-mcp/venv/bin/python` を指すためパスは不変）:

```bash
cd private/vendor/davinci-resolve-mcp
python3.12 -m venv venv && venv/bin/pip install -r requirements.txt
DAVINCI_RESOLVE_MCP_UPDATE_CHECK=0 venv/bin/python install.py --update-policy never   # update-check.json を永続化
```

注意: `resolve-advanced`（Node）は Phase 1 で差分ゼロだが、worktree は node_modules を
引き継がないため `npm ci` を `resolve-advanced/` で実行する（Node >= 20.9 を
mcp-doctor の `node_version` チェックで確認）。旧 venv・旧 `logs/update-check.json` は
引き継がれないため、上記 install.py 実行が必須。

## P2 — pin と期待値の編集（同一変更セットで）

`video-pipeline/config/toolchains/davinci-resolve-mcp.pin.json`:

```json
"commit": "4c42f429298ec63fd793e7a9d16bd7994d16f307",
"pinned_date": "<切替日時 ISO8601>"
```

`video-pipeline/services/toolchain/mcp_fit.py`:

```python
EXPECTED_PROVIDER_VERSION: Final = "2.210.0"
```

`mcp-inventory` CLI は `EXPECTED_PROVIDER_VERSION` と vendor `VERSION` の一致を検査する
ため、この 2 編集は必ず同じコミットに入れる。

## P3 — inventory / dispositions / manifest の再生成（live）

1. **inventory**: `python -m services.cli.mcp_inventory`。期待値: compound 36 ツール /
   750 action、既存 36 ツールの `input_schema_sha256` は v2.207.0 と同一のはず
   （差分が出たら schema-changed として精査）
2. **dispositions**: 新規 2 operation への行追加（既存 1026 行は無修正）。推奨初期値:
   - `resolve_control.inspect_operation|list_lifecycle_hooks` → category=read,
     route=deferred（実行トレース系は v2.207.0 時も deferred 扱い。製品からの利用が
     決まるまで mapped にしない）
   - seal（`pin_commit` / `inventory_sha256`）と `manifest.json` は inventory 再生成と
     同じ変更セットで更新すること（`mcp_surface_artifacts` が三者不一致を型エラーにする）
3. **mcp-doctor**: 切替後に `python -m services.cli.mcp_doctor` が全セクション ok
   であること（`server_alive` / `node_version` / `update_check` を含む）
4. **製品モデル parse 検証（v2.207.0 時の checklist 項目）**: `get_items_in_track`
   系の live 結果を実際の製品モデル（`ops_models.TrackItemsResult` 経由の
   `McpOps` / 02_apply 型 scan 経路）で parse して通すこと。生呼び出しの緑だけでは
   不十分。加えて今回から `_operation.lifecycle`（既定フックの観測寄与）が包絡に
   乗り得ることを製品経路で確認する

## P4 — 実施しないこと（Phase 2 でも）

- v2.209.x–2.210.0 の safe operations policy（`destructive.safe_mode` 等）の製品側
  有効化は本アップグレードの範囲外。既定値（safe_mode=False / audit_log=True）のまま
  動作を記録するのみ（能力記録は `baseline/mcp-2.210.0/vendor-surface.json`）
- v2.207.0 の historical 記録（`capabilities/v4.4/mcp-2.207.0/`、
  `capabilities/v4.4/baseline/mcp-2.207.0/`、`capabilities/v4.4/runs/`）は不変。
  `capabilities/mcp-coverage/` の現行ファイルは切替時の上書き生成対象
- `drt.assemble` / `spec.subtitles` の製品組み込み判断は引き続き別タスク
  （AGENTS.md §1.1: 実素材の失敗から機能追加）

## リスクと対処

| リスク | 影響 | 対処 |
|---|---|---|
| 既存ツールの schema hash が変わる | surface gate が drift で停止 | Phase 1 静的解析では入力包絡不変を確認済み。差分が出た該当ツールのみ精査 |
| `_operation.lifecycle` の混入 | strict 製品モデルが落ちる | ops_models は `extra="ignore"`、response_normalize は包絡を境界で除去 — Phase 2 の製品モデル parse 検証で実測する |
| 既定フックのオーバーヘッド | 全 action 呼び出しに観測処理 | 観測のみ（ショートサーキット無し、vendor テストで固定）。probes 緑で受入 |
| venv 再作成後の依存ドリフト | server が起動しない | requirements.txt は v2.207.0 から不変。doctor の server_alive で検出 |
| worktree 昇格時の git 状態 | vendor repo の branch 状態が乱れる | 昇格前に `git worktree list` を記録し、prune で掃除。旧 v2.207.0 チェックアウトは retired 名で温存 |
