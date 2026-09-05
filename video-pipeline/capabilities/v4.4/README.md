# v4.4 証跡ツリー — First Publishable Real Episode

このディレクトリは `v44-real-01` による最初の公開可能エピソード（Gate V44-0 → V44-1 → V44-2）の証跡を集約します。`v4.3/**` は歴史的証拠として不変です（書き換えません）。

## ツリー構成

```
capabilities/v4.4/
  baseline/
    baseline.json          # T2 freeze: baseline_commit_sha=c37247e..., pytest 2995 passed / ruff+pyright clean / mcp-fit sha / backends / v43 gate evidence
    manifest.json          # manifest-v1 (path+size+sha256, sorted, compact canonical JSON)
    mcp-2.207.0/           # MCP v2.207.0 オフライン表面 baseline（vendor-surface.json + manifest.json、Phase 1）
  mcp-2.207.0/             # MCP v2.98.3→v2.207.0 差分分析（Phase 1、オフライン）:
                           #   surface-diff.json/.md, fail-closed-impact.json, update-proposals.md, manifest.json
  mcp-fit.json             # 現行 capability 行列（mcp-fit-v1、MCP v2.207.0 実測 2026-09-05、22 行）
  runs/                    # v2.207.0 実測ラン（Phase 2、2026-09-05）:
                           #   probes/（22 capability ログ + ledger）、mcp-capability-snapshot.json、manifest.json
  product-proof/
    v44-0/BLOCKED.json     # Gate V44-0 BLOCKED (operator-needed)
    consolidated.json      # ProductProofReportV1 run_kind=v44-consolidated（本統合作成物）
    manifest.json
  live-runs/
    v44-1/BLOCKED.json     # Gate V44-1 BLOCKED (operator-needed)
    v44-2/BLOCKED.json     # Gate V44-2 BLOCKED (operator-needed)
    manifest.json
  README.md                # 本ファイル
```

各 `manifest.json` は `services/release/manifest.py` の `collect_entries` / `manifest_bytes` パターン（`manifest-v1`、entries は path でソート、compact canonical JSON、sha256 は `sha256_file` で計算、manifest.json 自体は除外）で生成しています。検証は `shasum -a 256` の再計算ループで可能です。

## ゲート状態（すべて BLOCKED — passed ではありません）

| Gate | 状態 | 理由 | 証跡 |
|------|------|------|------|
| V44-0（Editorial Feasibility + Evidence Quality） | **BLOCKED (operator-needed)** | 実素材空 / ground-truth placeholder / transcript-sample placeholder / 認証情報未設定 | `product-proof/v44-0/BLOCKED.json` |
| V44-1（Real First-Preview Vertical Slice） | **BLOCKED (operator-needed)** | 実素材空（絶対パス要） / V44-0 未通過 / Cockpit ライブ操作未実施 | `live-runs/v44-1/BLOCKED.json` |
| V44-2（First Publishable Real Episode） | **BLOCKED (operator-needed)** | V44-0/1 未通過 / 実素材空 / ground-truth+transcript placeholder / kit-selections 未記録 / subtitle-proof 未準備 / publishability+time-log 未記録 | `live-runs/v44-2/BLOCKED.json` |

いずれのゲートも `passed` として扱いません。`consolidated.json` でも `blocked` として記録しています。

## アンブロック手順（各 BLOCKED.json の operator_instructions 要約）

正本は各 `BLOCKED.json` の `operator_instructions` と `video-pipeline/docs/runbooks/v44-first-publish.md` です。以下は要約です。

### V44-0 をアンブロックする

1. 素材配置: カメラ素材を絶対パスで確認しつつ `private/reference-episodes/v44-real-01/sources/` に配置する。
   ```bash
   cd video-pipeline
   uv run python -m services.cli.v44_real01 prepare --episode /Users/stc/Developer/davinci-agent/private/reference-episodes/v44-real-01 --source-folder /path/to/camera/footage
   uv run python -m services.cli.v44_real01 status --episode /Users/stc/Developer/davinci-agent/private/reference-episodes/v44-real-01  # 0 file(s) でないこと
   ```
2. Ground truth: 実験出力を見る前に
   ```bash
   uv run python -m services.cli.v44_product_proof init-ground-truth --episode-id v44-real-01 --out /Users/stc/Developer/davinci-agent/private/reference-episodes/v44-real-01/ground-truth.json
   ```
   を実行し、`must_keep / good_optional / must_remove / uncertain` アンカーを日本語メモ付きで記入する。
3. 書き起こしサンプル: 2〜3 分の難読固有名詞を含む区間で `transcript-sample-corrected.json` を `TranscriptSampleV1`（`v44-transcript-sample-v1`, `segments:[{start_ms,end_ms,text}]` + `proper_nouns` オブジェクト）として手動作成する。
4. 認証情報: `EDITORIAL_DIRECTOR_API_KEY` と `EDITORIAL_DIRECTOR_NETWORK_ENABLED=1` を環境に設定する（ログに出さない）。
5. その後 Task 14（V44-0 実験 A/B/C）を再実行する。詳細は `private/reference-episodes/v44-real-01/README.md` と runbook を参照。

### V44-1 をアンブロックする（V44-0 通過後）

1. 上記素材・V44-0 を解消する（`product-proof/v44-0/BLOCKED.json` の未充足をすべて解消）。
2. サーバー起動:
   ```bash
   cd video-pipeline && uv run python -m services.cli.cockpit --port 8765 --episodes-root jobs/episodes --state-store jobs/state.db
   cd cockpit && COCKPIT_API=http://127.0.0.1:8765 bun dev --port 3100
   ```
