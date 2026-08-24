# V44 First Publish 運用手順書（Gate V44-2: 最初の公開可能リアルエピソード）

対象読者: オペレーター（あなた）。対象エピソード: `private/reference-episodes/v44-real-01/`。
この文書は Gate V44-2（最初の公開可能な実エピソード）を回すための正確なコマンド列と監視ポイントを示す。
実行の正本は PRD v4.4 §20（Gate V44-2 チェックリスト）と `docs/prd/implementation-plan-v4.4.md` である。

Run every command from `video-pipeline/` (the pipeline root). 実コマンド例の
`bash` ブロックはオフラインでそのまま実行できるリハーサルである。実エピソードに
対する本番コマンドは `text` ブロックに `"$UV_BIN"` / `"$EP"` 表記で示す
（`EP="../private/reference-episodes/v44-real-01"`）。

## 0. 前提条件（順番に確認すること）

1. **Gate V44-0 / V44-1 合格**（またはオペレーターによる明示的な再ベースライン記録）。
   V44-0 が NO-GO のままの場合、この先へ進んではならない。
2. **実素材・グランドトゥルースが揃っていること**: 下記の status コマンドが
   footage / ground truth / sample すべて present を返すこと。

   ```text
   "$UV_BIN" run python -m services.cli.v44_real01 status \
     --episode ../private/reference-episodes/v44-real-01
   ```

3. **T12 の注意（文字起こしサンプル）**: `transcript-sample-corrected.json` は
   T5 の `TranscriptSampleV1` 形式（`schema_version: "v44-transcript-sample-v1"`、
   `segments: [{start_ms, end_ms, text}]` + `proper_nouns: {id: 表記}` の**オブジェクト**）で
   入力すること。T6 が置いた古いプレースホルダ（`sample: null`）のままでは T12 ハーネスは
   `sample-parse-error` で失敗する（正しい失敗）。記入例は
   `video-pipeline/tests/fixtures/v44/sample-corrected.json` を参照。
4. **本番エディトリアル・トランスポート（2択、正本は `config/editorial-runtime.json` の `transport` フィールド）**:
   - **`codex-exec`（デフォルト）**: Codex サブスクリプション経由。事前に
     `codex login` 済みであること（codex-cli が PATH にあること）。API キー不要。
     未ログインの場合は `production-model-unavailable` でブロックされる
     （メッセージに `codex login` が示される。仕様どおりの正しい失敗）。
   - **`openai-api`**: OpenAI API キー経由。`EDITORIAL_DIRECTOR_API_KEY` +
     `EDITORIAL_DIRECTOR_NETWORK_ENABLED=1` の両環境変数が必要。
   - どちらのトランスポートでも `v44-real-01` は episode_id が `test-` 始まりで
     ないため本番ゲートは免除されず、ゲート不通はブロックされる（テスト用
     バイパスなし）。モデルピンはトランスポートによらず `gpt-5.6-sol`。
   - 確認コマンド（オペレーター実行; `bash` ブロックはオフライン確認用）:

   ```bash
   # オフライン確認: どちらのトランスポートが設定されているか
   uv run python -c "import json; print(json.load(open('config/editorial-runtime.json'))['transport'])"
   ```

   ```text
   codex login status
   ```
5. **キット A/B 済み**: `<episode-root>/kit-selections.json` に subtitle / audio / color
   各ドメインのオペレーター選択が記録済みであること（T11 の Cockpit 画面または
   `POST /episodes/{id}/kit-previews/<domain>/select`）。
   **注意: audio か color で「どちらも不要/現状維持」を選ぶと、そのドメインは
   仕上げプランなし＝`blocked`（品質ゲート却下）になる。** 公開可能判定を取るには
   3ドメインすべてでレシピを選ぶこと。意図的にスキップする場合は、その理由を後述の
   `record-publishability --dimension-comments` に残すこと。
