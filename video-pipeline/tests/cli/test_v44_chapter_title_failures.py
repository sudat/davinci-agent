from __future__ import annotations

import json
from dataclasses import replace

import pytest

from services.cli.v44_chapter_titles import (
    ChapterTitleInvocation,
    ChapterTitleProposalError,
    generate_proposal,
    main,
    proposal_path,
)
from services.editorial_v2.editorial_pins import CodexRunner, EditorialRuntimeError
from services.foundation_io import atomic_write
from tests.cli.v44_chapter_titles_support import (
    ChapterTitleCase,
    RecordingRunner,
    RuntimeSpec,
    TimeoutRunner,
    runner,
    write_runtime,
)


@pytest.mark.parametrize(
    "payload",
    [
        {"titles": ["一", "二"]},
        {"titles": ["同じ", "同じ", "別"]},
        {"titles": ["plain", "titles", "only"]},
        {"titles": ["一", "二", "三"], "approval": "approved"},
        {"titles": [{"id": "model-id", "title": "一"}, "二", "三"]},
        {"titles": ["一", "二", "三"], "tool_claims": ["rendered"]},
    ],
)
def test_model_response_contract_refuses_non_title_payloads(
    chapter_title_case: ChapterTitleCase, payload
) -> None:
    # Given
    case = chapter_title_case
    model_runner = RecordingRunner(json.dumps(payload, ensure_ascii=False))

    # When
    with pytest.raises(EditorialRuntimeError) as captured:
        generate_proposal(
            ChapterTitleInvocation(case.root, case.runtime), model_runner, case.approved
        )

    # Then
    assert captured.value.code == "model-bad-response"
    assert not proposal_path(case.root).exists()


def test_corrupt_review_seal_is_typed_and_calls_no_model(
    chapter_title_case: ChapterTitleCase,
) -> None:
    # Given
    case = chapter_title_case
    atomic_write(case.root / "review" / "events.jsonl.seal", b"{}")
    model_runner = runner()

    # When
    with pytest.raises(ChapterTitleProposalError) as captured:
        generate_proposal(
            ChapterTitleInvocation(case.root, case.runtime), model_runner, case.approved
        )

    # Then
    assert captured.value.code == "lineage-invalid"
    assert model_runner.calls == []
    assert not proposal_path(case.root).exists()


def test_wrong_plan_hash_is_typed_and_calls_no_model(
    chapter_title_case: ChapterTitleCase,
) -> None:
    # Given
    case = chapter_title_case
    approved = replace(case.approved, plan_sha256="0" * 64)
    model_runner = runner()

    # When
    with pytest.raises(ChapterTitleProposalError) as captured:
        generate_proposal(
            ChapterTitleInvocation(case.root, case.runtime), model_runner, approved
        )

    # Then
    assert captured.value.code == "lineage-mismatch"
    assert model_runner.calls == []
    assert not proposal_path(case.root).exists()


def test_wrong_operator_boundary_is_typed_and_calls_no_model(
    chapter_title_case: ChapterTitleCase,
) -> None:
    # Given
    case = chapter_title_case
    approved = replace(case.approved, record_frame=1633)
    model_runner = runner()

    # When
    with pytest.raises(ChapterTitleProposalError) as captured:
        generate_proposal(
            ChapterTitleInvocation(case.root, case.runtime), model_runner, approved
        )

    # Then
    assert captured.value.code == "boundary-mismatch"
    assert model_runner.calls == []
    assert not proposal_path(case.root).exists()


@pytest.mark.parametrize(
    "spec",
    [
        RuntimeSpec(mode="heuristic_diagnostic"),
        RuntimeSpec(transport="openai-api"),
        RuntimeSpec(purpose="other-purpose"),
        RuntimeSpec(model_id="other-model"),
    ],
)
def test_runtime_or_pin_drift_is_typed_and_calls_no_model(
    chapter_title_case: ChapterTitleCase,
    tmp_path,
    spec: RuntimeSpec,
) -> None:
    # Given
    case = chapter_title_case
    runtime = write_runtime(tmp_path, spec)
    model_runner = runner()

    # When
    with pytest.raises(EditorialRuntimeError) as captured:
        generate_proposal(
            ChapterTitleInvocation(case.root, runtime), model_runner, case.approved
        )

    # Then
    assert captured.value.code == "production-model-unavailable"
    assert model_runner.calls == []
    assert not proposal_path(case.root).exists()


def test_model_timeout_uses_existing_typed_vocabulary_and_writes_nothing(
    chapter_title_case: ChapterTitleCase,
) -> None:
    # Given
    case = chapter_title_case
    model_runner: CodexRunner = TimeoutRunner()

    # When
    with pytest.raises(EditorialRuntimeError) as captured:
        generate_proposal(
            ChapterTitleInvocation(case.root, case.runtime), model_runner, case.approved
        )

    # Then
    assert captured.value.code == "model-timeout"
    assert not proposal_path(case.root).exists()


def test_cli_model_failure_returns_refusal_without_output(
    chapter_title_case: ChapterTitleCase,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    case = chapter_title_case
    model_runner = RecordingRunner('{"titles":["重複","重複","別"]}')

    # When
    exit_code = main(
        ["--episode-root", str(case.root), "--editorial-runtime", str(case.runtime)],
        runner=model_runner,
        approved=case.approved,
    )

    # Then
    assert exit_code == 1
    assert "model-bad-response" in capsys.readouterr().err
    assert not proposal_path(case.root).exists()
