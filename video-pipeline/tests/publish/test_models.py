"""Task 53: PublishPackageV1 — credential-alias only artifact tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.publish.models import (
    CredentialLeakError,
    PublishPackageV1,
    RenderRef,
    build_package,
)


def _sha(n: int) -> str:
    # deterministic 64-hex sha: 64 chars, lower hex
    return f"{n:064x}"


def _render(n: int = 1) -> dict[str, object]:
    return {"render_sha256": _sha(n), "path": f"renders/ep{n}.mp4"}


def _full_payload(
    channel: str = "youtube-main",
    visibility: str = "private",
) -> dict[str, object]:
    return {
        "schema_version": "publish-package-v1",
        "episode_id": "ep_01JVLD",
        "render_ref": _render(1),
        "title_candidates": ["Title A", "Title B"],
        "selected_title": "Title A",
        "description": "A description for the episode.",
        "chapters": [
            {"start_seconds": 0, "title": "Intro"},
            {"start_seconds": 60.5, "title": "Main"},
        ],
        "tags": ["tag1", "tag2"],
        "thumbnail_ref": {"asset_id": "thumb_01", "source": "auto"},
        "playlist_target": "PL123",
        "visibility": visibility,
        "schedule_time": "2026-08-22T10:00:00+09:00",
        "approval_refs": ["appr_01"],
        "publication_approval_ref": "pub_01" if visibility == "public" else None,
        "channel_target": channel,
        "idempotency_key": "550e8400-e29b-41d4-a716-446655440000",
        "remote_video_id": None,
    }


# (a) full package round-trip
def test_full_package_round_trip() -> None:
    raw = _full_payload()

    pkg = PublishPackageV1.model_validate(raw)

    assert pkg.schema_version == "publish-package-v1"
    assert pkg.episode_id == "ep_01JVLD"
    assert pkg.render_ref.render_sha256 == _sha(1)
    assert pkg.title_candidates == ["Title A", "Title B"]
    dumped = pkg.model_dump()
    reparsed = PublishPackageV1.model_validate(dumped)
    assert reparsed == pkg
    # json round-trip
    reparsed2 = PublishPackageV1.model_validate_json(pkg.model_dump_json())
    assert reparsed2 == pkg


# (b) visibility public without approval ref -> ValidationError
def test_public_without_approval_rejected() -> None:
    raw = _full_payload(visibility="public")
    raw["publication_approval_ref"] = None

    with pytest.raises(ValidationError) as exc:
        PublishPackageV1.model_validate(raw)

    msg = str(exc.value).lower()
    assert "publication_approval" in msg or "public" in msg


def test_public_with_approval_ok() -> None:
    raw = _full_payload(visibility="public")
    raw["publication_approval_ref"] = "pub_ok"

    pkg = PublishPackageV1.model_validate(raw)

    assert pkg.visibility == "public"
    assert pkg.publication_approval_ref == "pub_ok"


# (c) channel_target containing token-looking string -> CredentialLeakError (parametrize)
@pytest.mark.parametrize(
    "token",
    [
        "ya29.a0AfH6SMC_fake_token_value_for_test_purposes_only",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.fake_payload_for_test",
        "x" * 100,
        "Bearer fake_bearer_token_for_test_purposes_only_123456",
        "AIzaSyFakeApiKeyForTestPurposesOnly_1234567890",
    ],
)
def test_channel_target_token_rejected(token: str) -> None:
    raw = _full_payload(channel=token)

    # Model must not accept a token-like alias — either a typed
    # CredentialLeakError or a ValidationError with credential_leak code.
    with pytest.raises((CredentialLeakError, ValidationError)) as exc:
        PublishPackageV1.model_validate(raw)

    # If wrapped as ValidationError, assert the credential_leak signal
    if isinstance(exc.value, ValidationError):
        errors = exc.value.errors()
        text = str(exc.value).lower()
        has_leak = any(e.get("type") == "credential_leak" for e in errors)
        assert has_leak or "credential" in text or "token" in text


def test_credential_leak_helper_direct() -> None:
    # Direct alias validator must raise typed CredentialLeakError
    from services.publish.models import _reject_token_strings  # noqa: PLC0415

    with pytest.raises(CredentialLeakError):
        _reject_token_strings("ya29.fake_for_test")


def test_deterministic_idempotency() -> None:
    r1 = RenderRef.model_validate(_render(10))
    r2 = RenderRef.model_validate(_render(20))
    meta = {"episode_id": "ep_A", "title_candidates": ["T"], "selected_title": "T"}

    pkg_a = build_package(r1, meta)
    pkg_b = build_package(r1, meta)
    pkg_c = build_package(r2, meta)

    assert pkg_a.idempotency_key == pkg_b.idempotency_key
    assert pkg_a.idempotency_key != pkg_c.idempotency_key
    assert "-" in pkg_a.idempotency_key
    assert len(pkg_a.idempotency_key) >= 32


def test_build_package_uses_derived_key_when_not_overridden() -> None:
    r = RenderRef.model_validate(_render(99))
    pkg1 = build_package(r, {"title_candidates": ["T"], "selected_title": "T"})
    pkg2 = build_package(r, {"title_candidates": ["Other"], "selected_title": "Other"})

    assert pkg1.idempotency_key == pkg2.idempotency_key


# (e) bad render sha -> rejected
def test_bad_render_sha_rejected() -> None:
    bad = {"render_sha256": "not-a-sha", "path": "renders/bad.mp4"}
    raw = _full_payload()
    raw["render_ref"] = bad

    with pytest.raises(ValidationError):
        PublishPackageV1.model_validate(raw)

    # also via RenderRef directly
    with pytest.raises(ValidationError):
        RenderRef.model_validate(bad)


def test_bad_render_sha_uppercase_rejected() -> None:
    # Sha256 pattern requires lower-case hex
    bad_upper = {"render_sha256": "A" * 64, "path": "renders/bad.mp4"}

    with pytest.raises(ValidationError):
        RenderRef.model_validate(bad_upper)


# (f) sparse-ish package (private, minimal fields) OK
def test_sparse_private_minimal_ok() -> None:
    raw: dict[str, object] = {
        "schema_version": "publish-package-v1",
        "episode_id": "ep_sparse",
        "render_ref": _render(2),
        "title_candidates": ["Only Title"],
        "selected_title": "Only Title",
        "channel_target": "youtube-main",
        "idempotency_key": "550e8400-e29b-41d4-a716-446655440001",
        "visibility": "private",
    }

    pkg = PublishPackageV1.model_validate(raw)

    assert pkg.visibility == "private"
    assert pkg.description == ""
    assert pkg.chapters == []
    assert pkg.tags == []
    assert pkg.thumbnail_ref is None
    assert pkg.playlist_target is None
    assert pkg.schedule_time is None
    assert pkg.publication_approval_ref is None
    assert pkg.remote_video_id is None


def test_sparse_via_builder_ok() -> None:
    r = RenderRef.model_validate(_render(3))
    pkg = build_package(
        r,
        {"episode_id": "ep_sparse2", "title_candidates": ["T"], "selected_title": "T"},
    )

    assert pkg.visibility == "private"
    assert pkg.episode_id == "ep_sparse2"
    # round-trip
    reparsed = PublishPackageV1.model_validate(pkg.model_dump())
    assert reparsed == pkg


def test_schedule_time_invalid_rejected() -> None:
    raw = _full_payload()
    raw["schedule_time"] = "not-a-time"

    with pytest.raises(ValidationError):
        PublishPackageV1.model_validate(raw)


def test_schedule_time_valid_variants() -> None:
    for iso in [
        "2026-08-22T10:00:00+09:00",
        "2026-08-22T10:00:00Z",
        "2026-08-22T10:00:00",
    ]:
        raw = _full_payload()
        raw["schedule_time"] = iso

        pkg = PublishPackageV1.model_validate(raw)

        assert pkg.schedule_time == iso
