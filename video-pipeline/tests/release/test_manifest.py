"""manifest-v1: canonical bytes, path rules, collisions, exclusions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.release.manifest import (
    MANIFEST_NAME,
    Manifest,
    ManifestEntry,
    ManifestError,
    build_manifest,
    candidate_id,
    collect_entries,
    manifest_bytes,
    tree_hash,
)

NFC_PATH = "caf\u00e9.txt"
NFD_PATH = "cafe\u0301.txt"


def make_tree(tmp: Path) -> Path:
    root = tmp / "tree"
    (root / "b").mkdir(parents=True)
    (root / "a.txt").write_bytes(b"alpha")
    (root / "b" / "z.json").write_bytes(b'{"k":1}')
    return root


def _entry_json(path: str, size: int, digest: str) -> bytes:
    return json.dumps(
        {"path": path, "sha256": digest, "size": size}, sort_keys=True, separators=(",", ":")
    ).encode()


def test_10_canonical_bytes_are_compact_sorted_and_minimal(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    manifest = build_manifest(root)
    payload = manifest_bytes(manifest)
    assert payload == (
        b'{"entries":['
        + _entry_json("a.txt", 5, hashlib.sha256(b"alpha").hexdigest())
        + b","
        + _entry_json("b/z.json", 7, hashlib.sha256(b'{"k":1}').hexdigest())
        + b'],"schema_version":"manifest-v1"}'
    )
    assert b" " not in payload
    assert b"candidate_id" not in payload


def test_11_candidate_id_is_sha256_of_manifest_bytes(tmp_path: Path) -> None:
    manifest = build_manifest(make_tree(tmp_path))
    assert candidate_id(manifest) == hashlib.sha256(manifest_bytes(manifest)).hexdigest()


def test_12_manifest_json_excluded_from_entries(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    (root / MANIFEST_NAME).write_bytes(b"{}")
    paths = [entry.path for entry in collect_entries(root)]
    assert MANIFEST_NAME not in paths
    assert paths == ["a.txt", "b/z.json"]


def test_20_unsafe_paths_rejected_at_model_level() -> None:
    for bad in ("/abs.txt", "a/../b", "./a", "a//b", "a\\b", "bad\x00name", "..", "."):
        with pytest.raises(ValidationError):
            ManifestEntry(path=bad, size=1, sha256="0" * 64)


def test_21_uppercase_hash_and_negative_size_rejected() -> None:
    with pytest.raises(ValidationError):
        ManifestEntry(path="a", size=1, sha256="A" * 64)
    with pytest.raises(ValidationError):
        ManifestEntry(path="a", size=-1, sha256="0" * 64)


def test_22_nfd_path_rejected_nfc_accepted() -> None:
    with pytest.raises(ValidationError):
        ManifestEntry(path=NFD_PATH, size=1, sha256="0" * 64)
    assert ManifestEntry(path=NFC_PATH, size=1, sha256="0" * 64).path == NFC_PATH


def test_30_case_fold_collision_rejected_at_model_level() -> None:
    entry_a = ManifestEntry(path="A.txt", size=1, sha256="0" * 64)
    entry_b = ManifestEntry(path="a.txt", size=1, sha256="0" * 64)
    with pytest.raises(ValidationError):
        Manifest(entries=(entry_a, entry_b))


def test_31_nfc_vs_nfd_collision_rejected_at_model_level() -> None:
    entry_a = ManifestEntry(path="Stra\u00dfe", size=1, sha256="0" * 64)
    entry_b = ManifestEntry(path="STRASSE", size=1, sha256="0" * 64)
    with pytest.raises(ValidationError):
        Manifest(entries=(entry_a, entry_b))


def test_32_unsorted_and_duplicate_entries_rejected() -> None:
    first = ManifestEntry(path="a", size=1, sha256="0" * 64)
    second = ManifestEntry(path="b", size=1, sha256="0" * 64)
    with pytest.raises(ValidationError):
        Manifest(entries=(second, first))
    with pytest.raises(ValidationError):
        Manifest(entries=(first, first))


def test_33_extra_fields_rejected_including_candidate_id() -> None:
    with pytest.raises(ValidationError):
        Manifest.model_validate_json(b'{"schema_version":"manifest-v1","entries":[],"candidate_id":"0"}')
    with pytest.raises(ValidationError):
        Manifest.model_validate_json(b'{"schema_version":"manifest-v2","entries":[]}')


def test_40_symlink_rejected(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    (root / "link.txt").symlink_to("a.txt")
    with pytest.raises(ManifestError, match="symlink"):
        collect_entries(root)


def test_41_non_regular_file_rejected(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    os.mkfifo(root / "pipe")
    with pytest.raises(ManifestError, match="non-regular"):
        collect_entries(root)


def test_42_nfd_filesystem_name_rejected(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    (root / NFD_PATH).write_bytes(b"x")
    with pytest.raises(ManifestError, match="NFC"):
        collect_entries(root)


def test_50_tree_hash_is_deterministic_and_content_bound(tmp_path: Path) -> None:
    assert tree_hash(make_tree(tmp_path / "one")) == tree_hash(make_tree(tmp_path / "two"))
    root = make_tree(tmp_path / "three")
    (root / "a.txt").write_bytes(b"omega")
    assert tree_hash(root) != tree_hash(make_tree(tmp_path / "four"))
