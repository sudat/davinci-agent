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

from pydantic import ValidationError

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
    record.add_argument("--bundle", type=Path, required=True)
    record.add_argument("--display-receipt", type=Path, required=True)
    record.add_argument("--purpose", choices=["EDITORIAL_APPROVED"], required=True)
    record.add_argument("--decision", choices=["approve", "reject"], required=True)
    record.add_argument("--out", type=Path, required=True)
    record.add_argument("--actor", default="local-operator")
    record.add_argument("--fixture", action="store_true", help="QA seam: fixture-marked record")
    export = sub.add_parser("export", help="assemble the operator_checkpoint artifact")
    export.add_argument("--bundle", type=Path, required=True)
    export.add_argument("--display-receipt", type=Path, required=True)
    export.add_argument("--operation-record", type=Path, required=True)
    export.add_argument("--out", type=Path, required=True)
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


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "show":
        return _show(arguments)
    if arguments.command == "record":
        return _record(arguments)
    return _export(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