6. **Resolve + ピン留め MCP サーバ**: DaVinci Resolve Studio を起動し、ピン留め
   `davinci-resolve-mcp` サーバ（`config/toolchains/davinci-resolve-mcp.pin.json`）を
   利用可能にしておく（`--executor live` は起動時に接続を試み、失敗なら
   `mcp-server-unreachable` で即ブロック。ハングしない）。
7. **単一ライターの注意**: この CLI は手動実行サーフェスで、runner.lock を取らない。
   Cockpit の runner（初回パイプライン/部分リビルド）が同じエピソードで動いていないことを
   確認してから実行すること（T9 issues.md 記録の既知のギャップ）。

## 1. V44-2 コマンド列（準備 → 仕上げ → 記録）

### 1.1 コマンド面の確認（オフライン）

登場する 3 つの CLI のヘルプを出して、コマンド面を確認する（何も書き換えない）:

```bash
uv run python -m services.cli.v44_finishing --help
uv run python -m services.cli.v44_subtitle_proof --help
uv run python -m services.cli.v44_real01 --help
```

### 1.2 リハーサル: レコーダと型付き拒否（スクラッチ、オフライン）

本番前に、スクラッチディレクトリで recorder のラウンドトリップと「未完了状態での
型付き拒否」を確認する（実エピソードに触れないのでいつでも実行できる）:

```bash
V44T="$(mktemp -d)"
# record-time: bootstrap の時間ログが追記されるか
uv run python -m services.cli.v44_finishing record-time \
  --episode-root "$V44T" --phase ordinary_review --minutes 1.5
# record-publishability: 仕上げレポートが無い状態では型付き拒否 (exit 1)
uv run python -m services.cli.v44_finishing record-publishability \
  --episode-root "$V44T" --verdict publishable 2>&1 \
  | grep -q "finishing-report-missing" && echo "refused: finishing-report-missing"
# run: コミット済み成果物が無い状態でも型付き拒否 (exit 1)
uv run python -m services.cli.v44_finishing run \
  --episode-root "$V44T" --executor fake 2>&1 \
  | grep -q "review-bundle-missing" && echo "refused: review-bundle-missing"
```

### 1.3 QC ポリシーの作成（一度だけ、実エピソード）

技術QCは解決済みポリシ（resolved QC policy）を必要とする。クリーンなレンダリングから
1回だけ作成する（提案ツール。エンジンは毎回再計測して照合する）。まず fake 実行で
final-preview を作らせてから、それを元にポリシーを作る:

```text
EP="../private/reference-episodes/v44-real-01"
"$UV_BIN" run python -m services.cli.v44_finishing run \
  --episode-root "$EP" --executor fake
"$UV_BIN" run python -m services.qc.run build-policy \
  --render "$EP/finishing/final-preview/preview.mp4" \
  --out "$EP/finishing/qc-policy.json"
```

### 1.4 仕上げ本体（live 実行、実エピソード）

```text
EP="../private/reference-episodes/v44-real-01"
"$UV_BIN" run python -m services.cli.v44_finishing run \
  --episode-root "$EP" \
  --executor live \
  --qc-policy "$EP/finishing/qc-policy.json"
```

Resolve でレンダリングした成果物に対してQCしたい場合は `--render <そのファイル>` を追加する。
終了コード: **0=ゲート通過 / 1=ブロック（ブロックドメイン名が stderr に出る） / 2=入力不正**。

### 1.5 オペレーターの視聴

`<ep>/finishing/final-preview/preview.mp4` を最終プレビューとして視聴する。
あわせて以下を確認するとよい:

- `<ep>/finishing/finishing-run.json` — 7品質ドメインの状態と根拠、QC評決、ピン/系譜
  （director pin の model_id、解析プロバイダ）、executor、所要時間
- `<ep>/finishing/quality-domain-report.json` — ドメイン毎の evidence / justification
- `<ep>/finishing/editorial-qc-report.json` — 編集QCの候補一覧
- `<ep>/finishing/mcp-run-report.json` — 実行ステップとフォールバック記録

