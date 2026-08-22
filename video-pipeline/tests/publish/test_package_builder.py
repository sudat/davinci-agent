"""Task 55: package builder — QC-gated assembly + Packaging AI proposal seam."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.publish.models import PublishPackageV1, RenderRef, build_package
from services.publish.package_builder import (
    ApproverRequiredError,
    PackageBuilderError,
    PackagingChoice,
    PackagingProposalsV1,
    PackagingSuggestions,
    ProposalChoiceError,
    QualityGateInput,
    QualityGateNotPassedException,
    build_publish_package,
    commit_proposal,
    propose_packaging,
)

_ISO = "2026-08-22T10:00:00+09:00"


def _sha(n: int) -> str:
    return f"{n:064x}"


def _render(n: int = 1) -> dict[str, object]:
    return {"render_sha256": _sha(n), "path": f"renders/ep{n}.mp4"}


def _gate(*, passed: bool = True, report_ref: str = "qc_01") -> QualityGateInput:
    return QualityGateInput(
        report_ref=report_ref,
        all_domains_resolved=passed,
        checked_at=_ISO,
    )


def _meta() -> dict[str, object]:
    return {
        "episode_id": "ep_01JVLD",
        "title_candidates": ["Original Title", "Backup Title"],
        "selected_title": "Original Title",
        "description": "Original description.",
        "chapters": [{"start_seconds": 0, "title": "Intro"}],
        "channel_target": "youtube-main",
    }


_SUGGESTIONS: dict[str, object] = {
    "title_alternatives": ["AI Title 1", "AI Title 2"],
    "description_draft": "AI drafted description.",
    "chapter_suggestions": [
        {"start_seconds": 0, "title": "AI Intro"},
        {"start_seconds": 120, "title": "AI Main"},
    ],
    "thumbnail_brief": "close-up of the presenter, warm light",
}


class _RecordedFakeLlm:
    """Fake llm_suggest seam: records received packages, returns canned payload."""

    def __init__(self, payload: PackagingSuggestions | dict[str, object]) -> None:
        self._payload = payload
        self.calls: list[PublishPackageV1] = []

    def __call__(self, package: PublishPackageV1) -> PackagingSuggestions | dict[str, object]:
        self.calls.append(package)
        return self._payload


def _built_package() -> PublishPackageV1:
    return build_publish_package(_render(1), _gate(), _meta())


# (a) QC-passed -> package with deterministic key
def test_build_qc_passed_deterministic_key() -> None:
    pkg_a = build_publish_package(_render(1), _gate(), _meta())
    pkg_b = build_publish_package(_render(1), _gate(), _meta())

    assert pkg_a == pkg_b
    assert (
        pkg_a.idempotency_key
        == build_package(RenderRef.model_validate(_render(1)), _meta()).idempotency_key
    )
    assert "qc_01" in pkg_a.approval_refs
    assert pkg_a.selected_title == "Original Title"


# (b) QC-failed -> typed refusal
def test_build_qc_failed_refused() -> None:
    with pytest.raises(QualityGateNotPassedException) as exc:
        build_publish_package(_render(1), _gate(passed=False, report_ref="qc_bad"), _meta())

    assert exc.value.report_ref == "qc_bad"
    assert isinstance(exc.value, PackageBuilderError)


# (b) malformed gate -> ValidationError (typed refusal, no package)
@pytest.mark.parametrize(
    "bad_gate",
    [
        {"report_ref": "qc_01", "checked_at": _ISO},
        {"report_ref": "qc_01", "all_domains_resolved": "yes", "checked_at": _ISO},
        {"report_ref": "qc_01", "all_domains_resolved": True, "checked_at": "not-a-time"},
        {"report_ref": "", "all_domains_resolved": True, "checked_at": _ISO},
    ],
)
def test_build_malformed_gate_rejected(bad_gate: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        build_publish_package(_render(1), bad_gate, _meta())


# (c) proposals attach WITHOUT mutating selected fields (byte-equal)
def test_propose_attaches_without_mutating_selected_fields() -> None:
    pkg = _built_package()
    before_json = pkg.model_dump_json()

    bundle = propose_packaging(pkg, _RecordedFakeLlm(_SUGGESTIONS))

    assert pkg.model_dump_json() == before_json
    assert bundle.package.model_dump_json() == before_json
    assert bundle.package is pkg
    assert bundle.proposals.suggestions.title_alternatives == ["AI Title 1", "AI Title 2"]
    assert bundle.proposals.provenance == "ai-proposal"


# (d) commit with approver -> new version + approval ref
def test_commit_with_approver_creates_new_version() -> None:
    pkg = _built_package()
    bundle = propose_packaging(pkg, _RecordedFakeLlm(_SUGGESTIONS))
    choice = PackagingChoice(
        selected_title="AI Title 1",
        apply_description_draft=True,
        apply_chapter_suggestions=True,
    )

    committed = commit_proposal(bundle.package, bundle.proposals, choice, approver="op_alice")

    assert committed != pkg
    assert committed.selected_title == "AI Title 1"
    assert "AI Title 1" in committed.title_candidates
    assert "AI Title 2" in committed.title_candidates
    assert "Original Title" in committed.title_candidates
    assert committed.description == "AI drafted description."
    assert [c.title for c in committed.chapters] == ["AI Intro", "AI Main"]
    assert "packaging-approval:op_alice" in committed.approval_refs
    assert "qc_01" in committed.approval_refs
    assert committed.idempotency_key == pkg.idempotency_key
    assert pkg.selected_title == "Original Title"


# (e) commit without approver -> typed error
@pytest.mark.parametrize("approver", [None, "", "   "])
def test_commit_without_approver_refused(approver: str | None) -> None:
    bundle = propose_packaging(_built_package(), _RecordedFakeLlm(_SUGGESTIONS))

    with pytest.raises(ApproverRequiredError):
        commit_proposal(
            bundle.package,
            bundle.proposals,
            PackagingChoice(selected_title="AI Title 1"),
            approver=approver,
        )


# (f) fake suggestions round-trip through the seam
def test_fake_suggestions_round_trip() -> None:
    pkg = _built_package()
    fake = _RecordedFakeLlm(_SUGGESTIONS)

    bundle = propose_packaging(pkg, fake)

    assert fake.calls == [pkg]
    prop = bundle.proposals
    assert prop.schema_version == "packaging-proposals-v1"
    assert prop.episode_id == pkg.episode_id
    assert prop.package_idempotency_key == pkg.idempotency_key
    assert PackagingProposalsV1.model_validate(prop.model_dump()) == prop
    assert PackagingProposalsV1.model_validate_json(prop.model_dump_json()) == prop
    assert PackagingSuggestions.model_validate(_SUGGESTIONS) == prop.suggestions


# extra: committing a title that was never proposed -> typed refusal
def test_commit_title_not_proposed_refused() -> None:
    bundle = propose_packaging(_built_package(), _RecordedFakeLlm(_SUGGESTIONS))

    with pytest.raises(ProposalChoiceError, match="not among"):
        commit_proposal(
            bundle.package,
            bundle.proposals,
            PackagingChoice(selected_title="Smuggled Title"),
            approver="op_alice",
        )


# extra: proposals belonging to another package (stale state) -> typed refusal
def test_commit_foreign_proposals_refused() -> None:
    pkg = _built_package()
    foreign = PackagingProposalsV1(
        episode_id=pkg.episode_id,
        package_idempotency_key="550e8400-e29b-41d4-a716-446655440099",
        suggestions=PackagingSuggestions.model_validate(_SUGGESTIONS),
    )

    with pytest.raises(ProposalChoiceError, match="do not belong"):
        commit_proposal(
            pkg, foreign, PackagingChoice(apply_description_draft=True), approver="op_alice"
        )


# extra: garbage LLM payload -> ValidationError, nothing partial attaches
def test_propose_garbage_suggestions_rejected() -> None:
    with pytest.raises(ValidationError):
        propose_packaging(_built_package(), _RecordedFakeLlm({"title_alternatives": [123]}))
