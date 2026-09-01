from __future__ import annotations

import hashlib
import json
from pathlib import Path

from services.cli.checkpoint import main as checkpoint_main
from services.cli.checkpoint_models import DisplayReceipt, DisplayTargets, OperatorCheckpoint
from services.foundation_io import canonical_model_bytes


def _receipt() -> DisplayReceipt:
    targets = DisplayTargets(
        episode_id="real-owner-episode-01",
        stage="PREVIEW_READY",
        plan_version="v1",
        plan_sha256="0" * 64,
        ir_sha256="1" * 64,
        preview_sha256="2" * 64,
        trace_sha256="3" * 64,
        edit_source_world_sha256="4" * 64,
    )
    return DisplayReceipt(
        purpose="EDITORIAL_APPROVED",
        targets=targets,
        target_bundle_sha256=_digest(targets),
    )


def _digest(targets: DisplayTargets) -> str:
    return hashlib.sha256(canonical_model_bytes(targets)).hexdigest()


def _checkpoint(receipt: DisplayReceipt, episode: str) -> OperatorCheckpoint:
    return OperatorCheckpoint(
        schema_version="operator-checkpoint-v1",
        purpose="EDITORIAL_APPROVED",
        episode_id=episode,
        fixture_only=False,
        eligibility_status="supported",
        fixture_manifest_sha256="0" * 64,
        edit_source_world_sha256="0" * 64,
        media_sha256=("0" * 64,),
        initial_plan_sha256="0" * 64,
        initial_ir_sha256="0" * 64,
        initial_preview_sha256="0" * 64,
        final_plan_sha256="0" * 64,
        final_ir_sha256="0" * 64,
        final_preview_sha256="0" * 64,
        event_chain=(),
        toolchain_lock_sha256="0" * 64,
        translator_policy_sha256="0" * 64,
        production_policy_sha256="0" * 64,
        displayed_target_sha256=receipt.target_bundle_sha256,
        display_receipt_sha256=hashlib.sha256(receipt.canonical_bytes()).hexdigest(),
        operation_record_id="op-1",
        operation_record_sha256="0" * 64,
        actor_id="local-operator",
        uid=501,
        tty="/dev/ttys000",
        wall_time_unix=1,
    )


def _write_pair(tmp_path: Path, episode: str) -> tuple[Path, Path]:
    receipt = _receipt()
    checkpoint = _checkpoint(receipt, episode)
    receipt_path = tmp_path / "display.json"
    checkpoint_path = tmp_path / "checkpoint.json"
    receipt_path.write_bytes(receipt.canonical_bytes())
    checkpoint_path.write_bytes(checkpoint.canonical_bytes())
    return checkpoint_path, receipt_path


def test_verify_accepts_a_consistent_real_episode_checkpoint(tmp_path: Path) -> None:
    checkpoint_path, receipt_path = _write_pair(tmp_path, "real-owner-episode-01")
    code = checkpoint_main(
        [
            "verify",
            "--checkpoint",
            str(checkpoint_path),
            "--display-receipt",
            str(receipt_path),
            "--require-purpose",
            "EDITORIAL_APPROVED",
            "--require-real-episode",
            "--recompute",
        ]
    )
    assert code == 0


def test_verify_refuses_synthetic_episode_claimed_as_real(tmp_path: Path) -> None:
    checkpoint_path, receipt_path = _write_pair(tmp_path, "p1-ref-05-review-mix")
    code = checkpoint_main(
        [
            "verify",
            "--checkpoint",
            str(checkpoint_path),
            "--display-receipt",
            str(receipt_path),
            "--require-purpose",
            "EDITORIAL_APPROVED",
            "--require-real-episode",
            "--recompute",
        ]
    )
    assert code == 1


def test_verify_refuses_drifted_receipt_bytes(tmp_path: Path) -> None:
    checkpoint_path, receipt_path = _write_pair(tmp_path, "real-owner-episode-01")
    drifted = json.loads(receipt_path.read_bytes())
    drifted["targets"]["plan_version"] = "v2"
    receipt_path.write_text(json.dumps(drifted, sort_keys=True, separators=(",", ":")))
    code = checkpoint_main(
        [
            "verify",
            "--checkpoint",
            str(checkpoint_path),
            "--display-receipt",
            str(receipt_path),
            "--require-purpose",
            "EDITORIAL_APPROVED",
            "--require-real-episode",
            "--recompute",
        ]
    )
    assert code == 1


def test_verify_refuses_fixture_marked_checkpoint(tmp_path: Path) -> None:
    checkpoint_path, receipt_path = _write_pair(tmp_path, "real-owner-episode-01")
    forged = json.loads(checkpoint_path.read_bytes())
    forged["fixture_only"] = True
    checkpoint_path.write_text(json.dumps(forged, sort_keys=True, separators=(",", ":")))
    code = checkpoint_main(
        [
            "verify",
            "--checkpoint",
            str(checkpoint_path),
            "--display-receipt",
            str(receipt_path),
            "--require-purpose",
            "EDITORIAL_APPROVED",
            "--require-real-episode",
            "--recompute",
        ]
    )
    assert code == 1