### 1.6 公開可能性の記録（判断はオペレーターのみ）

視聴のうえ、評決を記録する（このコマンドは記録するだけで、公開は一切しない）:

```text
EP="../private/reference-episodes/v44-real-01"
"$UV_BIN" run python -m services.cli.v44_finishing record-publishability \
  --episode-root "$EP" \
  --verdict publishable \
  --comments "全体として良好" \
  --dimension-comments audio_finishing="BGMもう少し低くてもよい" \
  --dimension-comments subtitle="固有名詞の表記は正確"
```

`--verdict` は `publishable` / `publishable_after_fixes` / `not_publishable` のいずれか。
`--dimension-comments` のキーは 7品質ドメイン
（editorial_construction / subtitle / audio_finishing / color_finishing /
framing_motion / graphics_presentation / delivery_qc）に限る。
評決は既存の `PublishabilityReviewV1`（publishable: as_is / after_small_corrections /
not_yet）として `<ep>/review/publishability.json` に書かれる。
**仕上げレポートが無い（未完了の）状態では型付きエラーで拒否される。**

`publishable_after_fixes` の場合: 通常のNL修正ループ（Cockpit のレビューチャット +
部分リビルド、V44-1 と同じ経路）で修正し、仕上げを再実行して再記録する。
独立した最終編集システムは使わない。

### 1.7 作業時間の記録（bootstrap AHT、実エピソード）

各フェーズの実作業時間を分で追記していく（ラベルは bootstrap 固定。
定常状態の30分目標とは比較しないこと — PRD §19.4）:

```text
EP="../private/reference-episodes/v44-real-01"
"$UV_BIN" run python -m services.cli.v44_finishing record-time \
  --episode-root "$EP" \
  --phase ordinary_review --minutes 12
```

`--phase` は `ordinary_review` / `kit_bootstrap` / `taste_calibration` /
`troubleshooting` / `direct_resolve` のいずれか。
行は `<ep>/time-log.jsonl` に `{schema_version, phase, minutes, label: "bootstrap", at}`
として追記される。Gate V44-2 のサマリでは bootstrap AHT 内訳 + direct_resolve の
分数として集計される。

### 1.8 日本語字幕プルーフ（別コマンド、ゲート証拠の一部）

```text
EP="../private/reference-episodes/v44-real-01"
"$UV_BIN" run python -m services.cli.v44_subtitle_proof \
  --audio "$EP/sources/<主要カメラファイル>" \
  --sample "$EP/transcript-sample-corrected.json" \
  --proper-nouns "$EP/proper-nouns.json" \
  --out-dir "$EP/finishing/subtitle-proof"
```

## 2. 監視ポイント

- **stderr の `blocked: <code>` と `blocked domains: ...`**: ゲート却下時はブロック
  ドメイン名が必ず stderr に出る。レポートの `domain_justifications` に各ドメインの
  理由が入っている。
- **`technical_qc`**: `verdict: null` は「QCを実行していない」ことを意味する
  （理由は `reason` に書かれる）。捏造された評決は存在しない。
- **`execution_outcome: "failed"`**: MCP 実行ステップのいずれかが失敗し、フォール
  バックでも回復しなかったもの。`mcp-run-report.json` の該当ステップを確認する。
- **`surfaced_manual_items`**: manual_fallback_required のドメイン。最終承認前に
  Cockpit で内容を確認する必要がある。
- **系譜（lineage）**: レポートの `director_model_id`（本番モデル）と
  `analysis_provider`（ピン留め whisper）が期待どおりか。

## 3. ブロックされたとき（6つの失敗MCP機能とフォールバック）

