"""``python -m services.cli.v44_baseline`` — freeze v4.4 baseline artifact.

Subcommands:
  write --out <path>  collect cheap facts + injected quality tails → baseline.json
"""

from __future__ import annotations

import argparse
import json
import subprocess  # CLI layer may subprocess per plan §5
import sys
from datetime import UTC, datetime
from pathlib import Path

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.metrics.v44_baseline import BaselineRecordV1, PytestSummary

BASELINE_COMMIT_SHA = "c37247e9c10fa7c1c2ca50b84f0d1296b50c9b81"
V43_GATE_EVIDENCE: tuple[str, ...] = (
    "capabilities/v4.3/runs/gate-v43-1/gate-summary.json",
    "capabilities/v4.3/runs/probes/episode0-freeze-manifest.json",
)


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
    # Fallback: parent of video-pipeline package directory.
    return Path(__file__).resolve().parents[3]


def _current_commit_sha(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo_root,
        timeout=5,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _mcp_fit_sha256(repo_root: Path) -> str:
    # Prefer foundation helper for byte-stability; CLI may subprocess per spec.
    # Use sha256_file for determinism, verify via shasum in evidence transcript.
    pipeline_root = repo_root / "video-pipeline"
    target = pipeline_root / "capabilities" / "v4.3" / "mcp-fit.json"
    if not target.is_file():
        # Fallback when running from video-pipeline cwd.
        target = Path("capabilities/v4.3/mcp-fit.json").resolve()
    return sha256_file(target)


def _backends(repo_root: Path) -> dict[str, str]:
    pipeline_root = repo_root / "video-pipeline"
    candidate = pipeline_root / "config" / "backends.json"
    if not candidate.is_file():
        candidate = Path("config/backends.json").resolve()
    payload: object = json.loads(candidate.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"backends.json is not an object: {candidate}")
    result: dict[str, str] = {
        key: value for key, value in payload.items() if isinstance(value, str)
    }
    return result


def _parse_pytest_counts(text: str) -> tuple[int, int, int]:
    """Parse ``passed=N,failed=N,skipped=N`` (order-insensitive, no spaces)."""
    parts = [part.strip() for part in text.split(",") if part.strip()]
    mapping: dict[str, int] = {}
    for part in parts:
        if "=" not in part:
            raise ValueError(f"pytest counts must be k=v pairs: {text!r}")
        key, raw = part.split("=", 1)
        key = key.strip()
        raw = raw.strip()
        if key not in ("passed", "failed", "skipped"):
            raise ValueError(f"unknown pytest key {key!r} in {text!r}")
        mapping[key] = int(raw)
    for key in ("passed", "failed", "skipped"):
        if key not in mapping:
            raise ValueError(f"missing pytest key {key!r} in {text!r}")
    return mapping["passed"], mapping["failed"], mapping["skipped"]


def collect(
    *,
    repo_root: Path | None = None,
) -> dict[str, object]:
    """Gather cheap facts that do not require running the long pytest suite."""
    root = repo_root or _repo_root()
    current = _current_commit_sha(root)
    mcp_hash = _mcp_fit_sha256(root)
    backends = _backends(root)
    return {
        "baseline_commit_sha": BASELINE_COMMIT_SHA,
        "current_commit_sha": current,
        "mcp_fit_sha256": mcp_hash,
        "v43_gate_evidence": V43_GATE_EVIDENCE,
        "backends": backends,
        "created_at": datetime.now(tz=UTC).isoformat(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m services.cli.v44_baseline")
    sub = parser.add_subparsers(dest="command", required=True)
    write = sub.add_parser("write", help="write baseline.json")
    write.add_argument("--out", type=Path, required=True, help="output path")
    write.add_argument(
        "--pytest",
        type=str,
        required=True,
        help="pytest counts as passed=N,failed=N,skipped=N",
    )
    write.add_argument(
        "--pytest-tail-file",
        type=Path,
        required=True,
        help="path to file containing pytest tail text",
    )
    write.add_argument("--ruff", type=str, required=True, help="ruff result tail")
    write.add_argument("--basedpyright", type=str, required=True, help="basedpyright result tail")
    write.add_argument("--notes", type=str, default=None, help="optional one-line note")
    return parser


def _cmd_write(args: argparse.Namespace) -> int:
    try:
        passed, failed, skipped = _parse_pytest_counts(args.pytest)
    except ValueError as exc:
        print(f"invalid --pytest: {exc}", file=sys.stderr)
        return 2
    try:
        tail = Path(args.pytest_tail_file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read --pytest-tail-file: {exc}", file=sys.stderr)
        return 2

    facts = collect()
    # Auto-note when current drifted from audited baseline (MUST DO #1).
    notes: str | None = args.notes
    if facts["current_commit_sha"] != facts["baseline_commit_sha"] and notes is None:
        notes = (
            f"current_commit_sha {facts['current_commit_sha']} differs from "
            f"audited baseline {facts['baseline_commit_sha']}"
        )

    try:
        record = BaselineRecordV1(
            baseline_commit_sha=str(facts["baseline_commit_sha"]),
            current_commit_sha=str(facts["current_commit_sha"]),
            pytest_summary=PytestSummary(
                passed=passed, failed=failed, skipped=skipped, tail=tail
            ),
            ruff_result=args.ruff,
            basedpyright_result=args.basedpyright,
            mcp_fit_sha256=str(facts["mcp_fit_sha256"]),
            v43_gate_evidence=tuple(str(p) for p in V43_GATE_EVIDENCE),
            backends=dict(facts["backends"]),  # type: ignore[arg-type]
            created_at=str(facts["created_at"]),
            notes=notes,
        )
    except Exception as exc:  # noqa: BLE001  # CLI boundary: typed validation failure
        print(f"baseline validation failed: {exc}", file=sys.stderr)
        return 2

    out: Path = args.out
    atomic_write(out, canonical_model_bytes(record))
    print(f"baseline: {out} sha256={sha256_file(out)[:12]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "write":
        return _cmd_write(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
