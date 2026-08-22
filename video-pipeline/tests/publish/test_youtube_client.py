"""Task 54: YouTube uploader + idempotency — mock-transport tests (no real API)."""

from __future__ import annotations

import datetime
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
from pydantic import ValidationError

from services.publish.idempotency import (
    DuplicateUploadKeyError,
    UploadLedger,
    UploadLedgerError,
)
from services.publish.models import build_package

if TYPE_CHECKING:
    from services.publish.models import PublishPackageV1
from services.publish.youtube_client import (
    CredentialResolutionError,
    HttpTransport,
    LiveUploadNotImplementedError,
    PublicationApprovalRequiredError,
    UploadSessionResult,
    YoutubeUploader,
    default_credential_lookup,
)


def _sha(n: int) -> str:
    return f"{n:064x}"


def _fixed_clock() -> Callable[[], datetime.datetime]:
    state = {"ticks": 0}

    def tick() -> datetime.datetime:
        state["ticks"] += 1
        return datetime.datetime(2026, 8, 22, 10, 0, state["ticks"], tzinfo=datetime.UTC)

    return tick


def _make_package(
    visibility: Literal["private", "unlisted", "public"] = "private",
    approval_ref: str | None = None,
) -> PublishPackageV1:
    return build_package(
        render_ref={"render_sha256": _sha(1), "path": "renders/ep1.mp4"},
        metadata={
            "episode_id": "ep_pub_01",
            "title_candidates": ["Episode One", "Alt Title"],
            "selected_title": "Episode One",
            "description": "An episode description.",
            "tags": ["vlog"],
            "visibility": visibility,
            "channel_target": "youtube-main",
        },
        approvals={"publication_approval_ref": approval_ref},
    )


def _credential_file(tmp_path: Path, *, content: str = '{"note": "placeholder"}') -> Path:
    cred = tmp_path / "creds" / "youtube-main.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text(content)
    return cred


class _MockTransport:
    """In-memory YoutubeTransport double — records every call, never touches network."""

    def __init__(
        self,
        *,
        fail_first_n: int = 0,
        create_then_lose_response: bool = False,
    ) -> None:
        self.upload_calls: list[dict[str, object]] = []
        self.already_calls: list[str] = []
        self.server_videos: list[str] = []
        self.keyed: dict[str, str] = {}
        self._fail_first_n = fail_first_n
        self._create_then_lose = create_then_lose_response

    def upload_session(
        self,
        media_path: Path,
        metadata: Mapping[str, object],
        visibility: str,
    ) -> UploadSessionResult:
        self.upload_calls.append(
            {
                "media_path": media_path,
                "metadata": dict(metadata),
                "visibility": visibility,
            },
        )
        key = str(metadata.get("idempotency_key", ""))
        if self._fail_first_n > 0:
            self._fail_first_n -= 1
            raise RuntimeError("mid-session transport failure")
        video_id = f"vid_{len(self.server_videos) + 1}"
        self.server_videos.append(video_id)
        self.keyed[key] = video_id
        if self._create_then_lose:
            raise ConnectionError("upload response lost after server created video")
        return UploadSessionResult(video_id=video_id)

    def already_uploaded(self, idempotency_key: str) -> str | None:
        self.already_calls.append(idempotency_key)
        return self.keyed.get(idempotency_key)


def _make_uploader(
    tmp_path: Path,
    transport: _MockTransport,
    *,
    cred_path: Path | None = None,
) -> tuple[YoutubeUploader, UploadLedger]:
    cred = cred_path if cred_path is not None else _credential_file(tmp_path)
    ledger = UploadLedger(tmp_path / "ledgers", "youtube-main")
    uploader = YoutubeUploader(
        transport,
        ledger,
        credential_lookup=lambda _alias: cred,
    )
    return uploader, ledger


def _media(tmp_path: Path) -> Path:
    media = tmp_path / "render.mp4"
    media.write_bytes(b"fake-mp4-bytes")
    return media


# --- UploadLedger (idempotency ledger) ---


def test_double_start_returns_first_record_and_appends_no_duplicate(tmp_path: Path) -> None:
    ledger = UploadLedger(tmp_path, "youtube-main", clock=_fixed_clock())

    first = ledger.start("idem-01")
    second = ledger.start("idem-01")

    assert second == first
    started = [r for r in ledger.records() if r.status == "started"]
    assert len(started) == 1
    assert ledger.lookup("idem-01") == first
    assert ledger.completed_video_id("idem-01") is None


