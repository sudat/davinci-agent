from __future__ import annotations

from fractions import Fraction

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from services.contracts import (
    EVIDENCE_0A_ADAPTER,
    ArtifactEnvelope,
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    canonical_json_bytes,
)
from services.contracts.serialization import CanonicalJsonFloatError

SHA256 = "a" * 64


class FloatPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    value: float


def test_envelope_round_trip_when_json_is_valid() -> None:
    raw = (
        '{"artifact_id":"art_01JTEST","artifact_type":"timeline_ir_0a",'
        '"schema_version":"0a.1","content_hash":"'
        + SHA256
        + '","producer":{"name":"timeline-compiler","version":"git:abc123"},'
        '"inputs":[{"artifact_id":"art_source","sha256":"'
        + SHA256
        + '"}]}'
    )

    envelope = ArtifactEnvelope.model_validate_json(raw)
    reparsed = ArtifactEnvelope.model_validate_json(canonical_json_bytes(envelope))

    assert reparsed == envelope


def test_rational_rate_arithmetic_when_ntsc_rate_is_used() -> None:
    rate = RationalFrameRate(num=30000, den=1001)

    duration = rate.duration_for(30000)

    assert duration == Fraction(1001, 1)


def test_rational_rate_equality_when_decimal_approximation_differs() -> None:
    ntsc = RationalFrameRate(num=30000, den=1001)
    decimal_approximation = RationalFrameRate(num=2997, den=100)

    assert ntsc.as_fraction != decimal_approximation.as_fraction


def test_half_open_length_when_span_is_valid() -> None:
    source_span = SourceFrameSpan(
        start_frame=12,
        end_frame=42,
        rate=RationalFrameRate(num=30, den=1),
    )
    record_span = RecordFrameSpan(start_frame=100, end_frame=130)

    assert source_span.length == 30
    assert record_span.length == 30


def test_discriminated_parse_when_timeline_ir_tag_is_known() -> None:
    raw = (
        '{"artifact_id":"art_timeline","artifact_type":"timeline_ir_0a",'
        '"schema_version":"0a.1","content_hash":"'
        + SHA256
        + '","producer":{"name":"compiler","version":"git:abc"},"inputs":[],'
        '"rate":{"num":30,"den":1},"tracks":[{"track":{"kind":"video",'
        '"index":1},"items":[{"item_id":"item_1","kind":"video",'
        '"source":{"source_id":"src_1","span":{"start_frame":0,'
        '"end_frame":30,"rate":{"num":30,"den":1}}},"record_span":'
        '{"start_frame":0,"end_frame":30},"av_link_id":"link_1"}]}]}'
    )

    evidence = EVIDENCE_0A_ADAPTER.validate_json(raw)

    assert evidence.artifact_type == "timeline_ir_0a"


def test_discriminated_parse_when_build_report_tag_is_known() -> None:
    raw = (
        '{"artifact_id":"art_report","artifact_type":"build_report_0a",'
        '"schema_version":"0a.1","content_hash":"'
        + SHA256
        + '","producer":{"name":"builder","version":"git:abc"},"inputs":[],'
        '"items":[],"timeline_fingerprint":"'
        + SHA256
        + '","output_hash":"'
        + SHA256
        + '","warnings":[],"failures":[]}'
    )

    evidence = EVIDENCE_0A_ADAPTER.validate_json(raw)

    assert evidence.artifact_type == "build_report_0a"


def test_extra_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RecordFrameSpan.model_validate(
            {"start_frame": 0, "end_frame": 1, "unexpected": True}
        )


@pytest.mark.parametrize("value", [12.0, "12.0"])
def test_float_frame_is_rejected_when_input_is_python(value: float | str) -> None:
    with pytest.raises(ValidationError):
        RecordFrameSpan.model_validate({"start_frame": value, "end_frame": 13})


def test_float_frame_is_rejected_when_input_is_json() -> None:
    with pytest.raises(ValidationError):
        RecordFrameSpan.model_validate_json('{"start_frame":12.0,"end_frame":13}')


def test_negative_span_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RecordFrameSpan(start_frame=-1, end_frame=4)


def test_inverted_span_is_rejected() -> None:
    with pytest.raises(ValidationError, match="end_frame"):
        RecordFrameSpan(start_frame=10, end_frame=9)


@pytest.mark.parametrize(
    "malformed",
    ["a" * 63, "A" * 64, "g" * 64],
    ids=["wrong-length", "uppercase", "nonhex"],
)
def test_malformed_sha_is_rejected(malformed: str) -> None:
    with pytest.raises(ValidationError):
        ArtifactEnvelope.model_validate(
            {
                "artifact_id": "art_1",
                "artifact_type": "timeline_ir_0a",
                "schema_version": "0a.1",
                "content_hash": malformed,
                "producer": {"name": "compiler", "version": "git:abc"},
                "inputs": [],
            }
        )


@pytest.mark.parametrize("artifact_id", ["", "bad id", "bad/id"])
def test_identifier_is_rejected_when_empty_or_charset_is_unsupported(
    artifact_id: str,
) -> None:
    with pytest.raises(ValidationError):
        ArtifactEnvelope.model_validate(
            {
                "artifact_id": artifact_id,
                "artifact_type": "timeline_ir_0a",
                "schema_version": "0a.1",
                "content_hash": SHA256,
                "producer": {"name": "compiler", "version": "git:abc"},
                "inputs": [],
            }
        )


@pytest.mark.parametrize(
    ("num", "den"),
    [(0, 1), (30, 0), (30.0, 1), (30, 1.0)],
)
def test_rational_rate_is_rejected_when_not_strictly_positive_integers(
    num: float,
    den: float,
) -> None:
    with pytest.raises(ValidationError):
        RationalFrameRate.model_validate({"num": num, "den": den})


def test_unknown_discriminator_is_rejected() -> None:
    raw = (
        '{"artifact_id":"art_unknown","artifact_type":"future_artifact",'
        '"schema_version":"0a.1","content_hash":"'
        + SHA256
        + '","producer":{"name":"future","version":"1"},"inputs":[]}'
    )

    with pytest.raises(ValidationError, match="union_tag_invalid"):
        EVIDENCE_0A_ADAPTER.validate_json(raw)


def test_canonical_json_rejects_float_values() -> None:
    payload = FloatPayload(value=1.25)

    with pytest.raises(CanonicalJsonFloatError):
        canonical_json_bytes(payload)
