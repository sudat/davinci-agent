"""``python -m services.cli.v44_real01`` — v44-real-01 episode protocol scaffolding.

Thin argparse CLI. Templates are embedded in this module (``private/`` is
git-ignored, so nothing inside it can be committed).

Subcommands:
  init    --episode <dir>            create scaffold
  prepare --episode <dir> --source-folder <path>  copy footage + manifest
  status  --episode <dir>            presence checks

Manifest logic reuses ``services.foundation_io`` utilities (``sha256_file``,
``atomic_write``, ``canonical_model_bytes``) — the same primitives that
``services/ingest/ingest.py:register_one`` uses for hashing and sealing.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file

# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class V44Real01Error(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Strict models (protocol metadata — NOT authoritative artifacts)
# ---------------------------------------------------------------------------


class ExpectedContentV1(StrictModel):
    has_speech: bool | None = None
    has_broll: bool | None = None
    has_quiet_moments: bool | None = None
    has_proper_nouns: bool | None = None
    has_alternate_takes: bool | None = None
    correction_reason: str | None = None


class V44EpisodeProtocolV1(StrictModel):
    schema_version: Literal["v44-episode-protocol-v1"] = "v44-episode-protocol-v1"
    episode_id: Identifier = "v44-real-01"  # type: ignore[assignment]
    title_intent: str | None = None
    brief_draft: str | None = Field(default=None, description="brief draft placeholder")
    expected_content: ExpectedContentV1 = Field(default_factory=ExpectedContentV1)
    created_at: str
    notes: str | None = None


class SourcesManifestEntry(StrictModel):
    relative_path: str = Field(min_length=1)
    sha256: Sha256
    size_bytes: int = Field(ge=0)


class SourcesManifestV1(StrictModel):
    schema_version: Literal["v44-sources-manifest-v1"] = "v44-sources-manifest-v1"
    episode_id: Identifier
    created_at: str
    sources: tuple[SourcesManifestEntry, ...]


# ---------------------------------------------------------------------------
# Embedded templates — materialized at init
# ---------------------------------------------------------------------------

TEMPLATE_EPISODE_JSON: dict[str, object] = {
    "schema_version": "v44-episode-protocol-v1",
    "episode_id": "v44-real-01",
    "title_intent": None,
    "brief_draft": None,
    "expected_content": {
        "has_speech": None,
        "has_broll": None,
        "has_quiet_moments": None,
        "has_proper_nouns": None,
        "has_alternate_takes": None,
        "correction_reason": None,
    },
    "created_at": "PLACEHOLDER",
    "notes": None,
}

TEMPLATE_GROUND_TRUTH_PLACEHOLDER: dict[str, object] = {
    "_placeholder": True,
    "schema_version": "editorial-ground-truth-v1",
    "instructions": (
        "このファイルはプレースホルダーです。実験前に Task 5 の CLI で正本を生成してください: "
        "uv run python -m services.cli.v44_product_proof init-ground-truth "
        "--episode-id v44-real-01 "
        "--out private/reference-episodes/v44-real-01/ground-truth.json "
        "その後、アンカー(must_keep / must_remove 等)を"
        "実験出力を一切見ずに記入してください。"
    ),
}

TEMPLATE_TRANSCRIPT_SAMPLE_PLACEHOLDER: dict[str, object] = {
    "_placeholder": True,
    "instructions": (
        "2-3分の音声サンプルを手動で正確に書き起こしてください。"
        "難読固有名詞を含む区間を選んでください。"
    ),
    "sample": None,
    "entries": [],
}

# NOTE: fullwidth parens replaced with halfwidth to satisfy RUF001;
# operator README on disk still reads naturally in Japanese.
README_JA = """# v44-real-01 エピソードプロトコル

このディレクトリは代表エピソード `v44-real-01` 用の素材置き場です。
PRD v4.4 5章に基づきます。

## フォルダ構成

```
v44-real-01/
  episode.json                     # エピソードのメタ情報(タイトル意図/概要/チェックリスト)
  sources/                         # カメラ素材を配置する場所
  sources-manifest.json            # prepare 実行時に自動生成される素材一覧(相対パス + sha256)
  ground-truth.json                # アンカー正解(プレースホルダーから開始)
  transcript-sample-corrected.json # 2-3分の手動書き起こしサンプル(プレースホルダーから開始)
  runs/                            # 実験実行結果の出力先
  README.md                        # このファイル
```

## 1. 素材の配置

- カメラで撮影した素材ファイル(.mp4 / .mov 等)を `sources/` に配置します。
- 手動でコピーしても構いませんが、推奨は次のコマンドです:

```bash
uv run python -m services.cli.v44_real01 prepare \\
  --episode private/reference-episodes/v44-real-01 \\
  --source-folder /path/to/camera/footage
```

`prepare` はファイルを `sources/` にコピーし、
`sources-manifest.json` に相対パスと sha256 を記録します。
`--source-folder` が存在しない場合は型付きエラー
`source-folder-not-found` で終了します。

