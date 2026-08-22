"""TDD for reference ingestion paths — task 25."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from services.contracts.primitives import Producer
from services.reference_learning.ingest import (
    LocalReferenceUnavailable,
    ReferenceIngestError,
    ingest_local_reference,
)
from services.reference_learning.models import ReferenceLibraryV1, ReferenceSourceV1
from services.reference_learning.youtube_reference import (
    YoutubeReferenceUnavailable,
    ingest_youtube_reference,
)

PRODUCER = Producer(name="test-producer", version="v1")


def _library() -> ReferenceLibraryV1:
    return ReferenceLibraryV1(
        library_id="lib-ingest-0001",
        version=1,
        sources=(),
        annotation_ids=(),
        provenance=PRODUCER,
        created_at="2026-08-22T00:00:00Z",
    )


def test_ingest_local_reference_registers_with_correct_hash_and_increments_version(
    tmp_path: Path,
) -> None:
    fake = tmp_path / "clip.mp4"
    payload = b"\x00\x00\x00\x18ftypmp42 fake content for test"
    fake.write_bytes(payload)
    expected_sha = hashlib.sha256(payload).hexdigest()
    expected_size = len(payload)

    lib = _library()
    source, new_lib = ingest_local_reference(str(fake), library=lib)

    assert isinstance(source, ReferenceSourceV1)
    assert source.sha256 == expected_sha
    assert source.location == str(fake)
    assert source.kind == "local_file"
    assert Path(source.location).stat().st_size == expected_size
    assert new_lib.version == lib.version + 1
    assert len(new_lib.sources) == 1
    assert new_lib.sources[0] == source
    assert lib.version == 1
    assert len(lib.sources) == 0


def test_ingest_local_reference_any_extension_accepted(tmp_path: Path) -> None:
    weird = tmp_path / "weird.xyz"
    weird.write_bytes(b"not a video but still accepted")
    lib = _library()
    source, new_lib = ingest_local_reference(str(weird), library=lib)
    assert isinstance(source, ReferenceSourceV1)
    assert new_lib.version == 2


def test_ingest_local_reference_missing_file_raises_typed_error(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.mp4"
    lib = _library()
    with pytest.raises(LocalReferenceUnavailable):
        ingest_local_reference(str(missing), library=lib)
    assert lib.version == 1
    assert len(lib.sources) == 0


def test_ingest_local_reference_missing_file_not_generic_exception(tmp_path: Path) -> None:
    missing = tmp_path / "missing2.mp4"
    lib = _library()
    with pytest.raises(ReferenceIngestError):
        ingest_local_reference(str(missing), library=lib)


def test_ingest_youtube_no_compliant_path_raises_with_local_file_message() -> None:
    lib = _library()
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    with pytest.raises(YoutubeReferenceUnavailable) as exc:
        ingest_youtube_reference(url, library=lib)

    msg = str(exc.value)
    assert (
        "ローカルファイルを提供してください" in msg or "please provide a local file" in msg
    ), f"message must request local file, got: {msg}"
    assert "local" in msg.lower() or "ローカル" in msg


def test_ingest_youtube_malformed_url_also_raises_typed_with_message() -> None:
    lib = _library()
    bad_url = "not-a-url"
    with pytest.raises(YoutubeReferenceUnavailable) as exc:
        ingest_youtube_reference(bad_url, library=lib)
    msg = str(exc.value)
    assert "ローカルファイルを提供してください" in msg or "please provide a local file" in msg


def test_ingest_youtube_unavailable_is_typed_reference_error() -> None:
    assert issubclass(YoutubeReferenceUnavailable, ReferenceIngestError)

    lib = _library()
    with pytest.raises(ReferenceIngestError):
        ingest_youtube_reference("https://www.youtube.com/watch?v=abc123", library=lib)


def test_library_round_trip_after_ingest(tmp_path: Path) -> None:
    fake = tmp_path / "roundtrip.mp4"
    fake.write_bytes(b"roundtrip payload")
    lib = _library()
    source, new_lib = ingest_local_reference(str(fake), library=lib)

    raw = new_lib.model_dump(mode="json")
    parsed = ReferenceLibraryV1.model_validate(raw)
    assert parsed.model_dump(mode="json") == new_lib.model_dump(mode="json")
    assert len(parsed.sources) == 1
    assert parsed.sources[0].sha256 == source.sha256
    assert parsed.version == 2
    assert lib.version == 1


def test_library_reload_stale_state_unchanged(tmp_path: Path) -> None:
    fake = tmp_path / "stale.mp4"
    fake.write_bytes(b"stale test")
    lib = _library()
    lib_copy = ReferenceLibraryV1.model_validate(lib.model_dump(mode="json"))

    _, new_lib = ingest_local_reference(str(fake), library=lib)

    assert lib_copy.version == 1
    assert len(lib_copy.sources) == 0
    assert new_lib.version == 2