def test_complete_rejects_second_completion_for_same_key(tmp_path: Path) -> None:
    ledger = UploadLedger(tmp_path, "youtube-main")

    ledger.start("idem-01")
    ledger.complete("idem-01", "vid_1")

    with pytest.raises(DuplicateUploadKeyError):
        ledger.complete("idem-01", "vid_1")
    completed = [r for r in ledger.records() if r.status == "completed"]
    assert len(completed) == 1
    assert completed[0].video_id == "vid_1"


def test_start_rejects_key_that_already_completed(tmp_path: Path) -> None:
    ledger = UploadLedger(tmp_path, "youtube-main")

    ledger.start("idem-01")
    ledger.complete("idem-01", "vid_1")

    with pytest.raises(DuplicateUploadKeyError):
        ledger.start("idem-01")


def test_append_only_existing_lines_stay_byte_identical(tmp_path: Path) -> None:
    ledger = UploadLedger(tmp_path, "youtube-main", clock=_fixed_clock())

    ledger.start("idem-01")
    first_line = (tmp_path / "youtube-main.jsonl").read_bytes().splitlines()[0]

    ledger.fail("idem-01", reason="ConnectionError")
    ledger.start("idem-01")
    ledger.complete("idem-01", "vid_1")

    lines = (tmp_path / "youtube-main.jsonl").read_bytes().splitlines()
    assert lines[0] == first_line
    assert len(lines) == 4


def test_records_rejects_corrupt_line(tmp_path: Path) -> None:
    ledger = UploadLedger(tmp_path, "youtube-main")
    ledger.start("idem-01")
    with (tmp_path / "youtube-main.jsonl").open("ab") as stream:
        stream.write(b"not-json\n")

    with pytest.raises(UploadLedgerError):
        ledger.records()


def test_records_rejects_wrong_status_payload(tmp_path: Path) -> None:
    ledger = UploadLedger(tmp_path, "youtube-main")
    with (tmp_path / "youtube-main.jsonl").open("ab") as stream:
        stream.write(b'{"idempotency_key": "k", "status": "bogus", "timestamp": "x"}\n')

    with pytest.raises(UploadLedgerError):
        ledger.records()


def test_ledger_rejects_path_unsafe_channel_alias(tmp_path: Path) -> None:
    with pytest.raises(UploadLedgerError):
        UploadLedger(tmp_path, "../escape")


# --- (d)/(e) publication approval gate ---


@pytest.mark.parametrize("approval", [None, "wrong-ref"])
def test_upload_refuses_public_before_any_call_when_approval_missing_or_mismatched(
    tmp_path: Path,
    approval: str | None,
) -> None:
    transport = _MockTransport()
    uploader, ledger = _make_uploader(tmp_path, transport)
    package = _make_package(visibility="public", approval_ref="pub_appr_01")

    with pytest.raises(PublicationApprovalRequiredError):
        uploader.upload(package, _media(tmp_path), publication_approval=approval)

    assert len(transport.upload_calls) == 0
    assert len(transport.already_calls) == 0
    assert ledger.records() == []


def test_upload_proceeds_when_public_approval_matches_package_ref(tmp_path: Path) -> None:
    transport = _MockTransport()
    uploader, ledger = _make_uploader(tmp_path, transport)
    package = _make_package(visibility="public", approval_ref="pub_appr_01")

    result = uploader.upload(package, _media(tmp_path), publication_approval="pub_appr_01")

    assert result.video_id == "vid_1"
    assert result.reused is False
    visibility = transport.upload_calls[0]["visibility"]
    assert visibility == "public"
    completed = [r for r in ledger.records() if r.status == "completed"]
    assert len(completed) == 1


# --- (a)/(b)/(c) idempotency and retry safety ---