## 2. Ground Truth アンカーの記録(重要)

- 実験出力(Experiment A/B の結果)を見る **前に** アンカーを記録してください。
- 正本スキーマは Task 5 が所有します。初期生成は次のコマンドで行います:

```bash
uv run python -m services.cli.v44_product_proof init-ground-truth \\
  --episode-id v44-real-01 \\
  --out private/reference-episodes/v44-real-01/ground-truth.json
```

- その後、以下のラベルでアンカーを記入します(PRD v4.4 6.2章):
  - `must_keep` — 失うとエピソードが大きく損なわれる瞬間
  - `good_optional` — あると良いが必須ではない
  - `must_remove` — 明らかに不要/冗長/破損/退屈な区間
  - `uncertain` — 採点対象外
- 各アンカーには任意で短い理由(日本語)を添えてください。

## 3. transcript-sample-corrected.json の記入

- 2-3分間の音声区間を選び、手動で正確に書き起こしてください。
- エピソードに登場する難読固有名詞(人名/地名/製品名等)を
  必ず含む区間を選んでください。
- タイムスタンプ付きで記入し、ASR 出力との比較
  (CER / 固有名詞 recall)に使用します。

## 4. 状態確認

```bash
uv run python -m services.cli.v44_real01 status \\
  --episode private/reference-episodes/v44-real-01
```

- 素材の有無、ground truth がプレースホルダーかどうか、
  書き起こしサンプルが記入済みかを一覧表示します。
- 作成直後は全項目が未入力(all-missing)でも正常な状態です。

## 5. 注意事項

- `private/` 配下は git 管理外です。素材や個人情報を誤ってコミットしないでください。
- `episode.json` の `expected_content` チェックリスト
  (speech / B-roll / quiet moments / proper nouns /
  alternate takes / correction_reason)は、
  素材の特性に合わせて true/false を記入してください。
