"""Model validation: policy disjointness, job-state gating, registry seal."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.retention.models import (
    JobRetentionState,
    RegisteredPath,
    RetentionPolicy,
    RetentionRegistry,
)
from tests.retention.support import policy_payload, sha


def test_policy_accepts_canonical_payload() -> None:
    policy = RetentionPolicy.model_validate(policy_payload())
    assert policy.rebuildable_retention_days == 30
    assert "proxies" in policy.rebuildable_names


def test_policy_rejects_name_in_two_classes() -> None:
    payload = policy_payload(rebuildable_names=("proxies", "sources"))
    with pytest.raises(ValidationError, match="class_overlap"):
        RetentionPolicy.model_validate(payload)


def test_policy_rejects_path_separator_in_name() -> None:
    payload = policy_payload(rebuildable_names=("a/b",))
    with pytest.raises(ValidationError, match="name_shape"):
        RetentionPolicy.model_validate(payload)


def test_policy_rejects_dot_and_dotdot_names() -> None:
    with pytest.raises(ValidationError, match="name_shape"):
        RetentionPolicy.model_validate(policy_payload(runtime_cache_names=(".",)))
    with pytest.raises(ValidationError, match="name_shape"):
        RetentionPolicy.model_validate(policy_payload(runtime_cache_names=("..",)))


def test_policy_rejects_duplicate_names_within_a_class() -> None:
    payload = policy_payload(rebuildable_names=("proxies", "proxies"))
    with pytest.raises(ValidationError, match="duplicate_name"):
        RetentionPolicy.model_validate(payload)


def test_policy_rejects_zero_retention_days() -> None:
    payload = policy_payload(rebuildable_retention_days=0)
    with pytest.raises(ValidationError):
        RetentionPolicy.model_validate(payload)


def test_frozen_state_requires_frozen_at() -> None:
    payload = {
        "schema_version": "retention-job-state-v1",
        "job_id": "job-1",
        "status": "FROZEN",
        "frozen_at_epoch_s": None,
    }
    with pytest.raises(ValidationError, match="frozen_at"):
        JobRetentionState.model_validate(payload)


def test_active_state_forbids_frozen_at() -> None:
    payload = {
        "schema_version": "retention-job-state-v1",
        "job_id": "job-1",
        "status": "ACTIVE",
        "frozen_at_epoch_s": 123,
    }
    with pytest.raises(ValidationError, match="frozen_at"):
        JobRetentionState.model_validate(payload)


def test_registry_mint_and_roundtrip() -> None:
    registry = RetentionRegistry.mint(
        {"proxies/p1.mp4": RegisteredPath(sha256=sha("p1"))}
    )
    parsed = RetentionRegistry.model_validate_json(registry.model_dump_json())
    assert parsed.entries["proxies/p1.mp4"].sha256 == sha("p1")


def test_registry_rejects_absolute_paths() -> None:
    entries = {"/etc/passwd": RegisteredPath(sha256=sha("x"))}
    with pytest.raises(ValidationError, match="path_shape"):
        RetentionRegistry.mint(entries)


def test_registry_rejects_parent_traversal_paths() -> None:
    entries = {"proxies/../sources/a": RegisteredPath(sha256=sha("x"))}
    with pytest.raises(ValidationError, match="path_shape"):
        RetentionRegistry.mint(entries)


def test_registry_seal_detects_tampered_entry() -> None:
    registry = RetentionRegistry.mint(
        {"proxies/p1.mp4": RegisteredPath(sha256=sha("p1"))}
    )
    tampered = registry.model_dump()
    tampered["entries"]["proxies/p1.mp4"]["sha256"] = sha("forged")
    with pytest.raises(ValidationError, match="registry_seal"):
        RetentionRegistry.model_validate(tampered)
