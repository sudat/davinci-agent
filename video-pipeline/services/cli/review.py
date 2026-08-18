"""``python -m services.cli.review`` — propose / apply review corrections.

``propose`` wraps the Todo-29 replay translator with the Todo-12 production
policy gate; ``apply`` commits one immutable event against the displayed base
hash, recompiles the IR, regenerates the preview, and logs event-derived
correction metrics. Ambiguous/schema-gap/unsupported outcomes never mutate the
plan and return typed results.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from services.cli.bundle import BundleDriftError, load_bundle
from services.cli.project import plan_sha256
from services.cli.review_apply import ApplyError, apply_proposal
from services.cli.review_replay import (
    PlanView,
    ReviewTranslateError,
    propose_review_command,
)
from services.config.models import ResolvedConfig
from services.fixtures.manifest_phase1 import Phase1TechnicalFixtureManifest
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.review_command.store import ReviewCommitError, load_head


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.cli.review",
        description="Propose and apply structured review corrections.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    propose = sub.add_parser("propose", help="translate one instruction via the replay translator")
    propose.add_argument("--bundle", type=Path, required=True)
    propose.add_argument("--instruction-file", type=Path, required=True)
    propose.add_argument("--translator-policy", type=Path, required=True)
    propose.add_argument("--production-policy", type=Path, required=True)
    propose.add_argument("--out", type=Path, required=True)
    apply_cmd = sub.add_parser("apply", help="apply one validated proposal to the bundle")
    apply_cmd.add_argument("--proposal", type=Path, required=True)
    apply_cmd.add_argument("--bundle", type=Path, required=True)
    apply_cmd.add_argument("--out", type=Path, required=True)
    return parser


def _propose(arguments: argparse.Namespace) -> int:
    try:
        bundle = load_bundle(arguments.bundle)
        manifest = Phase1TechnicalFixtureManifest.model_validate_json(
            (arguments.bundle.parent / bundle.fixture_manifest_path).read_bytes()
        )
        policy = ResolvedConfig.model_validate_json(arguments.production_policy.read_bytes())
        head = load_head(
            arguments.bundle.parent / bundle.events_log,
            arguments.bundle.parent / bundle.store_dir,
        )
    except (OSError, ValidationError, BundleDriftError, ReviewCommitError) as error:
        print(f"inputs_unreadable: {error}", file=sys.stderr)
        return 1
    if sha256_file(arguments.bundle.parent / bundle.fixture_manifest_path) != (
        bundle.fixture_manifest_sha256
    ):
        print(
            "manifest_drift: the bundle's fixture manifest no longer hashes to its seal",
            file=sys.stderr,
        )
        return 1
    current = PlanView(
        plan=head.plan,
        version=f"v{head.version}",
        plan_hash=plan_sha256(head.plan),
    )
    try:
        instruction = arguments.instruction_file.read_text(encoding="utf-8")
        outcome = propose_review_command(
            manifest,
            instruction,
            current,
            policy,
            policy_sha=sha256_file(arguments.production_policy),
            translator_sha=sha256_file(arguments.translator_policy),
        )
    except (OSError, ReviewTranslateError) as error:
        print(f"propose_failed: {error}", file=sys.stderr)
        return 1
    atomic_write(arguments.out, canonical_model_bytes(outcome))
    if outcome.status != "proposal":
        print(f"{outcome.error_code}: {outcome.error_detail}", file=sys.stderr)
        return 1
    if outcome.proposal_sha256 is None or outcome.base_plan_hash is None:
        print("proposal_invalid: translated proposal lacks hash bindings", file=sys.stderr)
        return 1
    print(f"proposal: {outcome.proposal_sha256[:12]}")
    print(f"classification: {outcome.classification}")
    print(f"base: {outcome.base_plan_hash[:12]}")
    return 0


def _apply(arguments: argparse.Namespace) -> int:
    try:
        result, code = apply_proposal(arguments.proposal, arguments.bundle, arguments.out)
    except (ApplyError, OSError) as error:
        print(f"apply_failed: {error}", file=sys.stderr)
        return 1
    if result.applied:
        print(f"applied: version v{result.version} event {str(result.event_id)[:12]}")
    else:
        print(f"not applied: {result.reason_code}")
    return code


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "propose":
        return _propose(arguments)
    return _apply(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
