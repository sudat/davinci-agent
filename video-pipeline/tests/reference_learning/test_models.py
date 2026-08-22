"""TDD for reference learning schemas — task 24.

Covers:
(a) round-trip for all five artifacts
(b) "I like only the color" -> color=like, others unspecified/absent + unnamed leak rejected
(c) polarity given with no domain named -> rejected
(d) DerivedTasteProfile entry with empty evidence_refs -> rejected
(e) contradiction record survives round-trip (not averaged)
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.contracts.primitives import Producer
from services.reference_learning.models import (
    ContradictionRecordV1,
    DerivedTasteEntryV1,
    DerivedTasteProfileV1,
    PairwisePreferenceV1,
    PreferenceDomain,
    ReferenceAnnotationV1,
    ReferenceLibraryV1,
    ReferenceSourceV1,
    ReferenceTimeRangeV1,
)

PRODUCER = Producer(name="test-producer", version="v1")
PRODUCER_B = Producer(name="test-producer-b", version="v1")


def _producer() -> Producer:
    return PRODUCER


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_source(
    *,
    source_id: str = "ref-0001",
    kind: str = "local_file",
) -> ReferenceSourceV1:
    return ReferenceSourceV1(
        source_id=source_id,
        kind=kind,  # type: ignore[arg-type]
        location="assets/video.mp4" if kind == "local_file" else "https://example.com/v",
        created_at="2026-08-22T00:00:00Z",
        provenance=_producer(),
        sha256="0" * 64 if kind == "local_file" else None,
    )


def make_timestamp_source() -> ReferenceSourceV1:
    return ReferenceSourceV1(
        source_id="ref-ts-0001",
        kind="timestamp_range",
        location="parent-ref-0001",
        created_at="2026-08-22T00:00:00Z",
        provenance=_producer(),
        parent_source_id="ref-0001",
        time_range=ReferenceTimeRangeV1(start_ms=1000, end_ms=4000),
    )


def make_annotation_color_only() -> ReferenceAnnotationV1:
    return ReferenceAnnotationV1(
        annotation_id="ann-0001",
        source_id="ref-0001",
        named_domains=(PreferenceDomain.color,),
        domains={PreferenceDomain.color: "like"},
        rationale="I like the color treatment in this video.",
        extracted_features={PreferenceDomain.color: {"saturation": "high"}},
        confidence=0.9,
        scope="channel",
        provenance=_producer(),
        human_approval="approved",
    )


def make_pairwise() -> PairwisePreferenceV1:
    return PairwisePreferenceV1(
        pairwise_id="pair-0001",
        domain=PreferenceDomain.pacing,
        choice="b",
        reference_a_id="ref-0001",
        reference_b_id="ref-0002",
        reason="A feels too busy",
        provenance=_producer(),
        created_at="2026-08-22T00:00:00Z",
    )


def make_derived_profile() -> DerivedTasteProfileV1:
    entry = DerivedTasteEntryV1(
        domain=PreferenceDomain.color,
        statement="warm color preferred",
        polarity="like",
        confidence=0.8,
        evidence_refs=("ann-0001",),
        source_kind="reference_annotation",
    )
    contradiction = ContradictionRecordV1(
        domain=PreferenceDomain.pacing,
        description="conflict: high density liked but busy cuts disliked",
        conflicting_refs=("ann-0001", "ann-0002"),
        note="needs human resolution",
    )
    return DerivedTasteProfileV1(
        profile_id="prof-0001",
        entries=(entry,),
        unresolved_contradictions=(contradiction,),
        provenance=_producer(),
        created_at="2026-08-22T00:00:00Z",
    )


def make_library() -> ReferenceLibraryV1:
    return ReferenceLibraryV1(
        library_id="lib-0001",
        version=1,
        sources=(make_source(),),
        annotation_ids=("ann-0001",),
        provenance=_producer(),
        created_at="2026-08-22T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# (a) round-trip serialize/parse for all five
# ---------------------------------------------------------------------------


def test_reference_source_round_trip() -> None:
    src = make_source()
    raw = src.model_dump(mode="json")
    parsed = ReferenceSourceV1.model_validate(raw)
    assert parsed == src


def test_timestamp_range_source_round_trip() -> None:
    src = make_timestamp_source()
    raw = src.model_dump(mode="json")
    parsed = ReferenceSourceV1.model_validate(raw)
    assert parsed == src


def test_reference_annotation_round_trip() -> None:
    ann = make_annotation_color_only()
    raw = ann.model_dump(mode="json")
    parsed = ReferenceAnnotationV1.model_validate(raw)
    assert parsed == ann


def test_pairwise_preference_round_trip() -> None:
    pw = make_pairwise()
    raw = pw.model_dump(mode="json")
    parsed = PairwisePreferenceV1.model_validate(raw)
    assert parsed == pw


def test_derived_taste_profile_round_trip() -> None:
    prof = make_derived_profile()
    raw = prof.model_dump(mode="json")
    parsed = DerivedTasteProfileV1.model_validate(raw)
    assert parsed == prof


def test_reference_library_round_trip() -> None:
    lib = make_library()
    raw = lib.model_dump(mode="json")
    parsed = ReferenceLibraryV1.model_validate(raw)
    assert parsed == lib


# ---------------------------------------------------------------------------
# (b) "I like only the color" -> color=like, others unspecified/absent
#     + unnamed leak -> ValidationError
# ---------------------------------------------------------------------------


def test_color_only_annotation_leaves_other_domains_unspecified() -> None:
    ann = make_annotation_color_only()
    assert ann.domains[PreferenceDomain.color] == "like"
    for dom in PreferenceDomain:
        if dom == PreferenceDomain.color:
            continue
        val = ann.domains.get(dom)
        assert val is None or val == "unspecified"


def test_cross_domain_leak_rejected() -> None:
    with pytest.raises(ValidationError, match="domain_not_named"):
        ReferenceAnnotationV1(
            annotation_id="ann-0002",
            source_id="ref-0001",
            named_domains=(PreferenceDomain.color,),
            domains={
                PreferenceDomain.color: "like",
                PreferenceDomain.subtitle: "like",
            },
            rationale="color ok but subtitle leak",
            scope="channel",
            provenance=_producer(),
        )


def test_unspecified_on_unnamed_allowed() -> None:
    ann = ReferenceAnnotationV1(
        annotation_id="ann-0003",
        source_id="ref-0001",
        named_domains=(PreferenceDomain.color,),
        domains={
            PreferenceDomain.color: "like",
            PreferenceDomain.subtitle: "unspecified",
        },
        rationale="color liked, subtitle explicitly unspecified",
        scope="channel",
        provenance=_producer(),
    )
    assert ann.domains[PreferenceDomain.subtitle] == "unspecified"


def test_feature_domain_not_named_rejected() -> None:
    with pytest.raises(ValidationError, match="feature_domain_not_named"):
        ReferenceAnnotationV1(
            annotation_id="ann-0004",
            source_id="ref-0001",
            named_domains=(PreferenceDomain.color,),
            domains={PreferenceDomain.color: "like"},
            rationale="color praise",
            extracted_features={PreferenceDomain.subtitle: {"chars_per_cue": "20"}},
            scope="channel",
            provenance=_producer(),
        )


def test_named_domain_missing_polarity_rejected() -> None:
    with pytest.raises(ValidationError, match="named_domain_missing"):
        ReferenceAnnotationV1(
            annotation_id="ann-0005",
            source_id="ref-0001",
            named_domains=(PreferenceDomain.color,),
            domains={},
            rationale="named but no polarity",
            scope="channel",
            provenance=_producer(),
        )


def test_named_domain_unspecified_rejected() -> None:
    with pytest.raises(ValidationError, match="named_domain_unspecified"):
        ReferenceAnnotationV1(
            annotation_id="ann-0006",
            source_id="ref-0001",
            named_domains=(PreferenceDomain.color,),
            domains={PreferenceDomain.color: "unspecified"},
            rationale="named but unspecified",
            scope="channel",
            provenance=_producer(),
        )


# ---------------------------------------------------------------------------
# (c) polarity given with no domain named -> rejected
# ---------------------------------------------------------------------------


def test_polarity_without_named_domain_rejected() -> None:
    with pytest.raises(ValidationError, match="polarity_without_domain"):
        ReferenceAnnotationV1(
            annotation_id="ann-0007",
            source_id="ref-0001",
            named_domains=(),
            domains={PreferenceDomain.color: "like"},
            rationale="like without naming",
            scope="channel",
            provenance=_producer(),
        )


def test_empty_named_and_empty_domains_allowed() -> None:
    ann = ReferenceAnnotationV1(
        annotation_id="ann-0008",
        source_id="ref-0001",
        named_domains=(),
        domains={},
        rationale="neutral note",
        scope="episode",
        provenance=_producer(),
    )
    assert ann.named_domains == ()


# ---------------------------------------------------------------------------
# (d) DerivedTasteProfile entry with empty evidence_refs -> ValidationError
# ---------------------------------------------------------------------------


def test_derived_entry_empty_evidence_refs_rejected() -> None:
    with pytest.raises(ValidationError):
        DerivedTasteEntryV1(
            domain=PreferenceDomain.pacing,
            statement="fast pacing liked",
            polarity="like",
            confidence=0.7,
            evidence_refs=(),
            source_kind="reference_annotation",
        )


def test_derived_profile_empty_evidence_via_raw_rejected() -> None:
    with pytest.raises(ValidationError):
        DerivedTasteProfileV1.model_validate(
            {
                "profile_id": "prof-0002",
                "entries": [
                    {
                        "domain": "color",
                        "statement": "warm",
                        "polarity": "like",
                        "confidence": 0.5,
                        "evidence_refs": [],
                        "source_kind": "explicit_rule",
                    }
                ],
                "unresolved_contradictions": [],
                "provenance": {"name": "test-producer", "version": "v1"},
                "created_at": "2026-08-22T00:00:00Z",
            }
        )


# ---------------------------------------------------------------------------
# (e) contradiction record survives round-trip (not averaged away)
# ---------------------------------------------------------------------------


def test_contradiction_survives_round_trip() -> None:
    prof = make_derived_profile()
    raw = prof.model_dump(mode="json")
    parsed = DerivedTasteProfileV1.model_validate(raw)
    assert len(parsed.unresolved_contradictions) == 1
    c = parsed.unresolved_contradictions[0]
    assert c.domain == PreferenceDomain.pacing
    assert c.conflicting_refs == ("ann-0001", "ann-0002")
    assert parsed.unresolved_contradictions == prof.unresolved_contradictions


def test_multiple_contradictions_round_trip() -> None:
    prof = DerivedTasteProfileV1(
        profile_id="prof-0003",
        entries=(
            DerivedTasteEntryV1(
                domain=PreferenceDomain.pacing,
                statement="fast liked",
                polarity="like",
                confidence=0.6,
                evidence_refs=("ann-0001",),
                source_kind="pairwise",
            ),
        ),
        unresolved_contradictions=(
            ContradictionRecordV1(
                domain=PreferenceDomain.color,
                description="color conflict",
                conflicting_refs=("ann-0001", "ann-0002"),
            ),
            ContradictionRecordV1(
                domain=PreferenceDomain.audio,
                description="audio conflict",
                conflicting_refs=("ann-0003", "ann-0004"),
                note="manual review needed",
            ),
        ),
        provenance=_producer(),
        created_at="2026-08-22T00:00:00Z",
    )
    parsed = DerivedTasteProfileV1.model_validate(prof.model_dump(mode="json"))
    assert parsed == prof
    assert len(parsed.unresolved_contradictions) == 2


# ---------------------------------------------------------------------------
# Additional: domain enum + polarity validation, source kinds
# ---------------------------------------------------------------------------


def test_preference_domain_set_complete() -> None:
    expected = {
        "story_structure",
        "pacing",
        "color",
        "subtitle",
        "b_roll",
        "framing_graphics",
        "audio",
    }
    assert {d.value for d in PreferenceDomain} == expected


def test_two_domain_annotation_round_trip() -> None:
    ann = ReferenceAnnotationV1(
        annotation_id="ann-0009",
        source_id="ref-0001",
        named_domains=(PreferenceDomain.pacing, PreferenceDomain.subtitle),
        domains={
            PreferenceDomain.pacing: "like",
            PreferenceDomain.subtitle: "dislike",
        },
        rationale="pacing good, subtitles bad",
        extracted_features={
            PreferenceDomain.pacing: {"shot_duration": "medium"},
            PreferenceDomain.subtitle: {"chars_per_cue": "30"},
        },
        confidence=0.75,
        scope="series",
        provenance=_producer(),
        human_approval="pending",
    )
    parsed = ReferenceAnnotationV1.model_validate(ann.model_dump(mode="json"))
    assert parsed == ann


def test_duplicate_named_domains_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate_named_domain"):
        ReferenceAnnotationV1(
            annotation_id="ann-0010",
            source_id="ref-0001",
            named_domains=(PreferenceDomain.color, PreferenceDomain.color),
            domains={PreferenceDomain.color: "like"},
            rationale="dup",
            scope="channel",
            provenance=_producer(),
        )
