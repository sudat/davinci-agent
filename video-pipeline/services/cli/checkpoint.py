"""``python -m services.cli.checkpoint`` — the H1 operator checkpoint surface.

``show`` rehashes the bundle and prints/writes the TTY display receipt;
``record`` gates the operator decision through the Todo-13 ingress (real
records require a controlling TTY; the explicit ``--fixture`` seam produces
fixture-MARKED records for QA only); ``export`` rehashes every target again,
refuses stale displays and fixture-marked records presented as real, and
assembles the strict ``operator_checkpoint`` artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter, ValidationError

from services.approvals.global_review import (
    DEFAULT_REVOCATIONS_PATH,
    GlobalReviewError,
    load_report_set,
    load_revocations,
)
from services.approvals.global_review_models import GitFullSha
from services.approvals.ingress import (
    IngressRefusalError,
    record_fixture_operation,
    record_operation,
)
from services.approvals.models import ChainedOperationRecord
from services.approvals.store import OperationRecordStore
from services.approvals.verify import (
    REFUSAL_FIXTURE,
    evaluate_authorization,
    validate_supersession_chain,
)
from services.cli.bundle import BundleDriftError, load_bundle, rehash_bundle_targets
from services.cli.checkpoint_models import DisplayReceipt, DisplayTargets
from services.cli.review_common import (
    CheckpointError,
    assemble_checkpoint,
    recompute_targets,
)
from services.foundation_io import atomic_write, canonical_model_bytes

_GIT_SHA_ADAPTER: Final[TypeAdapter[GitFullSha]] = TypeAdapter(GitFullSha)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.cli.checkpoint",
        description="Show, record, and export the H1 operator checkpoint.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show", help="display the target hashes and write the receipt")
    show.add_argument("--bundle", type=Path, required=True)
    show.add_argument("--receipt", type=Path, required=True)
    record = sub.add_parser("record", help="record the operator decision (TTY-gated)")
    record.add_argument("--bundle", type=Path)
    record.add_argument("--display-receipt", type=Path)
    record.add_argument(
        "--purpose",
        choices=["EDITORIAL_APPROVED", "FINAL_APPROVED"],
        required=True,
    )
    record.add_argument("--decision", choices=["approve", "reject"], required=True)
    record.add_argument("--out", type=Path, required=True)
    record.add_argument("--actor", default="local-operator")
    record.add_argument("--fixture", action="store_true", help="QA seam: fixture-marked record")
    record.add_argument("--work-id", help="FINAL_APPROVED: execution work id")
    record.add_argument("--git-sha", help="FINAL_APPROVED: reviewed full commit SHA")
    record.add_argument("--candidate-id", help="FINAL_APPROVED: release candidate id")
    record.add_argument(
        "--report-dir", type=Path, help="FINAL_APPROVED: Global Review Report v1 directory"
    )
    record.add_argument(
        "--revocations",
        type=Path,
        help="FINAL_APPROVED: local candidate revocation list (default config path when present)",
    )
    export = sub.add_parser("export", help="assemble the operator_checkpoint artifact")
    export.add_argument("--bundle", type=Path, required=True)
    export.add_argument("--display-receipt", type=Path, required=True)
    export.add_argument("--operation-record", type=Path, required=True)
    export.add_argument("--out", type=Path, required=True)
    verify = sub.add_parser(
        "verify", help="rehash and validate an exported operator checkpoint (H1 gate)"
    )
    verify.add_argument("--checkpoint", type=Path, required=True)
    verify.add_argument("--display-receipt", type=Path, required=True)
    verify.add_argument(
        "--require-purpose", choices=["EDITORIAL_APPROVED"], required=True
    )
    verify.add_argument(
        "--require-real-episode",
        action="store_true",
        help="refuse synthetic fixture episodes claimed as real",
    )
    verify.add_argument(
        "--recompute", action="store_true", help="rehash every bound target before accepting"
    )
    return parser


def _show(arguments: argparse.Namespace) -> int:
    try:
        bundle = load_bundle(arguments.bundle)
        rehash_bundle_targets(bundle, arguments.bundle)
        targets = recompute_targets(bundle, arguments.bundle)
    except (BundleDriftError, CheckpointError, OSError) as error:
        print(f"show_failed: {error}", file=sys.stderr)
        return 1
    receipt = DisplayReceipt(
        schema_version="display-receipt-v1",
        purpose="EDITORIAL_APPROVED",
        targets=targets,
        target_bundle_sha256=targets_digest(targets),
    )
    atomic_write(arguments.receipt, receipt.canonical_bytes())
    print("=== LOCAL OPERATOR DISPLAY (EDITORIAL_APPROVED) ===")
    print(f"episode: {targets.episode_id}")
    print(f"stage: {targets.stage}")
    print(f"plan {targets.plan_version} sha256: {targets.plan_sha256}")
    print(f"timeline ir sha256: {targets.ir_sha256}")
    print(f"preview sha256: {targets.preview_sha256}")
    print(f"preview trace sha256: {targets.trace_sha256}")
    print(f"edit-source world sha256: {targets.edit_source_world_sha256}")
    print(f"displayed target set sha256: {receipt.target_bundle_sha256}")
    return 0


def targets_digest(targets: DisplayTargets) -> str:
    return hashlib.sha256(canonical_model_bytes(targets)).hexdigest()


def _record(arguments: argparse.Namespace) -> int:
    if arguments.purpose == "FINAL_APPROVED":
        return _record_final(arguments)
    try:
        bundle = load_bundle(arguments.bundle)
        rehash_bundle_targets(bundle, arguments.bundle)
        receipt = DisplayReceipt.model_validate_json(arguments.display_receipt.read_bytes())
        targets = recompute_targets(bundle, arguments.bundle)
    except (OSError, ValidationError, BundleDriftError, CheckpointError) as error:
        print(f"record_failed: {error}", file=sys.stderr)
        return 1
    if receipt.targets != targets or receipt.target_bundle_sha256 != targets_digest(targets):
        print(
            "display_drift: the receipt does not match the bundle's recomputed targets",
            file=sys.stderr,
        )
        return 1
    try:
        if arguments.fixture:
            draft = record_fixture_operation(
                purpose="editorial",
                target_bundle_hash=receipt.target_bundle_sha256,
                decision=arguments.decision,
                actor_id=arguments.actor,
            )
        else:
            draft = record_operation(
                purpose="editorial",
                target_bundle_hash=receipt.target_bundle_sha256,
                decision=arguments.decision,
                actor_id=arguments.actor,
                tty_fd=0,
            )
        record = OperationRecordStore(arguments.out).append(draft)
    except (IngressRefusalError, OSError, ValidationError) as error:
        print(f"record_refused: {error}", file=sys.stderr)
        return 1
    marker = "FIXTURE-MARKED (never a real approval)" if record.fixture_only else "real operator"
    print(f"recorded: {record.record_id} [{marker}]")
    return 0


def _revoked_candidate_ids(arguments: argparse.Namespace) -> frozenset[str]:
    if arguments.revocations is not None:
        return load_revocations(arguments.revocations)
    if DEFAULT_REVOCATIONS_PATH.is_file():
        return load_revocations(DEFAULT_REVOCATIONS_PATH)
    return frozenset()


def _validated_git_sha(value: str) -> str:
    try:
        _GIT_SHA_ADAPTER.validate_python(value)
    except ValidationError as error:
        raise GlobalReviewError(
            "invalid-git-sha",
            f"--git-sha must be a full 40/64-hex commit SHA: {error}",
        ) from error
    return value


def _ensure_not_revoked(arguments: argparse.Namespace) -> None:
    if arguments.candidate_id in _revoked_candidate_ids(arguments):
        raise GlobalReviewError(
            "candidate-revoked",
            f"candidate {arguments.candidate_id} is on the local revocation list",
        )


def _record_final(arguments: argparse.Namespace) -> int:
    missing = [
        name
        for name, value in (
            ("--work-id", arguments.work_id),
            ("--git-sha", arguments.git_sha),
            ("--candidate-id", arguments.candidate_id),
            ("--report-dir", arguments.report_dir),
        )
        if not value
    ]
    if missing:
        print(
            "record_refused: missing-final-argument: "
            f"FINAL_APPROVED requires {' '.join(missing)}",
            file=sys.stderr,
        )
        return 1
    try:
        _validated_git_sha(arguments.git_sha)
        loaded = load_report_set(
            arguments.report_dir,
            full_sha=arguments.git_sha,
            candidate_id=arguments.candidate_id,
        )
        _ensure_not_revoked(arguments)
        binding = loaded.binding(arguments.work_id)
        if arguments.fixture:
            draft = record_fixture_operation(
                purpose="final",
                target_bundle_hash=loaded.report_set_sha256,
                decision=arguments.decision,
                actor_id=arguments.actor,
                final_binding=binding,
            )
        else:
            draft = record_operation(
                purpose="final",
                target_bundle_hash=loaded.report_set_sha256,
                decision=arguments.decision,
                actor_id=arguments.actor,
                tty_fd=0,
                final_binding=binding,
            )
        record = OperationRecordStore(arguments.out).append(draft)
    except (GlobalReviewError, IngressRefusalError, OSError, ValidationError) as error:
        print(f"record_refused: {error}", file=sys.stderr)
        return 1
    marker = "FIXTURE-MARKED (never a real approval)" if record.fixture_only else "real operator"
    print(
        f"recorded: {record.record_id} [{marker}] "
        f"report_set={loaded.report_set_sha256} candidate={arguments.candidate_id} "
        f"work={arguments.work_id}"
    )
    return 0


def _load_record_chain(path: Path) -> tuple[ChainedOperationRecord, ...]:
    lines = [line for line in path.read_bytes().splitlines() if line.strip()]
    if not lines:
        raise CheckpointError("record_empty", f"operation record has no lines: {path}")
    records = tuple(
        ChainedOperationRecord.model_validate_json(line) for line in lines
    )
    validate_supersession_chain(records)
    return records


def _export(arguments: argparse.Namespace) -> int:
    try:
        bundle = load_bundle(arguments.bundle)
        rehash_bundle_targets(bundle, arguments.bundle)
        receipt = DisplayReceipt.model_validate_json(arguments.display_receipt.read_bytes())
        chain = _load_record_chain(arguments.operation_record)
        record = chain[-1]
        checkpoint = assemble_checkpoint(bundle, arguments.bundle, receipt, record)
    except (OSError, ValidationError, BundleDriftError, CheckpointError) as error:
        print(f"export_failed: {error}", file=sys.stderr)
        return 1
    verdict = evaluate_authorization(
        chain,
        purpose="editorial",
        target_hash=receipt.target_bundle_sha256,
        target_type="edit-plan",
        operator_gate=True,
    )
    if not verdict.authorized and verdict.refusal_code == REFUSAL_FIXTURE:
        print(
            "fixture_record_rejected: a fixture-marked record can never satisfy "
            "the operator checkpoint",
            file=sys.stderr,
        )
        return 1
    if not verdict.authorized:
        print(f"record_unauthorized: {verdict.refusal_code}", file=sys.stderr)
        return 1
    atomic_write(arguments.out, checkpoint.canonical_bytes())
    print(f"operator checkpoint: {arguments.out}")
    print(f"displayed target sha256: {checkpoint.displayed_target_sha256}")
    return 0


def _verify(arguments: argparse.Namespace) -> int:  # noqa: PLR0911 (one typed refusal per drift)
    from services.cli.checkpoint_models import OperatorCheckpoint  # noqa: PLC0415
    from services.gates.phase1_technical import PHASE_1_TECHNICAL_FIXTURES  # noqa: PLC0415

    try:
        raw = arguments.checkpoint.read_bytes()
        checkpoint = OperatorCheckpoint.model_validate_json(raw)
        receipt = DisplayReceipt.model_validate_json(arguments.display_receipt.read_bytes())
    except OSError as error:
        print(f"verify_failed: checkpoint_unreadable: {error}", file=sys.stderr)
        return 1
    except ValidationError as error:
        first = error.errors()[0]
        print(
            f"verify_failed: checkpoint_invalid: {first.get('type')}: "
            "a synthetic (fixture-marked) checkpoint can never verify as a real "
            "operator checkpoint",
            file=sys.stderr,
        )
        return 1
    if arguments.require_purpose != checkpoint.purpose or receipt.purpose != checkpoint.purpose:
        print(
            f"verify_failed: purpose_mismatch: checkpoint is {checkpoint.purpose}, "
            f"required {arguments.require_purpose}",
            file=sys.stderr,
        )
        return 1
    if arguments.require_real_episode and (
        checkpoint.fixture_only or checkpoint.episode_id in PHASE_1_TECHNICAL_FIXTURES
    ):
        print(
            "verify_failed: synthetic-episode-rejected: "
            f"episode {checkpoint.episode_id} is a frozen synthetic fixture; a real "
            "owner-supplied episode is required",
            file=sys.stderr,
        )
        return 1
    if arguments.recompute:
        receipt_sha = hashlib.sha256(receipt.canonical_bytes()).hexdigest()
        if receipt_sha != checkpoint.display_receipt_sha256:
            print(
                "verify_failed: receipt_drift: the display receipt no longer hashes to "
                "the checkpoint binding",
                file=sys.stderr,
            )
            return 1
        if receipt.target_bundle_sha256 != checkpoint.displayed_target_sha256:
            print(
                "verify_failed: displayed_target_drift: the receipt target set differs "
                "from the checkpoint binding",
                file=sys.stderr,
            )
            return 1
        if receipt.target_digest() != receipt.target_bundle_sha256:
            print(
                "verify_failed: receipt_self_inconsistent: the receipt digest does not "
                "match its own targets",
                file=sys.stderr,
            )
            return 1
        if receipt.targets.episode_id != checkpoint.episode_id:
            print(
                "verify_failed: episode_drift: the receipt displays a different episode",
                file=sys.stderr,
            )
            return 1
    print(f"verify: purpose={checkpoint.purpose}")
    print(f"verify: episode={checkpoint.episode_id} real_episode={not checkpoint.fixture_only}")
    print(f"verify: displayed_target={checkpoint.displayed_target_sha256}")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "show":
        return _show(arguments)
    if arguments.command == "record":
        return _record(arguments)
    if arguments.command == "verify":
        return _verify(arguments)
    return _export(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
