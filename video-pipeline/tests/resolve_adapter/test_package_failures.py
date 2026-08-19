"""Todo-47 fail-closed probes: every unsupported input is a typed refusal.

Missing matrix evidence, unsupported retime, implied transitions, wrong track
map, stale IR hashes, Resolve fields inside the IR, media binding drift, and
the stale-capability fault fixture each compile to a typed
PackageCompileError — never to a silently-degraded package.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.contracts.primitives import RationalFrameRate, RecordFrameSpan
from services.contracts.timeline_ir import (
    SubtitleCueItem,
    TimelineGapItem,
    TimelineIrProduction,
    TimelineItem0C,
    TimelineTrackProduction,
    TrackRef0C,
)
from services.foundation_io import canonical_model_bytes
from services.resolve_adapter.errors import (
    MEDIA_HASH_DRIFT,
    STALE_IR,
    UNSUPPORTED_RETIME,
    UNSUPPORTED_TRANSITION,
    WRONG_TRACK_MAP,
    PackageCompileError,
)
from services.resolve_adapter.models import MediaBinding
from services.resolve_adapter.package import PackageCompileRequest, compile_resolve_package
from tests.resolve_adapter.support import (
    declared_media,
    ir_for,
    load_p2_manifest,
    lock_sha256,
    phase2_lock,
)


def _request(fixture_id: str, **overrides: object) -> PackageCompileRequest:
    manifest = load_p2_manifest(fixture_id)
    base: dict[str, object] = {
        "ir": ir_for(manifest),
        "lock": phase2_lock(),
        "lock_sha256": lock_sha256(),
        "declared_media": declared_media(manifest),
        "artifact_id": f"resolve-package-{fixture_id}",
    }
    base.update(overrides)
    return PackageCompileRequest(**base)  # type: ignore[arg-type]


def test_stale_capability_matrix_fault_is_a_typed_refusal() -> None:
    manifest = load_p2_manifest("p2-stale-capability")
    assert manifest.fault.kind == "stale-capability"
    stale_lock = phase2_lock().model_copy(
        update={
            "resolve_package": phase2_lock().resolve_package.model_copy(
                update={"capability_matrix_sha256": manifest.fault.declared_matrix_sha256}
            )
        }
    )
    with pytest.raises(PackageCompileError) as raised:
        compile_resolve_package(_request("p2-stale-capability", lock=stale_lock))
    assert raised.value.code == "capability-matrix-stale"


def test_missing_matrix_evidence_is_a_typed_refusal(tmp_path: Path) -> None:
    matrix = json.loads(
        Path("capabilities/resolve-21.0.4/capability-matrix.json").read_bytes()
    )
    for row in matrix["capabilities"]:
        if row["capability"] == "base_cut":
            row["evidence_refs"] = []
    broken = tmp_path / "broken-matrix.json"
    broken.write_bytes(
        json.dumps(matrix, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    )
    section = phase2_lock().resolve_package.model_copy(
        update={
            "capability_matrix_path": str(broken),
            "capability_matrix_sha256": hashlib.sha256(broken.read_bytes()).hexdigest(),
        }
    )
    with pytest.raises(PackageCompileError) as raised:
        compile_resolve_package(
            _request(
                "p2-stale-capability",
                lock=phase2_lock().model_copy(update={"resolve_package": section}),
            )
        )
    assert raised.value.code == "matrix-evidence-missing"


def test_same_duration_wrong_media_fault_is_a_typed_refusal() -> None:
    manifest = load_p2_manifest("p2-same-duration-wrong-media")
    assert manifest.fault.kind == "same-duration-wrong-media"
    declared = declared_media(manifest)[0]
    presented = declared.model_copy(
        update={
            "sha256": manifest.fault.presented_media_sha256,
            "duration_frames": manifest.fault.presented_duration_frames,
        }
    )
    with pytest.raises(PackageCompileError) as raised:
        compile_resolve_package(
            _request("p2-same-duration-wrong-media", presented_media=(presented,))
        )
    assert raised.value.code == MEDIA_HASH_DRIFT


def test_unsupported_retime_is_a_typed_refusal() -> None:
    manifest = load_p2_manifest("p2-stale-capability")
    ir = ir_for(manifest)
    retimed_tracks: list[TimelineTrackProduction] = []
    for track in ir.tracks:
        new_items: list[TimelineItem0C | SubtitleCueItem | TimelineGapItem] = []
        for item in track.items:
            if isinstance(item, TimelineItem0C | SubtitleCueItem):
                new_items.append(
                    item.model_copy(
                        update={
                            "source": item.source.model_copy(
                                update={
                                    "span": item.source.span.model_copy(
                                        update={"rate": RationalFrameRate(num=24000, den=1000)}
                                    )
                                }
                            )
                        }
                    )
                )
            else:
                new_items.append(item)
        retimed_tracks.append(track.model_copy(update={"items": tuple(new_items)}))
    digest = hashlib.sha256()
    digest.update(canonical_model_bytes(ir.rate))
    for track in retimed_tracks:
        digest.update(canonical_model_bytes(track))
    retimed = ir.model_copy(
        update={"tracks": tuple(retimed_tracks), "content_hash": digest.hexdigest()}
    )
    with pytest.raises(PackageCompileError) as raised:
        compile_resolve_package(_request("p2-stale-capability", ir=retimed))
    assert raised.value.code == UNSUPPORTED_RETIME


def test_implied_transition_is_a_typed_refusal() -> None:
    manifest = load_p2_manifest("p2-stale-capability")
    ir = ir_for(manifest)
    squeezed_tracks: list[TimelineTrackProduction] = []
    for track in ir.tracks:
        new_items = []
        for item in track.items:
            if isinstance(item, TimelineItem0C) and track.track.kind == "video":
                new_items.append(
                    item.model_copy(
                        update={
                            "record_span": RecordFrameSpan(
                                start_frame=item.record_span.start_frame,
                                end_frame=item.record_span.end_frame - 5,
                            )
                        }
                    )
                )
            else:
                new_items.append(item)
        squeezed_tracks.append(track.model_copy(update={"items": tuple(new_items)}))
    digest = hashlib.sha256()
    digest.update(canonical_model_bytes(ir.rate))
    for track in squeezed_tracks:
        digest.update(canonical_model_bytes(track))
    squeezed = ir.model_copy(
        update={"tracks": tuple(squeezed_tracks), "content_hash": digest.hexdigest()}
    )
    with pytest.raises(PackageCompileError) as raised:
        compile_resolve_package(_request("p2-stale-capability", ir=squeezed))
    assert raised.value.code == UNSUPPORTED_TRANSITION


def test_wrong_track_map_is_a_typed_refusal() -> None:
    manifest = load_p2_manifest("p2-stale-capability")
    ir = ir_for(manifest)
    remapped_tracks: list[TimelineTrackProduction] = []
    for track in ir.tracks:
        new_index = 7 if track.track.kind == "audio" else track.track.index
        remapped_tracks.append(
            track.model_copy(
                update={"track": TrackRef0C(kind=track.track.kind, index=new_index)}
            )
        )
    digest = hashlib.sha256()
    digest.update(canonical_model_bytes(ir.rate))
    for track in remapped_tracks:
        digest.update(canonical_model_bytes(track))
    remapped = ir.model_copy(
        update={"tracks": tuple(remapped_tracks), "content_hash": digest.hexdigest()}
    )
    with pytest.raises(PackageCompileError) as raised:
        compile_resolve_package(_request("p2-stale-capability", ir=remapped))
    assert raised.value.code == WRONG_TRACK_MAP


def test_stale_ir_content_hash_is_a_typed_refusal() -> None:
    manifest = load_p2_manifest("p2-stale-capability")
    ir = ir_for(manifest)
    stale = ir.model_copy(update={"content_hash": "1" * 64})
    with pytest.raises(PackageCompileError) as raised:
        compile_resolve_package(_request("p2-stale-capability", ir=stale))
    assert raised.value.code == STALE_IR


def test_resolve_field_injected_into_ir_is_rejected_at_the_model_level() -> None:
    payload = json.loads(canonical_model_bytes(ir_for(load_p2_manifest("p2-stale-capability"))))
    payload["resolve_track_index"] = 3
    with pytest.raises(ValidationError, match="resolve_field_forbidden"):
        TimelineIrProduction.model_validate(payload)


def test_missing_media_binding_is_a_typed_refusal() -> None:
    other = MediaBinding(
        source_id="some-other-source",
        path="jobs/x/sources/edit-source.mov",
        sha256="0" * 64,
        duration_frames=600,
    )
    with pytest.raises(PackageCompileError) as raised:
        compile_resolve_package(_request("p2-stale-capability", declared_media=(other,)))
    assert raised.value.code == "media-binding-missing"


def test_media_extent_overflow_is_a_typed_refusal() -> None:
    manifest = load_p2_manifest("p2-stale-capability")
    short = declared_media(manifest)[0].model_copy(update={"duration_frames": 100})
    with pytest.raises(PackageCompileError) as raised:
        compile_resolve_package(_request("p2-stale-capability", declared_media=(short,)))
    assert raised.value.code == "media-extent-overflow"
