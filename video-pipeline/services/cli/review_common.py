"""Shared H1 checkpoint helpers: recomputed targets + artifact assembly.

``recompute_targets`` rebuilds the display target set from the bundle's bytes
(s drift anywhere is a typed refusal), and ``assemble_checkpoint`` folds the
bundle, the display receipt, and the operator record into the strict
``operator_checkpoint`` artifact with the full event chain and policy hashes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from services.cli.chain import PREVIEW_TOOLS_LOCK
from services.cli.checkpoint_models import (
    CheckpointEventRow,
    DisplayReceipt,
    DisplayTargets,
    OperatorCheckpoint,
)
from services.contracts.edit_plan_0c import EditPlan0C
from services.contracts.timeline_ir import TimelineIr0C
from services.editorial.correction_metrics import build_metrics
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.preview.models import PreviewTraceManifest
from services.preview.tools import PinnedTools, load_pinned_tools
from services.review_command.events import event_proposal, parse_event_stream
from services.validate.edit_commit_schema import tuplize

if TYPE_CHECKING:
    from services.approvals.models import ChainedOperationRecord
    from services.cli.bundle import ReviewBundle

ZERO_SHA256 = "0" * 64


class CheckpointError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def recompute_targets(
    bundle: ReviewBundle, bundle_file: Path
) -> DisplayTargets:
    preview_dir = bundle_file.parent / bundle.current.preview_dir
    plan_path = bundle_file.parent / bundle.store_dir / (
        f"plan-v{bundle.current.plan_version[1:]}.json"
    )
    ir_path = bundle_file.parent / bundle.store_dir / (
        f"ir-v{bundle.current.plan_version[1:]}.json"
    )
    for path, expected, label in (
        (plan_path, bundle.current.plan_sha256, "plan"),
        (ir_path, bundle.current.ir_sha256, "ir"),
        (preview_dir / "preview.mp4", bundle.current.preview_sha256, "preview"),
        (preview_dir / "preview-trace.json", bundle.current.trace_sha256, "trace"),
    ):
        actual = sha256_file(path)
        if actual != expected:
            raise CheckpointError(
                "target_drift", f"{label} hashes {actual[:12]} but the bundle shows {expected[:12]}"
            )
    return DisplayTargets(
        episode_id=bundle.episode_id,
        stage=bundle.stage,
        plan_version=bundle.current.plan_version,
        plan_sha256=bundle.current.plan_sha256,
        ir_sha256=bundle.current.ir_sha256,
        preview_sha256=bundle.current.preview_sha256,
        trace_sha256=bundle.current.trace_sha256,
        edit_source_world_sha256=bundle.edit_source_world_sha256,
    )


def _classification_row(*, applied: bool, reason: str | None) -> str:
    if applied:
        return "clear"
    if reason in ("ambiguous", "conflict"):
        return reason
    return "unclassified"


def _event_rows(bundle: ReviewBundle, bundle_file: Path) -> tuple[CheckpointEventRow, ...]:
    log = bundle_file.parent / bundle.events_log
    events = parse_event_stream(log.read_bytes())
    rows: list[CheckpointEventRow] = []
    for event in events:
        if event.kind == "proposal_recorded":
            continue
        proposal = event_proposal(event)
        rows.append(
            CheckpointEventRow(
                event_id=event.event_id,
                kind=event.kind,
                proposal_id=proposal.proposal_id,
                classification=_classification_row(
                    applied=event.applied, reason=event.reason
                ),
                base_plan_version=event.base_plan_version,
                result_plan_version=event.result_plan_version,
                command_kind=proposal.command_kind,
            )
        )
    return tuple(rows)


def assemble_checkpoint(
    bundle: ReviewBundle,
    bundle_file: Path,
    receipt: DisplayReceipt,
    record: ChainedOperationRecord,
) -> OperatorCheckpoint:
    targets = recompute_targets(bundle, bundle_file)
    if bundle.fixture_only:
        raise CheckpointError(
            "fixture_lineage_rejected",
            "H1 operator checkpoints bind real owner-supplied episodes; a fixture "
            "lineage can never produce one (record it as QA evidence instead)",
        )
    if receipt.targets != targets:
        raise CheckpointError(
            "receipt_drift",
            "the display receipt does not match the bundle's recomputed targets",
        )
    if record.fixture_only:
        raise CheckpointError(
            "fixture_record_rejected",
            "a fixture-marked operation record can never produce an operator checkpoint",
        )
    if record.decision != "approve" or record.purpose != "editorial":
        raise CheckpointError(
            "record_unauthorized",
            f"record {record.record_id} is {record.purpose}/{record.decision}; "
            "EDITORIAL_APPROVED requires editorial/approve",
        )
    return OperatorCheckpoint(
        schema_version="operator-checkpoint-v1",
        purpose="EDITORIAL_APPROVED",
        episode_id=bundle.episode_id,
        fixture_only=bundle.fixture_only,
        eligibility_status=bundle.eligibility_status,
        fixture_manifest_sha256=bundle.fixture_manifest_sha256,
        edit_source_world_sha256=bundle.edit_source_world_sha256,
        media_sha256=tuple(media.sha256 for media in bundle.media),
        initial_plan_sha256=bundle.initial.plan_sha256,
        initial_ir_sha256=bundle.initial.ir_sha256,
        initial_preview_sha256=bundle.initial.preview_sha256,
        final_plan_sha256=bundle.current.plan_sha256,
        final_ir_sha256=bundle.current.ir_sha256,
        final_preview_sha256=bundle.current.preview_sha256,
        event_chain=_event_rows(bundle, bundle_file),
        toolchain_lock_sha256=bundle.toolchain_lock_sha256,
        translator_policy_sha256=bundle.translator_policy_sha256,
        production_policy_sha256=bundle.production_policy_sha256
        if bundle.production_policy_sha256 is not None
        else ZERO_SHA256,
        displayed_target_sha256=receipt.target_bundle_sha256,
        display_receipt_sha256=sha256_bytes(receipt.canonical_bytes()),
        operation_record_id=record.record_id,
        operation_record_sha256=sha256_bytes(canonical_model_bytes(record)),
        actor_id=record.actor_id,
        uid=record.uid if record.uid is not None else 0,
        tty=record.tty if record.tty is not None else "",
        wall_time_unix=record.wall_time_unix if record.wall_time_unix is not None else 0,
    )


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def load_tools() -> PinnedTools:
    return load_pinned_tools(PREVIEW_TOOLS_LOCK)


def mezzanine_for(bundle_file: Path, bundle: ReviewBundle) -> Path:
    media = bundle.media[0]
    path = bundle_file.parent / media.path
    if sha256_file(path) != media.sha256:
        raise CheckpointError("media_drift", f"edit-source media drifted: {path}")
    return path


def previous_trace(bundle_file: Path, bundle: ReviewBundle) -> PreviewTraceManifest:
    trace = bundle_file.parent / bundle.current.preview_dir / "preview-trace.json"
    return PreviewTraceManifest.model_validate_json(trace.read_bytes())


def write_metrics(bundle_file: Path, bundle: ReviewBundle, out_dir: Path) -> str:
    events = parse_event_stream((bundle_file.parent / bundle.events_log).read_bytes())
    metrics = build_metrics(
        events, episode_id=bundle.episode_id, fixture_only=bundle.fixture_only
    )
    atomic_write(out_dir / "correction-metrics.json", metrics.canonical_bytes())
    return "correction-metrics.json"


def store_ir(ir_path: Path) -> TimelineIr0C:
    document = json.loads(ir_path.read_bytes())
    return TimelineIr0C.model_validate(tuplize(document))


def store_plan(plan_path: Path) -> EditPlan0C:
    document = json.loads(plan_path.read_bytes())
    return EditPlan0C.model_validate(tuplize(document))


__all__ = [
    "CheckpointError",
    "assemble_checkpoint",
    "load_tools",
    "mezzanine_for",
    "previous_trace",
    "recompute_targets",
    "store_ir",
    "store_plan",
    "write_metrics",
]
