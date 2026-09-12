# MCP v3.2.0 切替波（Phase 1-5 実行記録 — 2026-09-12）

v2.210.0（commit `4c42f429298ec63fd793e7a9d16bd7994d16f307`）→ **v3.2.0（commit
`c8fbe1887324de9d897e6036efcde60417e33e8c`、リリースページで実行時に再確認済み）**。
Codex 指示の Phase 1-5 を実行。**client-facing switch（`.mcp.json` / vendor 昇格 /
session 再起動 / fresh handshake）はこの後のオーケストレータ Phase**。

## 並列チェックアウト（昇格なし・ロールバック可能）

- 新 checkout: `private/vendor/davinci-resolve-mcp-v3.2.0/`（detached HEAD = pinned
  commit、clean）。既存 `private/vendor/davinci-resolve-mcp/`（v2.210.0）は
  **無変更**（HEAD・venv・`logs/update-check.json` すべて現状維持、server 起動まで実測）。
- venv: 同 checkout 配下 `venv/`、Python 3.12.10、`mcp` 1.30.0、`pyaaf2` 1.7.1、
  `install.py --update-policy never` で `update_mode=never` 永続化済み。
- `resolve-advanced/`: `npm ci` + build 成功（`bin/davinci-resolve-advanced-mcp.mjs`）、
  Node v24.19.0（>= 20.9 条件を満たす）。
- pin の `venv_python` は新 checkout の venv を指す（canonical 名への昇格は未実施 —

## オフライン検証（pin 切替前）

- `initialize`: `DaVinciResolveMCP 1.30.0`（MCP SDK handshake）。
- `tools/list`: compound 37（既存 36 の `inputSchema` sha256 は **drift 0**、追加 `lut`）。
- 実 response parse（稼働中 Resolve 21.0.4.5 対）： `get_version` は `product` /
  `version_string` / `mcp.version=3.2.0` / `update_mode=never` を parse。
- 削除 2 操作の live 確認： `script_plugin.run_inline` / `script_plugin.execute` は
  明示的な削除エラー（"removed in v3.0.0: this server does not execute
  caller-supplied code"）。

## 同一変更セットで更新した製品側の固定値

- `video-pipeline/config/toolchains/davinci-resolve-mcp.pin.json`: commit → `c8fbe18…`、
  `venv_python` → 新 venv、`pinned_date` 更新。
- `services/toolchain/mcp_fit.py`: `EXPECTED_PROVIDER_VERSION = "3.2.0"`。
- `services/mcp_client/version_pin.py`: `PINNED_HANDSHAKE_VERSION = "1.30.0"`。
- `services/toolchain/mcp_coverage_models.py`: compound 36→**37**、granular 353→**384**、
  kernel 136（不変）。
- `capabilities/mcp-coverage/`: inventory 再生成（37 / 384 / 136、operation
  **1,028 → 1,090**（+64 / −2）、既存 operation の `input_schema_sha256` drift 0）、
  dispositions 再構築（削除 2 行を除去、+64 行はすべて `deferred`。sealed:
  `pin_commit` + `inventory_sha256=3d6ceac94779…`）、manifest 再生成。
- `capabilities/v4.4/mcp-fit.json`: header + 22 行の `provider_version` → 3.2.0
  （status / readback / evidence は今回の live run で更新）。
- `services/mcp_client/response_normalize.py`: v3 の
  `build.unavailable_on_this_build` が文字列 → オブジェクト配列に変わったため
  `UnavailableOnThisBuildEntry` を追加（strict・fail-closed）。live 実測は probe run の
  session open で確認（v2 モデルのままでは session open が型エラーで停止 = 想定どおりの
  fail-closed 動作）。
- test fixtures: `fake_server.py` / `pin_backed_stub.py` / `server-info.json` /
  `test_client.py` / `test_response_normalize.py` の handshake 1.30.0 化、
  `test_mcp_coverage.py` の COMMIT / counts / domains 更新。
