"""Extraction: full/source-only copies, no-follow refusal, tamper refusal."""

from __future__ import annotations

from pathlib import Path

from services.release.extract_candidate import main as extract_main
from tests.release.support import build_test_candidate, make_writable


def argv(candidate: Path, out: Path, *extra: str) -> list[str]:
    return ["--candidate", str(candidate), "--no-follow", "--out", str(out), *extra]


def test_10_full_extract_contains_inputs_and_source(tmp_path: Path) -> None:
    _repo, candidate, _sha = build_test_candidate(tmp_path)
    out = tmp_path / "extract"
    assert extract_main(argv(candidate, out)) == 0
    assert (out / "source" / "pyproject.toml").is_file()
    assert (out / "inputs" / "h1" / "binding.json").is_file()
    assert (out / "identity.json").is_file()
    assert (out / "extract-receipt.json").is_file()


def test_11_source_only_extract_is_project_root(tmp_path: Path) -> None:
    _repo, candidate, _sha = build_test_candidate(tmp_path)
    out = tmp_path / "source-extract"
    assert extract_main(argv(candidate, out, "--source-only")) == 0
    assert (out / "pyproject.toml").is_file()
    assert not (out / "inputs").exists()
    assert not (out / "source").exists()


def test_20_missing_no_follow_refused(tmp_path: Path) -> None:
    _repo, candidate, _sha = build_test_candidate(tmp_path)
    rc = extract_main(["--candidate", str(candidate), "--out", str(tmp_path / "e")])
    assert rc == 2


def test_21_tampered_candidate_refused(tmp_path: Path) -> None:
    _repo, candidate, _sha = build_test_candidate(tmp_path)
    target = candidate / "source" / "services" / "core.py"
    make_writable(target)
    target.write_text("VALUE = 42\n")
    target.chmod(0o444)
    assert extract_main(argv(candidate, tmp_path / "e")) == 2
