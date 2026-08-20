"""F1 entrypoint: verify an immutable release candidate.

Exact lane contract: ``--candidate``, ``--expected-git-sha``,
``--schema manifest-v1``, ``--require-total-h1-binding``, ``--recompute``.
Prints the canonical verification JSON on success; exits 2 on any typed
release-prevention failure.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from services.foundation_io import canonical_model_bytes
from services.release.errors import ReleaseGateError
from services.release.manifest import MANIFEST_SCHEMA
from services.release.verify import verify_candidate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--expected-git-sha", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--require-total-h1-binding", action="store_true")
    parser.add_argument("--recompute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.schema != MANIFEST_SCHEMA:
        print(f"unsupported manifest schema: {arguments.schema}", file=sys.stderr)
        return 2
    try:
        verification = verify_candidate(
            arguments.candidate,
            expected_git_sha=arguments.expected_git_sha,
            require_h1_binding=arguments.require_total_h1_binding,
            recompute=arguments.recompute,
            require_readonly=False,
        )
    except (ReleaseGateError, OSError) as error:
        print(error, file=sys.stderr)
        return 2
    print(canonical_model_bytes(verification).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
