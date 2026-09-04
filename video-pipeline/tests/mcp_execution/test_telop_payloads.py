"""WBS-2 telop payload assembly: typed params mirror the subtitle payloads.

DESIGN telop-nested §8 WBS-2: ``TelopParams``/``TelopCardPayload`` are the
``SubtitleParams``/``SubtitleCuePayload`` mirror for the three telop kinds
(opening / persistent / chapter). The payload carries kind, exact text,
half-open record spans, and the style reference resolved through the WBS-0
profile ``telop_style`` — never presentation values themselves.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from services.mcp_execution.plan_payloads import (
    StepParams,
    TelopCardPayload,
    TelopParams,
)

_STEP_PARAMS = TypeAdapter(StepParams)


def _card(kind: str, start: int, end: int) -> dict[str, object]:
    return {
        "card_id": f"telop-{kind}",
        "kind": kind,
        "text": "【人生終わった】カメラのケースが見つからない件",
        "record_span": {"start_frame": start, "end_frame": end},
    }


def _params(cards: list[dict[str, object]]) -> dict[str, object]:
    return {
        "action": "apply_telop",
        "cards": cards,
        "style_profile_id": "telop/default",
    }


def test_telop_params_parse_the_three_committed_kinds() -> None:
    # Given: one card per telop kind with non-empty half-open record spans
    # When: parsing the normalized params through the discriminated union
    # Then: every kind survives as itself and the spans stay half-open
    parsed = TelopParams.model_validate(
        _params(
            [
                _card("opening", 0, 90),
                _card("persistent", 90, 7837),
                _card("chapter", 1632, 1677),
            ]
        )
    )
    assert [card.kind for card in parsed.cards] == [
        "opening",
        "persistent",
        "chapter",
    ]
    spans = [
        (card.record_span.start_frame, card.record_span.end_frame) for card in parsed.cards
    ]
    assert spans == [
        (0, 90),
        (90, 7837),
        (1632, 1677),
    ]
    assert parsed.style_profile_id == "telop/default"


def test_step_params_union_discriminates_apply_telop() -> None:
    # Given: the apply_telop payload inside the closed StepParams union
    # When: validating through the action discriminator
    # Then: it parses as TelopParams (a subtitle action cannot wear it)
    parsed = _STEP_PARAMS.validate_python(_params([_card("chapter", 1632, 1677)]))
    assert isinstance(parsed, TelopParams)
    with pytest.raises(ValidationError):
        _STEP_PARAMS.validate_python(
            {**_params([_card("chapter", 1632, 1677)]), "action": "apply_subtitles"}
        )


def test_telop_params_reject_unknown_kind_and_extras() -> None:
    # Given: a card whose kind is outside the three committed kinds
    # When: the params parse
    # Then: the closed Literal refuses it (no free-form kind strings)
    with pytest.raises(ValidationError):
        TelopParams.model_validate(_params([_card("closing", 7000, 7837)]))
    # And: extra fields stay forbidden (StrictModel, no silent passthrough)
    with pytest.raises(ValidationError):
        TelopParams.model_validate(
            {**_params([_card("opening", 0, 90)]), "font_size_px": 38}
        )


def test_telop_params_refuse_empty_and_inverted_spans() -> None:
    # Given: cards whose record spans are empty or inverted
    # When: the params parse
    # Then: the span_empty validator names the offending card id
    for start, end in ((90, 90), (120, 90)):
        with pytest.raises(ValidationError, match=r"span_empty|span_inverted"):
            TelopParams.model_validate(_params([_card("persistent", start, end)]))


def test_telop_card_payload_refuses_blank_text() -> None:
    # Given: a telop card with no display text
    # When: parsing the card payload alone
    # Then: it refuses (the mutation reproduces exact text — blank is a bug)
    with pytest.raises(ValidationError):
        TelopCardPayload.model_validate({**_card("opening", 0, 90), "text": ""})
