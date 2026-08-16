from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.source_snapshot import SnapshotError, create_snapshot


def test_snapshot_includes_sources_and_excludes_runtime_state(tmp_path: Path) -> None:
    root = tmp_path / "pipeline"
    (root / "services").mkdir(parents=True)
    (root / ".omo").mkdir()
    (root / "services/app.py").write_text("VALUE = 1\n")
    (root / ".omo/secret.json").write_text("{}")
    contract = tmp_path / "execution-contract.json"
    contract.write_text("{}")
    out = tmp_path / "snapshot.json"

    create_snapshot(root, contract, out)

    paths = [entry["path"] for entry in json.loads(out.read_text())["entries"]]
    assert paths == ["services/app.py"]


def test_snapshot_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "pipeline"
    (root / "services").mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("secret\n")
    (root / "services/escape.py").symlink_to(outside)
    contract = tmp_path / "execution-contract.json"
    contract.write_text("{}")

    with pytest.raises(SnapshotError):
        create_snapshot(root, contract, tmp_path / "snapshot.json")


def test_snapshot_tree_hash_changes_with_content(tmp_path: Path) -> None:
    root = tmp_path / "pipeline"
    (root / "services").mkdir(parents=True)
    source = root / "services/app.py"
    source.write_text("VALUE = 1\n")
    contract = tmp_path / "execution-contract.json"
    contract.write_text("{}")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    create_snapshot(root, contract, first)
    source.write_text("VALUE = 2\n")
    create_snapshot(root, contract, second)

    assert json.loads(first.read_text())["tree_sha256"] != json.loads(second.read_text())[
        "tree_sha256"
    ]