def test_upload_private_happy_path_completes_ledger(tmp_path: Path) -> None:
    transport = _MockTransport()
    uploader, ledger = _make_uploader(tmp_path, transport)
    package = _make_package(visibility="private")

    result = uploader.upload(package, _media(tmp_path))

    assert result.video_id == "vid_1"
    assert result.reused is False
    assert result.idempotency_key == package.idempotency_key
    assert len(transport.upload_calls) == 1
    metadata = transport.upload_calls[0]["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["title"] == "Episode One"
    assert metadata["idempotency_key"] == package.idempotency_key
    assert transport.upload_calls[0]["visibility"] == "private"
    assert ledger.completed_video_id(package.idempotency_key) == "vid_1"


def test_upload_retry_after_mid_session_failure_reuses_key_single_completion(
    tmp_path: Path,
) -> None:
    transport = _MockTransport(fail_first_n=1)
    uploader, ledger = _make_uploader(tmp_path, transport)
    package = _make_package(visibility="private")
    media = _media(tmp_path)

    with pytest.raises(RuntimeError, match="mid-session transport failure"):
        uploader.upload(package, media)

    result = uploader.upload(package, media)

    assert result.video_id == "vid_1"
    # Exactly two transport sessions: failed attempt + retry — no more.
    assert len(transport.upload_calls) == 2
    # Both sessions carried the SAME idempotency key.
    keys = set()
    for call in transport.upload_calls:
        recorded = call["metadata"]
        assert isinstance(recorded, dict)
        keys.add(str(recorded.get("idempotency_key")))
    assert keys == {package.idempotency_key}
    completed = [r for r in ledger.records() if r.status == "completed"]
    assert len(completed) == 1
    failed = [r for r in ledger.records() if r.status == "failed"]
    assert len(failed) == 1


def test_upload_returns_existing_video_without_second_call_when_server_already_has_it(
    tmp_path: Path,
) -> None:
    transport = _MockTransport(create_then_lose_response=True)
    uploader, ledger = _make_uploader(tmp_path, transport)
    package = _make_package(visibility="private")
    media = _media(tmp_path)

    with pytest.raises(ConnectionError, match="response lost"):
        uploader.upload(package, media)

    result = uploader.upload(package, media)

    assert result.video_id == "vid_1"
    assert result.reused is True
    # Server created the video on the first (lost-response) session only.
    assert len(transport.upload_calls) == 1
    assert ledger.completed_video_id(package.idempotency_key) == "vid_1"


def test_upload_returns_ledger_completed_video_without_transport_call(
    tmp_path: Path,
) -> None:
    transport = _MockTransport()
    uploader, ledger = _make_uploader(tmp_path, transport)
    package = _make_package(visibility="private")
    ledger.start(package.idempotency_key)
    ledger.complete(package.idempotency_key, "vid_existing")

    result = uploader.upload(package, _media(tmp_path))

    assert result.video_id == "vid_existing"
    assert result.reused is True
    assert len(transport.upload_calls) == 0
    assert len(transport.already_calls) == 0


# --- (f) credential handling / token-leak guards ---


def test_upload_refuses_missing_credential_before_transport_call(tmp_path: Path) -> None:
    transport = _MockTransport()
    missing = tmp_path / "creds" / "youtube-main.json"
    ledger = UploadLedger(tmp_path / "ledgers", "youtube-main")
    uploader = YoutubeUploader(transport, ledger, credential_lookup=lambda _a: missing)
    package = _make_package(visibility="private")

    with pytest.raises(CredentialResolutionError):
        uploader.upload(package, _media(tmp_path))

    assert len(transport.upload_calls) == 0
    assert ledger.records() == []


def test_upload_outputs_contain_no_token_material(tmp_path: Path) -> None:
    canary = "ya29.a0AfH6SM_FAKE_CANARY_Eyj"
    cred = _credential_file(tmp_path, content=f'{{"access_token": "{canary}"}}')
    transport = _MockTransport()
    uploader, _ledger = _make_uploader(tmp_path, transport, cred_path=cred)
    package = _make_package(visibility="private")

    result = uploader.upload(package, _media(tmp_path))

    ledger_text = (tmp_path / "ledgers" / "youtube-main.jsonl").read_text()
    assert canary not in ledger_text
    assert canary not in result.model_dump_json()
    assert str(cred) not in ledger_text
    for call in transport.upload_calls:
        assert canary not in str(call)
        assert str(cred) not in str(call)
    for marker in ("ya29.", "Bearer", "AIza", "eyJ"):
        assert marker not in ledger_text


def test_upload_rejects_package_whose_alias_is_token_like(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        build_package(
            render_ref={"render_sha256": _sha(2), "path": "renders/ep2.mp4"},
            metadata={"channel_target": "ya29.SECRET"},
        )


# --- HttpTransport placeholder seam ---


def test_http_transport_placeholder_raises_typed_error_without_network() -> None:
    transport = HttpTransport()

    with pytest.raises(LiveUploadNotImplementedError):
        transport.upload_session(Path("m.mp4"), {}, "private")
    with pytest.raises(LiveUploadNotImplementedError):
        transport.already_uploaded("idem-01")


def test_default_credential_lookup_maps_alias_to_config_path() -> None:
    path = default_credential_lookup("youtube-main")

    assert path.name == "youtube-main.json"
    assert path.parent.name == "credentials"
    assert HttpTransport().credential_path_for("youtube-main") == path


def test_http_transport_credential_lookup_is_injectable(tmp_path: Path) -> None:
    cred = tmp_path / "custom.json"
    transport = HttpTransport(credential_lookup=lambda _alias: cred)

    assert transport.credential_path_for("any-alias") == cred
