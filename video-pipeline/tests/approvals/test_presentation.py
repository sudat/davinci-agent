"""Todo-61 acceptance: PRESENTATION_APPROVED binding, supersession, isolation.

Offline: a presentation approval is its OWN Todo-13 chained operation
record bound to (Preview artifact sha, Presentation Manifest sha); any
manifest/profile/asset drift SUPERSEDES it (enforced in code), editorial
meaning/timing drift ALSO supersedes EDITORIAL_APPROVED, and reusing an
Editorial or Final record for the presentation purpose is the typed
``purpose_reuse`` refusal — never an authorization. Malformed bindings
and tampered stores fail closed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from services.approvals.models import OperationDraft
from services.approvals.presentation import (
    REFUSAL_PURPOSE_REUSE,
    PresentationApprovalBinding,
    PresentationApprovalError,
    authorize_presentation,
    bind_presentation_approval,
    enforce_supersession,
    evaluate_binding_supersession,
    evaluate_supersession,
    other_purpose_for_target,
    record_presentation_approval,
)
from services.approvals.store import OperationRecordError, OperationRecordStore
from services.contracts.primitives import RecordFrameSpan
from services.contracts.serialization import artifact_content_hash
from services.fixtures.manifest_phase3 import Phase3FixtureManifest
from services.presentation.asset_registry import (
    RegistrySnapshot,
    registry_from_phase3_manifests,
)
from services.presentation.models import EpisodePresentationProfile
from services.presentation.profiles import resolve_presentation_profile
from tests.approvals.support import fixture_draft, hand_chained, make_store
from tests.presentation.test_manifest import (
    MANIFEST_DIR,
    _channel,
    _compile,
    _edit_plan,
    _system,
    _timeline_ir,
)

if TYPE_CHECKING:
    from services.contracts.timeline_ir import TimelineIrProduction
    from services.presentation.manifest import PresentationManifest
    from services.presentation.models import ResolvedPresentationProfile

PREVIEW_SHA = "d" * 64


def _load_brand(fixture_id: str) -> Phase3FixtureManifest:
    return Phase3FixtureManifest.model_validate_json(
        (MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
    )


@pytest.fixture(scope="module")
def brand_a() -> Phase3FixtureManifest:
    return _load_brand("p3-brand-a")


@pytest.fixture(scope="module")
def registry() -> RegistrySnapshot:
    return registry_from_phase3_manifests(MANIFEST_DIR)


@pytest.fixture(scope="module")
def profile_a(
    registry: RegistrySnapshot, brand_a: Phase3FixtureManifest
) -> ResolvedPresentationProfile:
    catalog = tuple(sorted(entry.asset_id for entry in registry.entries))
    return resolve_presentation_profile(
        _system(brand_a, catalog),
        episode=EpisodePresentationProfile(episode_id="episode-p3"),
        registry=registry,
    )


@pytest.fixture(scope="module")
def manifest_a(
    brand_a: Phase3FixtureManifest,
    profile_a: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
) -> PresentationManifest:
    return _compile(_edit_plan(brand_a), _timeline_ir(brand_a), profile_a, registry)


@pytest.fixture(scope="module")
def manifest_b(
    brand_a: Phase3FixtureManifest, registry: RegistrySnapshot
) -> PresentationManifest:
    catalog = tuple(sorted(entry.asset_id for entry in registry.entries))
    brand_b = _load_brand("p3-brand-b")
    profile_b = resolve_presentation_profile(
        _system(brand_a, catalog),
        episode=EpisodePresentationProfile(episode_id="episode-p3"),
        channel=_channel(brand_b),
        registry=registry,
    )
    return _compile(_edit_plan(brand_a), _timeline_ir(brand_a), profile_b, registry)


def _editorial_drifted_ir(base: Phase3FixtureManifest) -> TimelineIrProduction:
    ir = _timeline_ir(base)
    track = ir.tracks[0]
    item = track.items[0]
    shifted = item.model_copy(
        update={
            "record_span": RecordFrameSpan(
                start_frame=item.record_span.start_frame,
                end_frame=item.record_span.end_frame + 3,
            )
        }
    )
    drifted_track = track.model_copy(update={"items": (shifted, *track.items[1:])})
    drifted = ir.model_copy(update={"tracks": (drifted_track, *ir.tracks[1:])})
    return drifted.model_copy(update={"content_hash": artifact_content_hash(drifted)})


# ------------------------------------------------------------------ binding --


def test_binding_binds_preview_and_manifest_hashes(
    manifest_a: PresentationManifest,
) -> None:
    binding = bind_presentation_approval(
        manifest_a, preview_artifact_sha256=PREVIEW_SHA
    )
    assert binding.preview_artifact_sha256 == PREVIEW_SHA
    assert binding.presentation_manifest_sha256 == manifest_a.manifest_sha256
    assert binding.editorial_fingerprint_sha256 == manifest_a.editorial_fingerprint
    assert binding.bundle_hash() == binding.bundle_hash()
    other = bind_presentation_approval(manifest_a, preview_artifact_sha256="e" * 64)
    assert other.bundle_hash() != binding.bundle_hash()

    with pytest.raises(ValidationError):
        PresentationApprovalBinding(
            episode_id="episode-p3",
            preview_artifact_sha256="not-a-sha",
            presentation_manifest_sha256=manifest_a.manifest_sha256,
            editorial_fingerprint_sha256=manifest_a.editorial_fingerprint,
        )


def test_marked_fixture_presentation_record_authorizes(
    manifest_a: PresentationManifest, tmp_path: Path
) -> None:
    store: OperationRecordStore = make_store(tmp_path)
    binding = bind_presentation_approval(
        manifest_a, preview_artifact_sha256=PREVIEW_SHA
    )
    record = record_presentation_approval(store, binding, actor_id="test-operator")
    assert record.purpose == "presentation"
    assert record.target_type == "presentation-bundle"
    assert record.target_hash == binding.bundle_hash()
    assert record.fixture_only is True
    store.verify_chain()

    verdict = authorize_presentation(store.all_records(), binding)
    assert verdict.authorized is True
    assert verdict.record_id == record.record_id
    operator = authorize_presentation(store.all_records(), binding, operator_gate=True)
    assert operator.authorized is False
    assert operator.refusal_code == "fixture-record"


# --------------------------------------------------------------- isolation --


def test_editorial_or_final_purpose_reuse_is_typed(
    manifest_a: PresentationManifest, tmp_path: Path
) -> None:
    binding = bind_presentation_approval(
        manifest_a, preview_artifact_sha256=PREVIEW_SHA
    )
    target = binding.bundle_hash()
    for purpose in ("editorial", "final"):
        reused_store: OperationRecordStore = make_store(tmp_path / f"reuse-{purpose}")
        reused_store.append(fixture_draft(purpose=purpose, target=target))
        verdict = authorize_presentation(reused_store.all_records(), binding)
        assert verdict.authorized is False
        assert verdict.refusal_code == REFUSAL_PURPOSE_REUSE
        assert verdict.purpose == purpose
    reused = (hand_chained(seq=1, purpose="editorial", target=target),)
    assert other_purpose_for_target(reused, target) == "editorial"
    assert other_purpose_for_target((), target) is None


# ------------------------------------------------------------- supersession --


def test_changed_asset_or_manifest_after_approval_supersedes(
    manifest_a: PresentationManifest,
    manifest_b: PresentationManifest,
) -> None:
    binding = bind_presentation_approval(
        manifest_a, preview_artifact_sha256=PREVIEW_SHA
    )
    verdict = evaluate_supersession(manifest_a, manifest_b)
    assert verdict.presentation_superseded is True
    assert verdict.editorial_superseded is False
    assert "profile_drift" in verdict.causes
    assert "asset_drift" in verdict.causes
    assert "manifest_drift" in verdict.causes

    binding_verdict = evaluate_binding_supersession(binding, manifest_b)
    assert binding_verdict.presentation_superseded is True
    assert binding_verdict.editorial_superseded is False

    with pytest.raises(PresentationApprovalError) as raised:
        enforce_supersession(verdict)
    assert raised.value.code == "superseded_presentation"

    fresh_binding = bind_presentation_approval(
        manifest_b, preview_artifact_sha256=PREVIEW_SHA
    )
    assert authorize_presentation((), fresh_binding).refusal_code == "no-record"


def test_editorial_change_also_supersedes_editorial(
    brand_a: Phase3FixtureManifest,
    profile_a: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
    manifest_a: PresentationManifest,
) -> None:
    drifted_manifest = _compile(
        _edit_plan(brand_a), _editorial_drifted_ir(brand_a), profile_a, registry
    )
    assert drifted_manifest.editorial_fingerprint != manifest_a.editorial_fingerprint
    binding = bind_presentation_approval(
        manifest_a, preview_artifact_sha256=PREVIEW_SHA
    )
    verdict = evaluate_binding_supersession(binding, drifted_manifest)
    assert verdict.presentation_superseded is True
    assert verdict.editorial_superseded is True
    assert "editorial_semantic_drift" in verdict.causes
    assert (
        evaluate_supersession(manifest_a, drifted_manifest).editorial_superseded is True
    )

    with pytest.raises(PresentationApprovalError) as raised:
        enforce_supersession(verdict)
    assert raised.value.code == "superseded_editorial"


def test_no_drift_keeps_authorization(
    manifest_a: PresentationManifest, tmp_path: Path
) -> None:
    store: OperationRecordStore = make_store(tmp_path)
    binding = bind_presentation_approval(
        manifest_a, preview_artifact_sha256=PREVIEW_SHA
    )
    record_presentation_approval(store, binding, actor_id="test-operator")
    verdict = evaluate_supersession(manifest_a, manifest_a)
    assert verdict.presentation_superseded is False
    assert verdict.editorial_superseded is False
    enforce_supersession(verdict)
    assert authorize_presentation(store.all_records(), binding).authorized is True


# ---------------------------------------------------------------- malformed --


def test_malformed_records_and_tampered_store_fail_closed(
    manifest_a: PresentationManifest, tmp_path: Path
) -> None:
    store: OperationRecordStore = make_store(tmp_path)
    binding = bind_presentation_approval(
        manifest_a, preview_artifact_sha256=PREVIEW_SHA
    )
    record_presentation_approval(store, binding, actor_id="test-operator")
    path = store.records_path
    lines = path.read_bytes().splitlines()
    lines[0] = b'{"tampered": true}'
    path.write_bytes(b"\n".join(lines) + b"\n")
    with pytest.raises(OperationRecordError) as raised:
        OperationRecordStore(path).all_records()
    assert raised.value.code == "record-line-invalid"

    with pytest.raises(ValidationError):
        OperationDraft.model_validate(
            fixture_draft(purpose="editorial").model_dump()
            | {"target_type": "presentation-bundle"}
        )
