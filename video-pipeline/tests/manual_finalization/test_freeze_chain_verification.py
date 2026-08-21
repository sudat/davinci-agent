"""Blocker-fix regression: authorization boundaries verify the record chain.

``assemble_freeze_package`` (manual-freeze authorization) and
``authorize_presentation`` (presentation authorization) used to accept raw
in-memory record tuples and evaluate semantics only — a record set whose
keyed hash chain was broken (offline forgery/tamper) could still authorize.
The fixed boundaries require the caller-supplied ``chain_key`` and verify
the full supersession chain BEFORE any authorization evaluation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.approvals.chain_key import load_chain_key
from services.approvals.presentation import (
    PresentationApprovalBinding,
    PresentationApprovalError,
    authorize_presentation,
)
from services.approvals.verify import evaluate_authorization, validate_supersession_chain
from services.final_review.ledger import FinalReviewLedger
from services.final_review.routes import RouteRefusal, route_unsupported
from services.manual_finalization.freeze import (
    FreezeInputs,
    FreezeRefusal,
    assemble_freeze_package,
)
from services.manual_finalization.store import FreezeStore
from tests.final_review.support import (
    EPISODE,
    fixture_record,
    make_records_store,
    sha,
)

TARGET = sha("freeze-target")


def freeze_inputs(target: str) -> dict[str, object]:
    return {
        "episode_id": EPISODE,
        "fixture_only": True,
        "target_set_hash": target,
        "drt_drp": {
            "toolchain_lock_sha256": sha("toolchain-lock"),
            "input_hashes": ({"name": "edit-plan", "sha256": sha("plan-v1")},),
            "reproduction_commands": ("uv run python -m services.build.check --episode ep",),
        },
        "render": {"name": "final-render", "sha256": sha("final-render")},
        "timeline_fingerprint": sha("timeline"),
        "conformance_fingerprint": sha("conformance"),
        "change_log": ({"entry": "manual intro trim"},),
        "last_committed_plan": {"name": "edit-plan", "sha256": sha("plan-v1")},
        "reports": (
            {"kind": "rights", "sha256": sha("rights-report")},
            {"kind": "privacy", "sha256": sha("privacy-report")},
            {"kind": "qc", "sha256": sha("qc-report")},
        ),
    }


def tampered_store(tmp_path: Path):
    store = make_records_store(tmp_path)
    fixture_record(store, purpose="manual_freeze", target_hash=TARGET)
    records = tuple(
        record.model_copy(update={"decision": "forged"})
        if index == 0
        else record
        for index, record in enumerate(store.all_records())
    )
    chain_key = load_chain_key(store.records_path, create=False)
    with pytest.raises(ValueError, match="chain"):
        validate_supersession_chain(records, chain_key=chain_key)
    return records, chain_key


def test_freeze_boundary_refuses_broken_chain(tmp_path: Path) -> None:
    records, chain_key = tampered_store(tmp_path)
    with pytest.raises(FreezeRefusal, match="records-chain-invalid"):
        assemble_freeze_package(
            records=records,
            inputs=freeze_inputs(TARGET),
            reason_detail="unsupported",
            fixture_mode=True,
            chain_key=chain_key,
        )


def test_presentation_boundary_refuses_broken_chain(tmp_path: Path) -> None:
    records, chain_key = tampered_store(tmp_path)
    binding = PresentationApprovalBinding.model_validate(
        {
            "episode_id": EPISODE,
            "preview_artifact_sha256": sha("preview"),
            "presentation_manifest_sha256": sha("manifest"),
            "editorial_fingerprint_sha256": sha("editorial"),
        }
    )
    with pytest.raises(PresentationApprovalError, match="records-chain-invalid"):
        authorize_presentation(records, binding, operator_gate=False, chain_key=chain_key)


def test_route_unsupported_refuses_broken_chain(tmp_path: Path) -> None:
    records, chain_key = tampered_store(tmp_path)
    ledger = FinalReviewLedger(tmp_path / "final-review-events.jsonl")
    with pytest.raises(RouteRefusal, match="records-chain-invalid"):
        route_unsupported(
            ledger,
            records=records,
            inputs=FreezeInputs.model_validate(freeze_inputs(TARGET)),
            freeze_store=FreezeStore(tmp_path / "manual-finalization"),
            reason_detail="unsupported",
            chain_key=chain_key,
            fixture_mode=True,
        )


def test_freeze_boundary_accepts_verified_chain(tmp_path: Path) -> None:
    store = make_records_store(tmp_path)
    fixture_record(store, purpose="manual_freeze", target_hash=TARGET)
    chain_key = load_chain_key(store.records_path, create=False)
    package = assemble_freeze_package(
        records=store.all_records(),
        inputs=freeze_inputs(TARGET),
        reason_detail="unsupported",
        fixture_mode=True,
        chain_key=chain_key,
    )
    assert package.automation_frozen is True
    verdict = evaluate_authorization(
        store.all_records(),
        purpose="manual_freeze",
        target_hash=TARGET,
        target_type="frozen-timeline",
        operator_gate=False,
    )
    assert verdict.authorized is True