`capabilities/v4.3/mcp-fit.json`（歴史的証拠・不変）で status=failed の機能は、
コンパイラが自動的にフォールバック段（fallback rung）へ経路変更する。**自前で
ネイティブ化しないこと**（計画の禁止事項）。失敗が今回のエピソードを実際にブロック
する場合のみ、文書化して最小限の修正を検討する。

| 機能 (capability) | 状態 | フォールバック | 実運用上の意味 |
|---|---|---|---|
| title-text-plus | failed | template_external | タイトル文字変更はテンプレート/外部経路 |
| transition-path | failed | template_external | トランジション追加は公開APIに不存在 |
| audio-property-operation | failed | legacy_direct | 音声プロパティ直接操作は不可 |
| bgm-track-ducking | failed | legacy_direct | Fairlight ダッキング制御なし |
| edit-engine-selects | failed | manual | セレクト候補抽出は手動 |
| alternate-shot-similarity | failed | manual | 類似ショット埋め込みは手動 |

上の表は不変の mcp-fit.json から機械的に検証できる（表が歴史的証拠からズレたら
この検証が exit 1 になる）:

```bash
uv run python - <<'PY'
from pathlib import Path

from services.toolchain.mcp_fit import load_mcp_fit

EXPECTED = {
    "title-text-plus": "template_external",
    "transition-path": "template_external",
    "audio-property-operation": "legacy_direct",
    "bgm-track-ducking": "legacy_direct",
    "edit-engine-selects": "manual",
    "alternate-shot-similarity": "manual",
}
rows = {
    row["capability"]: (row["status"], row.get("fallback", "legacy_direct"))
    for row in load_mcp_fit(Path("capabilities/v4.3/mcp-fit.json"))["capabilities"]
}
failed = {cap: fallback for cap, (status, fallback) in rows.items() if status == "failed"}
assert failed == EXPECTED, f"failed-capability drift: {failed}"
print(f"fallback matrix OK: {len(EXPECTED)} failed capabilities route to their fallback rungs")
PY
```

その他のブロック要因と対処:

- `mcp-server-unreachable`（--executor live）: Resolve とピン留め MCP サーバを起動して
  再実行。プローブは即時・有界で、ハングしない。
- `kit-selections-missing`: T11 の A/B プレビュー→選択を先に済ませる。
- `audio_finishing` / `color_finishing` が blocked（no explicit ... plan exists）:
  そのドメインの選択が「どちらも不要/現状維持」になっていないか確認。レシピを選んで
  再実行するか、意図なら理由を dimension comment に残す。
- `review-store-missing` / `review-bundle-missing`: エピソードが PREVIEW_READY に
  達していない。Cockpit からパイプラインを先に回す。
- `delivery_qc` blocked: 実行結果が未完了、または技術QCが未実施/評決 blocked。
  `--qc-policy` を渡して再実行する。

## 4. Gate V44-2 チェックリスト（機械+人間）

機械検証（T16 が実行）: FinishingRunReport・SubtitleProofReport・品質ドメイン報告・
QC報告・publishability 記録・time-log が `capabilities/v4.4/live-runs/v44-2/` 配下に
存在し検証されること。ゲートサマリは publishable 系の評決 + ブロックドメインゼロ +
QC passed が揃わなければ `passed` を発行しない（捏造防止は構造）。

人間確認（あなたの仕事）: 最終プレビューの視聴、公開可能性評決の記録、
各フェーズ時間の記録、必要なら after-fixes の修正ラウンド。

## 5. 関連資料

- 正本: `docs/prd/PRD_v4.4.md` §20（Gate V44-2）, §17（公開可能性レビュー）, §12（適用ドメイン）, §19.4（時間ログ）
- 実行計画: `docs/prd/implementation-plan-v4.4.md`
- ハーネス: `video-pipeline/services/cli/v44_finishing.py`（run / record-publishability / record-time）
- 品質ドメイン契約: `video-pipeline/services/creative_plan/quality_domains.py`
- 失敗機能の記録: `video-pipeline/capabilities/v4.3/mcp-fit.json`