- `docs/capability-map-social.md`: F06 / N06 の `run_inline` 再現経路に
  「v3 では再実行不可」の注記（N06 の代替は型付き mark in/out API）。

## dispositions: 削除 2 操作（歴史記録）

`script_plugin.execute` と `script_plugin.run_inline` は现行表から除外し、
`removed-operations.json`（本ディレクトリ）に完全な行を保存。両行とも v2 では
`deferred` であり、製品 route は接続していなかった（影響 0）。

## 追加 64 操作の処遇（すべて deferred — 非採用）

- read 31 行（owner `task13-read-mapping` → `task13:read-session`）: LUT 読取系
  （`lut.list/path/read/capabilities`、`graph.get_lut_*`、`graph.list_lut_files`）、
  Resolve 21.1 read（`resolve_211.get_*` 11 + `is_resolve_studio`）、keyboard preset、
  audio render format/codec、`render.get_audio_*`、fade/speed/blanking/normalize の
  get 系、`resolve_control.is_studio`。
- guarded_mutation 33 行（owner `task15-guarded-mapping` → `task15:guarded-handler`）:
  LUT install/remove/attenuate、`dctl.encrypt_native`、`graph.read_lut_file` を含む
  書込系、Resolve 21.1 write（transition、multicam、fade/speed/blanking/normalize の
  set 系、`validate_dctl_native`）、`timeline_item_color.apply_trace_plan`
  （confirm-token 対象 +1）、`resolve_control.report_issue`。
- Resolve 21.1 専用操作は「pinned Resolve build は 21.0.4」を reason に明記。
  v3 新機能（LUT 書込、21.1 専用）は本波では**非採用**（PRD v4.4 §0.1 の凍結規則）。

## live 回帰（22 probe、使い捨て project、1 回のみ）

- `pytest -m mcp_live test_live_probes.py::test_live_capability_probes_fill_matrix_and_snapshot`
  を pin 先 v3.2.0 server + Resolve 21.0.4.5 で実行。使い捨て project
  `v43-probe-144950` は `close_live_session` の raw `project_manager.delete` が
  **v3 の delete gate に拒否**されたため残存 → `safe_project_delete`
  （`allow_non_mcp_name=True` + `close_current=True`。gate の要求する flag は
  エラーメッセージが案内）で削除し、残置 0 を確認。
- 結果: **17 accepted / 5 failed**。failed は v2.210.0 行列と同一の既知失敗
  （transition-path / audio-property-operation / bgm-track-ducking /
  edit-engine-selects / alternate-shot-similarity）。**新規 failure 0、regression 0**
  （codex 基準: ≥17 accepted ∧ 新規 failure 0 を満たす）。
- probe は型付き `McpOps` 経路で駆動 → v3 の `_operation` / result 拡張が
  製品モデルで受容されることを実測。
- mcp-doctor 12 セクションすべて ok（`--clone` は新 checkout を絶対パスで指定。

## オフライン試験

- `tests/mcp_client tests/toolchain tests/mcp_execution`: **580 passed / 4 skipped**
  （4 skip は live マーカー類）。ruff 全pass、basedpyright 0 errors
  （変更 file 群）。

## ロールバック

- v2 checkout / venv は無変更で起動を実測（`DaVinciResolveMCP 1.29.1` 応答）。
- `.mcp.json` は未変更（現行 session は依然 v2 で稼働）。
- ロールバック = 本変更セットの revert のみ（pin が v2 の commit / venv path /
  1.29.1 / 旧 inventory に戻り、v2 vendor は実体が残っている）。

## 未実施（次 Phase = orchestrator）

- `.mcp.json` の指向先変更 / vendor ディレクトリ昇格、全 client session 再起動、
  fresh `get_version`・doctor・handshake の確認。
- base-cut parity / backend flag transitions / episode0 freeze（本波の codex 指示は
  22 probe のみ。既知の episode0 未通過は実素材欠損で pin 起因ではない）。