"""

EPISODE_JSON_NAME = "episode.json"
SOURCES_DIR_NAME = "sources"
GROUND_TRUTH_NAME = "ground-truth.json"
TRANSCRIPT_SAMPLE_NAME = "transcript-sample-corrected.json"
RUNS_DIR_NAME = "runs"
README_NAME = "README.md"
SOURCES_MANIFEST_NAME = "sources-manifest.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if completed.returncode == 0:
            return Path(completed.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return Path(__file__).resolve().parents[3]


def _resolve_episode_dir(raw: str | None) -> Path:
    if raw is not None:
        p = Path(raw)
        if not p.is_absolute():
            p = _repo_root() / p
        return p
    return _repo_root() / "private" / "reference-episodes" / "v44-real-01"


def _is_placeholder_file(path: Path) -> bool:
    if not path.is_file():
        return True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return True
    if not isinstance(data, dict):
        return True
    return bool(data.get("_placeholder") is True)


def _has_footage(episode_dir: Path) -> tuple[bool, int]:
    sources_dir = episode_dir / SOURCES_DIR_NAME
    manifest = episode_dir / SOURCES_MANIFEST_NAME
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            sources = payload.get("sources", [])
            if isinstance(sources, list) and len(sources) > 0:
                return True, len(sources)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    if not sources_dir.is_dir():
        return False, 0
    count = sum(1 for p in sources_dir.rglob("*") if p.is_file())
    return (count > 0), count


def _collect_source_files(source_folder: Path) -> list[Path]:
    files = [p for p in source_folder.rglob("*") if p.is_file()]
    if not files:
        files = [child for child in source_folder.iterdir() if child.is_file()]
    return sorted(files)


def _copy_and_hash_entries(
    collected: list[Path],
    source_folder: Path,
    episode_dir: Path,
    sources_dir: Path,
) -> list[SourcesManifestEntry]:
    entries: list[SourcesManifestEntry] = []
    for src in collected:
        try:
            rel_inside = src.relative_to(source_folder)
        except ValueError:
            rel_inside = Path(src.name)
        dest = sources_dir / rel_inside
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        sha = sha256_file(dest)
        size = dest.stat().st_size
        try:
            rel_episode = dest.relative_to(episode_dir)
        except ValueError:
            rel_episode = Path(SOURCES_DIR_NAME) / rel_inside
        entries.append(
            SourcesManifestEntry(
                relative_path=str(rel_episode),
                sha256=sha,
                size_bytes=size,
            )
        )
    return entries


def _read_episode_id(episode_dir: Path) -> str:
    ep_path = episode_dir / EPISODE_JSON_NAME
    if ep_path.is_file():
        try:
            data = json.loads(ep_path.read_text(encoding="utf-8"))
            if isinstance(data.get("episode_id"), str):
                return str(data["episode_id"])
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return "v44-real-01"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cmd_init(episode_dir: Path) -> int:
    episode_dir.mkdir(parents=True, exist_ok=True)
    sources_dir = episode_dir / SOURCES_DIR_NAME
    runs_dir = episode_dir / RUNS_DIR_NAME
    sources_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)

    created_at = datetime.now(tz=UTC).isoformat()
    episode_payload = dict(TEMPLATE_EPISODE_JSON)
    episode_payload["created_at"] = created_at
    model = V44EpisodeProtocolV1.model_validate(episode_payload)
    episode_path = episode_dir / EPISODE_JSON_NAME
    if not episode_path.exists():
        atomic_write(episode_path, canonical_model_bytes(model))

    gt_path = episode_dir / GROUND_TRUTH_NAME
    if not gt_path.exists():
        gt_path.write_text(
            json.dumps(TEMPLATE_GROUND_TRUTH_PLACEHOLDER, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    ts_path = episode_dir / TRANSCRIPT_SAMPLE_NAME
    if not ts_path.exists():
        ts_path.write_text(
            json.dumps(TEMPLATE_TRANSCRIPT_SAMPLE_PLACEHOLDER, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    readme_path = episode_dir / README_NAME
    if not readme_path.exists():
        readme_path.write_text(README_JA, encoding="utf-8")

    print(f"init: scaffold ready at {episode_dir}")
    return 0


def _cmd_prepare(episode_dir: Path, source_folder: Path) -> int:
    if not source_folder.exists() or not source_folder.is_dir():
        print(
            f"source-folder-not-found: {source_folder} does not exist or is not a directory",
            file=sys.stderr,
        )
        return 2
    if not episode_dir.is_dir():
        print(
            f"episode-not-initialized: {episode_dir} does not exist; run init first",
            file=sys.stderr,
        )
        return 2

    sources_dir = episode_dir / SOURCES_DIR_NAME
    sources_dir.mkdir(parents=True, exist_ok=True)

    collected = _collect_source_files(source_folder)
    entries = _copy_and_hash_entries(collected, source_folder, episode_dir, sources_dir)

    manifest = SourcesManifestV1(
        episode_id=_read_episode_id(episode_dir),  # type: ignore[arg-type]
        created_at=datetime.now(tz=UTC).isoformat(),
        sources=tuple(entries),
    )
    out_path = episode_dir / SOURCES_MANIFEST_NAME
    atomic_write(out_path, canonical_model_bytes(manifest))
    print(f"prepare: {len(entries)} file(s) -> {out_path}")
    for e in entries:
        print(f"  {e.relative_path} sha256={e.sha256[:12]} size={e.size_bytes}")
    return 0


def _cmd_status(episode_dir: Path) -> int:
    footage_present, footage_count = _has_footage(episode_dir)
    gt_path = episode_dir / GROUND_TRUTH_NAME
    ts_path = episode_dir / TRANSCRIPT_SAMPLE_NAME
    gt_placeholder = _is_placeholder_file(gt_path)
    ts_placeholder = _is_placeholder_file(ts_path)

    footage_label = "present" if footage_present else "missing"
    gt_label = "missing (placeholder)" if gt_placeholder else "filled"
    ts_label = "missing (placeholder)" if ts_placeholder else "filled"

    ep_exists = (episode_dir / EPISODE_JSON_NAME).is_file()
    manifest_exists = (episode_dir / SOURCES_MANIFEST_NAME).is_file()

    lines = [
        f"episode: {episode_dir}",
        f"  episode.json: {'present' if ep_exists else 'missing'}",
        f"  sources/: {footage_label} ({footage_count} file(s))",
        f"  sources-manifest.json: {'present' if manifest_exists else 'missing'}",
        f"  ground-truth.json: {gt_label}",
        f"  transcript-sample-corrected.json: {ts_label}",
    ]
    if not footage_present and gt_placeholder and ts_placeholder:
        lines.append("  status: all-missing (fresh scaffold)")
    elif footage_present and not gt_placeholder and not ts_placeholder:
        lines.append("  status: ready")
    else:
        lines.append("  status: partial")

    print("\n".join(lines))
    return 0


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m services.cli.v44_real01")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create episode scaffold")
    p_init.add_argument(
        "--episode",
        type=str,
        default=None,
        help="episode dir (default: private/reference-episodes/v44-real-01)",
    )

    p_prepare = sub.add_parser("prepare", help="copy footage + produce manifest")
    p_prepare.add_argument("--episode", type=str, default=None, help="episode dir")
    p_prepare.add_argument(
        "--source-folder",
        type=str,
        required=True,
        help="folder containing footage to register",
    )

    p_status = sub.add_parser("status", help="presence checks")
    p_status.add_argument(
        "--episode",
        type=str,
        default=None,
        help="episode dir (default: private/reference-episodes/v44-real-01)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    episode_dir = _resolve_episode_dir(getattr(args, "episode", None))

    if args.command == "init":
        return _cmd_init(episode_dir)
    if args.command == "prepare":
        source_folder = Path(str(args.source_folder))
        if not source_folder.is_absolute():
            source_folder = (_repo_root() / source_folder).resolve()
        else:
            source_folder = source_folder.resolve()
        return _cmd_prepare(episode_dir, source_folder)
    if args.command == "status":
        return _cmd_status(episode_dir)

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
