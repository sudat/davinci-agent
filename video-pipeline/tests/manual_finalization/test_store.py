"""FreezeStore: append-only versions, frozen state, typed Builder refusal."""

from __future__ import annotations

import pytest

from services.manual_finalization.freeze import (
    FreezeInputs,
    assemble_freeze_package,
)
from services.manual_finalization.store import (
    FreezeStateError,
    FreezeStore,
    FrozenJobRefusal,
)
from tests.final_review.support import (
    EPISODE,
    fixture_record,
    make_records_store,
    sha,
)


def freeze_payload(target: str, render_seed: str = "final-render") -> dict[str, object]:
    return {
        "episode_id": EPISODE,
        "fixture_only": True,
        "target_set_hash": target,
        "drt_drp": {
            "toolchain_lock_sha256": sha("toolchain-lock"),
            "input_hashes": ({"name": "edit-plan", "sha256": sha("plan-v1")},),
            "reproduction_commands": ("uv run python -m services.build.check",),
        },
        "render": {"name": "final-render", "sha256": sha(render_seed)},
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


def make_package(tmp_path, target: str, render_seed: str = "final-render"):
    from services.approvals.chain_key import load_chain_key  # noqa: PLC0415

    store = make_records_store(tmp_path)
    fixture_record(store, purpose="manual_freeze", target_hash=target)
    return assemble_freeze_package(
        records=store.all_records(),
        inputs=FreezeInputs.model_validate(freeze_payload(target, render_seed)),
        reason_detail="manual finish",
        chain_key=load_chain_key(store.records_path, create=False),
        fixture_mode=True,
    )


def test_publish_freezes_job_and_records_state(tmp_path) -> None:
    target = sha("freeze-target")
    freeze_store = FreezeStore(tmp_path / "manual-finalization")
    package = make_package(tmp_path, target)
    published = freeze_store.publish(package)
    assert published.version == 1
    assert freeze_store.is_frozen()
    state = freeze_store.frozen_state()
    assert state is not None
    assert state.automation_frozen is True
    assert state.target_set_hash == target


def test_builder_refuses_any_mutation_after_freeze(tmp_path) -> None:
    freeze_store = FreezeStore(tmp_path / "manual-finalization")
    freeze_store.publish(make_package(tmp_path, sha("freeze-target")))
    with pytest.raises(FrozenJobRefusal, match="job-frozen"):
        freeze_store.assert_buildable()
    called = False

    def builder() -> None:
        nonlocal called
        called = True

    with pytest.raises(FrozenJobRefusal):
        freeze_store.guard_build(builder)
    assert called is False


def test_unfrozen_job_builds_through_guard(tmp_path) -> None:
    freeze_store = FreezeStore(tmp_path / "manual-finalization")
    marker: list[str] = []
    freeze_store.guard_build(lambda: marker.append("built"))
    assert marker == ["built"]


def test_refreeze_appends_new_version_never_overwrites(tmp_path) -> None:
    target = sha("freeze-target")
    freeze_store = FreezeStore(tmp_path / "manual-finalization")
    first = freeze_store.publish(make_package(tmp_path, target))
    second = freeze_store.publish(
        make_package(tmp_path, target, render_seed="final-render-v2")
    )
    assert second.version == 2
    packages_dir = tmp_path / "manual-finalization" / "packages"
    v1 = (packages_dir / "freeze-v1.json").read_bytes()
    v2 = (packages_dir / "freeze-v2.json").read_bytes()
    assert v1 != v2
    assert first.package.package_sha256 in v1.decode()
    assert second.package.package_sha256 in v2.decode()
    state = freeze_store.frozen_state()
    assert state is not None
    assert state.version == 2


def test_identical_republish_is_idempotent(tmp_path) -> None:
    target = sha("freeze-target")
    freeze_store = FreezeStore(tmp_path / "manual-finalization")
    package = make_package(tmp_path, target)
    first = freeze_store.publish(package)
    again = freeze_store.publish(package)
    assert first.version == 1
    assert again.version == 1
    assert again.idempotent is True


def test_frozen_state_tamper_detected(tmp_path) -> None:
    target = sha("freeze-target")
    freeze_store = FreezeStore(tmp_path / "manual-finalization")
    freeze_store.publish(make_package(tmp_path, target))
    state_path = tmp_path / "manual-finalization" / "frozen.json"
    raw = state_path.read_bytes().replace(
        b'"automation_frozen":true', b'"automation_frozen":false'
    )
    state_path.write_bytes(raw)
    with pytest.raises(FreezeStateError):
        freeze_store.is_frozen()