3. Cockpit UI `/new-episode` で source folder を指定して intake → `PREVIEW_READY` まで実パイプライン実行を監視 → preview を視聴 → 自然言語で 1 件以上の修正指示 → 必要なら曖昧さを確認 → 適用 → 部分再ビルド完了を監視（`POST /rebuild` は 202）。
4. 観測:
   ```bash
   uv run python -m services.cli.v44_real01 observe-v44-1 --episode <cockpit-ep-id> --out <out-dir> --episodes-root jobs/episodes --state-store jobs/state.db
   ```
   `observation.json` が `v44-1-observation-v1` を満たし、`ttfrp_seconds` が numeric、`corrections >=1`、`rebuild_records` が壁時計を持つこと、`internal_path_leak` が false であることを確認する。
5. `uv run python -m services.cli.v44_real01 record-operator-note --episode <cockpit-ep-id> --note "この選択で続行したい" --episodes-root jobs/episodes` でサインオフする。

### V44-2 をアンブロックする（V44-0/1 通過後）

正本は runbook §0〜§1.8 です。

1. 前提: V44-0/1 合格または再ベースライン記録済み、実素材配置済み、T11 の 3 ドメイン（subtitle/audio/color）選択済み（`<ep>/kit-selections.json` が存在）。
2. 仕上げ:
   ```bash
   cd video-pipeline
   uv run python -m services.cli.v44_finishing run --episode-root <ABSOLUTE path> --executor live --qc-policy <ep>/finishing/qc-policy.json
   ```
3. 最終プレビュー（`<ep>/finishing/final-preview/preview.mp4`）を視聴し、
   ```bash
   uv run python -m services.cli.v44_finishing record-publishability --episode-root <ABSOLUTE path> --verdict {publishable|publishable_after_fixes|not_publishable} [--comments ...] [--dimension-comments domain=text]
   uv run python -m services.cli.v44_finishing record-time --episode-root <ABSOLUTE path> --phase {ordinary_review|kit_bootstrap|taste_calibration|troubleshooting|direct_resolve} --minutes N
   uv run python -m services.cli.v44_subtitle_proof --audio <主要カメラファイル> --sample <transcript-sample-corrected.json> --proper-nouns <proper-nouns.json> --out-dir <ep>/finishing/subtitle-proof
   uv run python -m services.cli.v44_finishing gate-summary --episode-root <ABSOLUTE path> [--out <出力先> --commit-sha <40-hex> --subtitle-proof-ref <パス>]
   ```
   ゲートサマリライターは `passed: true` を証拠が揃った状態でのみ発行し、ブロックドメイン・不足する publishability・QC blocked・AHT 未記録があれば `passed: true` を拒否または `BLOCKED` を返します。

## 統合レポート（consolidated.json）

`product-proof/consolidated.json` は `ProductProofReportV1`（`run_kind="v44-consolidated"`）です。

- `run`: `episode_id=v44-real-01`, `commit_sha=<現行 HEAD>`, `model_pin=gpt-5.6-sol`, `analysis_provider_pin=whisper-cpp-cli:<sha12>`
- `editorial / progressive_lift / evidence_quality`: `null`（実験未実行 — ゲートが BLOCKED のため）
- `operator`: `continuation_yes_no=null`, `publishability=null`, `comments=<日本語の BLOCKED 要約と runbook 参照>`
- `efficiency`: `null`（AHT/TTFRP は未計測、bootstrap は 30 分目標の対象外）
- `notes / summary`: ゲート状態（V44-0/1/2=blocked）、工学的に PROVED なこと（production model の typed blocking、real deep-review providers + lineage guard、cockpit real launch + live E2E green、real partial rebuild、kit preview、JP subtitle harness with real ASR fixture、finishing harness + gate-summary anti-fabrication）、未証明なこと（実素材での編集品質仮説、publishability）、V44-3/4/5 へのポインタを記載

`notes` と `operator.comments` の二層で、機械検証（`passed` は pending の間は常に false）と人間の可読性を両立しています。

## 検証

```bash
# manifest ハッシュの再計算検証（stale-state 対策）
cd video-pipeline
for d in capabilities/v4.4/baseline capabilities/v4.4/product-proof capabilities/v4.4/live-runs; do
  echo "== $d ==";
  python -c "from pathlib import Path; from services.release.manifest import build_manifest, manifest_bytes; import json; m=build_manifest(Path('$d')); print(manifest_bytes(m).decode())" | python -m json.tool
  # 各 entry の sha256 を shasum で再計算して突合
  for f in $(python -c "from pathlib import Path; from services.release.manifest import build_manifest; m=build_manifest(Path('$d')); print('\n'.join(e.path for e in m.entries))"); do
    echo -n "$f: "; shasum -a 256 "$d/$f" | cut -d' ' -f1
  done
done

# v4.3 不変確認
git status -- capabilities/v4.3  # 変更なしであること
```

## V44-3 以降のスロット（予約のみ — 実装しない）

- `live-runs/v44-3/` — Publication proof（公開証明）の成果物を将来ここに配置します。現時点では予約のみで、何も作成しません。
- V44-4（定常計測: 次エピソードでの AHT 計測）、V44-5（学習活性化: reference/channel learning）は Gate V44-2 通過後に着手します。`consolidated.json` の notes/summary にポインタのみを記載しています。

## 参照

- `video-pipeline/docs/runbooks/v44-first-publish.md` — オペレーター手順の正本
- `video-pipeline/capabilities/v4.3/` — 歴史的証拠（不変）
- `video-pipeline/services/release/manifest.py` — manifest-v1 実装
- `video-pipeline/services/metrics/v44_product_proof.py` — ProductProofReportV1 スキーマ
