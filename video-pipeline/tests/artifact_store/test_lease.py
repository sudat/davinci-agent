from __future__ import annotations

from pathlib import Path

import pytest

from services.artifact_store.lease import LeaseAuthority, LeaseError


def test_valid_holder_commits_within_ttl(tmp_path: Path) -> None:
    authority = LeaseAuthority(tmp_path / "leases")

    record = authority.acquire("cp-baseline", "holder-a", now_unix=1_000, ttl_seconds=60)

    assert authority.commit("cp-baseline", "holder-a", now_unix=1_059) == record


def test_wrong_holder_cannot_commit_or_steal(tmp_path: Path) -> None:
    authority = LeaseAuthority(tmp_path / "leases")
    authority.acquire("cp-baseline", "holder-a", now_unix=1_000, ttl_seconds=60)

    with pytest.raises(LeaseError, match="not-holder") as commit_error:
        authority.commit("cp-baseline", "holder-b", now_unix=1_010)
    assert commit_error.value.code == "not-holder"

    with pytest.raises(LeaseError, match="lease-held") as acquire_error:
        authority.acquire("cp-baseline", "holder-b", now_unix=1_010, ttl_seconds=60)
    assert acquire_error.value.code == "lease-held"


def test_same_holder_reacquire_extends(tmp_path: Path) -> None:
    authority = LeaseAuthority(tmp_path / "leases")
    authority.acquire("cp-baseline", "holder-a", now_unix=1_000, ttl_seconds=60)

    extended = authority.acquire("cp-baseline", "holder-a", now_unix=1_030, ttl_seconds=60)

    assert extended.expires_at_unix == 1_090


def test_commit_without_lease_is_refused(tmp_path: Path) -> None:
    authority = LeaseAuthority(tmp_path / "leases")

    with pytest.raises(LeaseError, match="lease-missing"):
        authority.commit("cp-baseline", "holder-a", now_unix=1_000)


def test_lease_file_names_cannot_escape(tmp_path: Path) -> None:
    authority = LeaseAuthority(tmp_path / "leases")

    with pytest.raises(LeaseError, match="lease-name"):
        authority.acquire("../escape", "holder-a", now_unix=1_000, ttl_seconds=60)
