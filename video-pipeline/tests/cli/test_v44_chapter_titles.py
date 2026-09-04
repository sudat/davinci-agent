from __future__ import annotations

import json

import pytest

from services.cli.v44_chapter_titles import (
    REQUEST_DATA_MARKER,
    ChapterTitleInvocation,
    ChapterTitleProposalError,
    ChapterTitleProposalSidecar,
    generate_proposal,
    main,
    proposal_path,
)
from services.creative_plan.presentation_intents import ChapterCardParams
from tests.cli.v44_chapter_titles_support import (
    TITLES,
    ChapterTitleCase,
    protected_snapshot,
    runner,
)


def test_generate_proposal_binds_exact_evidence_without_mutating_lineage(
    chapter_title_case: ChapterTitleCase,
) -> None:
    # Given
    case = chapter_title_case
    before = protected_snapshot(case.root)
    model_runner = runner()

    # When
    result = generate_proposal(
        ChapterTitleInvocation(case.root, case.runtime), model_runner, case.approved
    )

    # Then
    assert result.approval_status == "pending"
    assert result.boundary.record_frame == 1632
    assert result.boundary.record_seconds == 54.4
    assert result.boundary.source_frame == 1845
    assert result.evidence.cue_count == 79
    assert result.evidence.first_subtitle_id == "st21"
    assert len({candidate.candidate_id for candidate in result.candidates}) == 3
    for candidate, title in zip(result.candidates, TITLES, strict=True):
        params = candidate.presentation_intent.params
        assert isinstance(params, ChapterCardParams)
        assert params.title == title
        assert params.duration_frames == 45
        assert candidate.presentation_intent.target_span.start_frame == 1632
        assert candidate.presentation_intent.target_span.end_frame == 1677
    assert protected_snapshot(case.root) == before
    call = model_runner.calls[0]
    assert call.model == "gpt-5.6-sol"
    assert call.images == ()
    instructions, request_json = call.prompt.split(REQUEST_DATA_MARKER, maxsplit=1)
    request = json.loads(request_json)
    assert request["subtitle_evidence"][0]["subtitle_id"] == "st21"
    assert len(request["subtitle_evidence"]) == 79
    assert "章タイトル用の字幕21" not in instructions


def test_cli_writes_parseable_pending_runtime_sidecar(
    chapter_title_case: ChapterTitleCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    case = chapter_title_case

    # When
    exit_code = main(
        ["--episode-root", str(case.root), "--editorial-runtime", str(case.runtime)],
        runner=runner(),
        approved=case.approved,
    )

    # Then
    assert exit_code == 0
    sidecar = ChapterTitleProposalSidecar.model_validate_json(
        proposal_path(case.root).read_bytes()
    )
    assert sidecar.proposal_only is True
    assert sidecar.base_plan_version == "v3"
    assert sidecar.base_plan_sha256 == case.approved.plan_sha256
    assert "proposal ready" in capsys.readouterr().out


def test_byte_identical_rerun_is_allowed(chapter_title_case: ChapterTitleCase) -> None:
    # Given
    case = chapter_title_case
    invocation = ChapterTitleInvocation(case.root, case.runtime)
    generate_proposal(invocation, runner(), case.approved)
    before = proposal_path(case.root).read_bytes()

    # When
    generate_proposal(invocation, runner(), case.approved)

    # Then
    assert proposal_path(case.root).read_bytes() == before


def test_different_rerun_refuses_overwrite(
    chapter_title_case: ChapterTitleCase,
) -> None:
    # Given
    case = chapter_title_case
    invocation = ChapterTitleInvocation(case.root, case.runtime)
    generate_proposal(invocation, runner(), case.approved)
    before = proposal_path(case.root).read_bytes()

    # When
    with pytest.raises(ChapterTitleProposalError) as captured:
        generate_proposal(
            invocation,
            runner(("別の候補一", "別の候補二", "別の候補三")),
            case.approved,
        )

    # Then
    assert captured.value.code == "sidecar-conflict"
    assert proposal_path(case.root).read_bytes() == before
