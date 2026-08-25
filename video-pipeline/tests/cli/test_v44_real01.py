"""Task 6: v44-real-01 protocol scaffolding (Tier A)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(p) for p in argv],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(cwd) if cwd is not None else None,
        timeout=30,
    )


def _ffmpeg() -> Path | None:
    found = shutil.which("ffmpeg")
    return Path(found) if found is not None else None


def test_init_creates_scaffold(tmp_path: Path) -> None:
    episode = tmp_path / "v44-real-01"
    args = [sys.executable, "-m", "services.cli.v44_real01", "init", "--episode", str(episode)]
    result = _run(args)
    assert result.returncode == 0, result.stderr
    assert (episode / "episode.json").is_file()
    assert (episode / "sources").is_dir()
    assert (episode / "ground-truth.json").is_file()
    assert (episode / "transcript-sample-corrected.json").is_file()
    assert (episode / "runs").is_dir()
    assert (episode / "README.md").is_file()

    data = json.loads((episode / "episode.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == "v44-episode-protocol-v1"
    assert data["episode_id"] == "v44-real-01"
    assert "expected_content" in data
    ec = data["expected_content"]
    keys = (
        "has_speech",
        "has_broll",
        "has_quiet_moments",
        "has_proper_nouns",
        "has_alternate_takes",
    )
    for key in keys:
        assert key in ec

    readme = (episode / "README.md").read_text(encoding="utf-8")
    assert "v44-real-01" in readme
    assert "sources" in readme
    assert "v44_product_proof" in readme

    gt = json.loads((episode / "ground-truth.json").read_text(encoding="utf-8"))
    assert gt.get("_placeholder") is True
    ts = json.loads((episode / "transcript-sample-corrected.json").read_text(encoding="utf-8"))
    assert ts.get("_placeholder") is True


def test_status_all_missing_on_fresh_scaffold(tmp_path: Path) -> None:
    episode = tmp_path / "fresh"
    _run([sys.executable, "-m", "services.cli.v44_real01", "init", "--episode", str(episode)])
    args = [sys.executable, "-m", "services.cli.v44_real01", "status", "--episode", str(episode)]
    result = _run(args)
    assert result.returncode == 0, result.stderr
    assert "all-missing" in result.stdout
    assert "sources/: missing" in result.stdout
    assert "ground-truth.json: missing (placeholder)" in result.stdout
    assert "transcript-sample-corrected.json: missing (placeholder)" in result.stdout


def test_prepare_with_dummy_clip_produces_manifest_and_status_flips(tmp_path: Path) -> None:
    ffmpeg = _ffmpeg()
    if ffmpeg is None:
        pytest.skip("ffmpeg not available")

    episode = tmp_path / "ep-prepare"
    args = [sys.executable, "-m", "services.cli.v44_real01", "init", "--episode", str(episode)]
    result = _run(args)
    assert result.returncode == 0, result.stderr

    src_folder = tmp_path / "src-footage"
    src_folder.mkdir()
    clip = src_folder / "clip.mp4"
    proc = subprocess.run(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=24:duration=2",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(clip),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert clip.is_file()

    try:
        args = [
            sys.executable,
            "-m",
            "services.cli.v44_real01",
            "prepare",
            "--episode",
            str(episode),
            "--source-folder",
            str(src_folder),
        ]
        result = _run(args)
        assert result.returncode == 0, result.stderr
        assert "prepare:" in result.stdout

        assert (episode / "sources" / "clip.mp4").is_file()
        manifest_path = episode / "sources-manifest.json"
        assert manifest_path.is_file()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["schema_version"] == "v44-sources-manifest-v1"
        assert len(manifest["sources"]) == 1
        entry = manifest["sources"][0]
        assert entry["relative_path"] == "sources/clip.mp4"
        assert len(entry["sha256"]) == 64
        assert entry["size_bytes"] > 0

        status_args = [
            sys.executable,
            "-m",
            "services.cli.v44_real01",
            "status",
            "--episode",
            str(episode),
        ]
        result = _run(status_args)
        assert result.returncode == 0
        assert "sources/: present" in result.stdout
        assert "all-missing" not in result.stdout
    finally:
        clip.unlink(missing_ok=True)
        copied = episode / "sources" / "clip.mp4"
        assert not clip.exists(), "dummy clip should be deleted after prepare"
        copied.unlink(missing_ok=True)


def test_prepare_with_nonexistent_folder_typed_error(tmp_path: Path) -> None:
    episode = tmp_path / "ep-fail"
    args = [sys.executable, "-m", "services.cli.v44_real01", "init", "--episode", str(episode)]
    result = _run(args)
    assert result.returncode == 0, result.stderr

    bogus = tmp_path / "does-not-exist-zzz"
    args = [
        sys.executable,
        "-m",
        "services.cli.v44_real01",
        "prepare",
        "--episode",
        str(episode),
        "--source-folder",
        str(bogus),
    ]
    result = _run(args)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "source-folder-not-found" in combined


def test_prepare_samefile_inplace_mints_manifest(tmp_path: Path) -> None:
    ffmpeg = _ffmpeg()
    if ffmpeg is None:
        pytest.skip("ffmpeg not available")

    episode = tmp_path / "ep-samefile"
    args = [sys.executable, "-m", "services.cli.v44_real01", "init", "--episode", str(episode)]
    result = _run(args)
    assert result.returncode == 0, result.stderr

    # Simulate operator dropping footage directly into sources/
    clip = episode / "sources" / "inplace.mp4"
    proc = subprocess.run(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=24:duration=1",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(clip),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert clip.is_file()
    mtime_before = clip.stat().st_mtime

    # Prepare with --source-folder pointing at the episode's own sources dir
    result = _run(
        [
            sys.executable,
            "-m",
            "services.cli.v44_real01",
            "prepare",
            "--episode",
            str(episode),
            "--source-folder",
            str(episode / "sources"),
        ]
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "SameFileError" not in (result.stdout + result.stderr)
    assert clip.is_file()
    assert clip.stat().st_mtime == mtime_before

    manifest = json.loads((episode / "sources-manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["sources"]) == 1
    assert manifest["sources"][0]["relative_path"] == "sources/inplace.mp4"


def test_prepare_mixed_inplace_and_external(tmp_path: Path) -> None:
    ffmpeg = _ffmpeg()
    if ffmpeg is None:
        pytest.skip("ffmpeg not available")

    episode = tmp_path / "ep-mixed"
    args = [sys.executable, "-m", "services.cli.v44_real01", "init", "--episode", str(episode)]
    result = _run(args)
    assert result.returncode == 0, result.stderr

    # In-place file already in sources/
    inplace = episode / "sources" / "inplace.mp4"
    proc = subprocess.run(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=24:duration=1",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(inplace),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr

    # External file in a separate source folder
    external_src = tmp_path / "external"
    external_src.mkdir()
    external_clip = external_src / "external.mp4"
    proc = subprocess.run(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=24:duration=1",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(external_clip),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr

    # Build a combined source folder containing one samefile entry and one external copy.
    # Use a hard link for the samefile case so src.resolve() == dest.resolve() via inode.
    combined = tmp_path / "combined"
    combined.mkdir()
    # hard link to the in-place file (same inode) — will be detected as samefile
    hardlink = combined / "inplace.mp4"
    hardlink.hardlink_to(inplace)
    shutil.copy2(external_clip, combined / "external.mp4")

    result = _run(
        [
            sys.executable,
            "-m",
            "services.cli.v44_real01",
            "prepare",
            "--episode",
            str(episode),
            "--source-folder",
            str(combined),
        ]
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "SameFileError" not in (result.stdout + result.stderr)

    # Both files should now be present in sources and listed in manifest
    assert (episode / "sources" / "inplace.mp4").is_file()
    assert (episode / "sources" / "external.mp4").is_file()
    manifest = json.loads((episode / "sources-manifest.json").read_text(encoding="utf-8"))
    paths = {entry["relative_path"] for entry in manifest["sources"]}
    assert "sources/inplace.mp4" in paths
    assert "sources/external.mp4" in paths
    assert len(manifest["sources"]) == 2
