"""Rejection path for a release candidate (Todo 67).

Revocation retains the candidate bytes untouched, records a typed
revocation with the Todo-67/F-lane reset semantics, and requires any
rebuild to target a new SHA-specific path (the old path stays occupied).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from services.contracts.primitives import Sha256, StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes
from services.release.errors import ReleaseGateCode, ReleaseGateError
from services.release.finalize_candidate import REVOCATION_SUFFIX
from services.release.manifest import candidate_id
from services.release.models import IDENTITY_NAME, ReleaseIdentity
from services.release.verify import read_manifest

RevocationReason = Literal[
    ReleaseGateCode,
    "global_review_reject",
    "operator_reject",
    "adversarial_verify_reject",
]

RESET_LANES: tuple[str, ...] = ("todo-67", "F1", "F2", "F3", "F4")
UNKNOWN_CANDIDATE_ID = "0" * 64


class RevocationRecord(StrictModel):
    schema_version: Literal["candidate-revocation-v1"] = "candidate-revocation-v1"
    candidate_path: str
    candidate_id: Sha256
    git_sha: str
    reason: RevocationReason
    detail: str
    retains_bytes: Literal[True] = True
    resets: tuple[str, ...] = RESET_LANES
    rebuild: Literal["new-sha-required"] = "new-sha-required"


def revoke_candidate(
    candidate: Path, reason: RevocationReason, detail: str, out: Path
) -> RevocationRecord:
    """Record a typed revocation; candidate bytes are never modified."""

    if not candidate.is_dir():
        raise ReleaseGateError("missing_release_input", f"candidate not found: {candidate}")
    marker = candidate.with_name(candidate.name + REVOCATION_SUFFIX)
    if marker.exists():
        raise ReleaseGateError("revoked_candidate", f"candidate already revoked: {candidate}")
    try:
        manifest, _ = read_manifest(candidate)
        revoked_id = candidate_id(manifest)
    except (ReleaseGateError, OSError):
        revoked_id = UNKNOWN_CANDIDATE_ID
    try:
        git_sha = ReleaseIdentity.model_validate_json(
            (candidate / IDENTITY_NAME).read_bytes()
        ).git_sha
    except (OSError, ValidationError):
        git_sha = ""
    record = RevocationRecord(
        candidate_path=str(candidate),
        candidate_id=revoked_id,
        git_sha=git_sha,
        reason=reason,
        detail=detail,
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(canonical_model_bytes(record).decode())
    marker.chmod(0o444)
    atomic_write(out, canonical_model_bytes(record))
    return record


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--detail", required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        record = revoke_candidate(
            arguments.candidate, arguments.reason, arguments.detail, arguments.out
        )
    except (ReleaseGateError, OSError) as error:
        print(error, file=sys.stderr)
        return 2
    print(canonical_model_bytes(record).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
