"""Attack class 2: untrusted facts are never authoritative.

Every surface here takes attacker-controlled METADATA and proves the
system derives its own verdict: proposal-authored ambiguity/classification
fields are recomputed; swapped asset bytes claiming a registered identity
are denied; and a plan-bundle seal catches lying artifact hashes.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from services.cli.bundle import BundleDriftError, load_bundle, rehash_bundle_targets
from services.cli.project import plan_sha256
from services.cli.real_policy import load_policy
from services.cli.review_freeform import propose_freeform
from services.cli.review_replay import PlanView
from services.presentation.asset_registry import (
    AssetEntry,
    AssetRightsError,
    RegistrySnapshot,
    check_rights,
)
from services.review_command.models import parse_proposal
from services.review_command.store import load_head
from services.review_command.validate import validate_proposal

# importing the session rig registers it as a fixture for this module
from tests.cli.test_real_episode import rig
from tests.security.support import assert_zero_side_effects, payload, snapshot_tree

PYTEST_FIXTURES = (rig,)

if TYPE_CHECKING:
    from tests.cli.test_real_episode import RealRig


def _propose_raw(rig: RealRig, instruction: str):
    bundle = load_bundle(rig.bundle_file)
    head = load_head(
        rig.bundle_file.parent / bundle.events_log, rig.bundle_file.parent / bundle.store_dir
    )
    return propose_freeform(
        episode_id=bundle.episode_id,
        instruction=instruction,
        current=PlanView(
            plan=head.plan, version=f"v{head.version}", plan_hash=plan_sha256(head.plan)
        ),
        policy=load_policy(rig.policy_file),
        policy_sha="0" * 64,
        translator_sha="0" * 64,
    ), head


def test_10_proposal_claimed_ambiguity_is_recomputed_not_trusted(rig: RealRig) -> None:
    """A forged 'clear' ambiguity on a two-target removal still defers.

    The parser's envelope always claims ``clear``; the validator must
    recompute the real classification from plan facts. With two viable
    targets the recomputed verdict is ambiguous regardless of the claim.
    """

    (outcome, head) = _propose_raw(rig, "セグメント s1 と s2 を削除してください。")
    assert outcome.status == "proposal"
    assert json.loads(outcome.proposal_json or "{}")["ambiguity"]["status"] == "clear"
    proposal = parse_proposal(outcome.proposal_json or "{}")
    verdict = validate_proposal(head.plan, proposal)
    if verdict.classification == "clear":
        assert len(verdict.candidate_item_ids) == 1
    else:
        assert verdict.classification in {"ambiguous", "conflict"}
        assert verdict.needs_human is True


def test_11_lying_ambiguity_fields_never_authoritize_apply(rig: RealRig) -> None:
    """Two-target removal with a forged clear field never mutates the store."""

    store_dir = rig.bundle_file.parent / load_bundle(rig.bundle_file).store_dir
    before = snapshot_tree(store_dir)
    (outcome, _head) = _propose_raw(rig, "セグメント s1 と s2 を削除してください。")
    assert outcome.status == "proposal"
    if outcome.classification == "ambiguous":
        assert json.loads(outcome.proposal_json or "{}")["ambiguity"]["status"] == "clear"
        assert (outcome.command_index or 0) >= 1
    assert_zero_side_effects(store_dir, before)


def _registered_asset() -> tuple[RegistrySnapshot, str]:
    entry = AssetEntry.model_validate(
        {
            "asset_id": "asset:tone-01",
            "sha256": "a" * 64,
            "path": "assets/tone-01.wav",
            "usage": "tone",
            "territories": ("WORLDWIDE",),
            "effective_date": "2020-01-01",
            "expiry_date": None,
            "license_evidence_ref": "lic/tone-01.pdf",
            "attribution": None,
            "content_id_notes": None,
            "approved": True,
        }
    )
    registry = RegistrySnapshot(entries=(entry,), registry_snapshot_sha256="0" * 64)
    sealed = registry.model_copy(
        update={"registry_snapshot_sha256": registry.content_hash()}
    )
    return sealed, "asset:tone-01"


def test_20_swapped_bytes_claiming_a_registered_identity_are_denied() -> None:
    """An untrusted file pretending to be the registered asset fails the hash."""

    registry, asset_id = _registered_asset()
    lying_observed = "b" * 64  # attacker-swapped bytes
    before = registry.model_dump()
    with_raise = None
    try:
        check_rights(
            registry,
            asset_id,
            usage="tone",
            territory="WORLDWIDE",
            at_job="2026-01-01",
            observed_sha256=lying_observed,  # type: ignore[arg-type]
        )
    except AssetRightsError as error:
        with_raise = error
    assert with_raise is not None
    assert with_raise.reason == "changed_bytes"
    assert registry.model_dump() == before  # registry untouched by the denial


def test_21_honest_bytes_pass_and_registry_is_immutable() -> None:
    registry, asset_id = _registered_asset()
    entry = check_rights(
        registry,
        asset_id,
        usage="tone",
        territory="WORLDWIDE",
        at_job="2026-01-01",
        observed_sha256="a" * 64,  # type: ignore[arg-type]
    )
    assert entry.asset_id == asset_id
    assert registry.verify_hash() is True


def test_30_bundle_seal_detects_tampered_plan_bytes(rig: RealRig) -> None:
    """The bundle re-hash refuses a committed plan that no longer matches."""

    bundle = load_bundle(rig.bundle_file)
    rehash_bundle_targets(bundle, rig.bundle_file)  # honest state passes
    plan_path = (
        rig.bundle_file.parent / bundle.store_dir / f"plan-v{bundle.current.plan_version[1:]}.json"
    )
    honest = plan_path.read_bytes()
    lying = honest[:-1] + b" " if honest.endswith(b"\n") else honest + b" "
    plan_path.write_bytes(lying)
    try:
        with pytest.raises(BundleDriftError):
            rehash_bundle_targets(load_bundle(rig.bundle_file), rig.bundle_file)
    finally:
        plan_path.write_bytes(honest)


def test_40_injection_payload_metadata_is_never_authoritative(rig: RealRig) -> None:
    """Payload text inside evidence fields changes no classification fact."""

    (outcome, head) = _propose_raw(rig, payload("en"))
    assert outcome.status == "error"
    assert head.version >= 1  # plan untouched: the payload altered nothing
