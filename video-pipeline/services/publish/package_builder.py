"""Package builder — QC-gated PublishPackageV1 assembly + Packaging AI proposal seam.

Spec: PRD v4.3 §15 (publishing) + implementation-plan §11.3 (Packaging AI).
Builds a publish package ONLY from an approved render plus a quality gate
with every domain resolved. Optional LLM packaging suggestions attach as a
``PackagingProposalsV1`` side model (provenance ``ai-proposal``) and NEVER
mutate the package's selected title/description/chapters — a human (or
channel policy) commits the final version via ``commit_proposal``.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, StrictModel
from services.publish.models import (
    Chapter,
    PublishPackageV1,
    RenderRef,
    build_package,
)

# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class PackageBuilderError(RuntimeError):
    """Base class for typed package-builder refusals."""


class QualityGateNotPassedException(PackageBuilderError):  # noqa: N818 - task-mandated name
    """Typed refusal when the quality gate still has unresolved domains."""

    def __init__(self, report_ref: str) -> None:
        self.report_ref = report_ref
        super().__init__(
            f"quality gate not passed (all_domains_resolved=false): {report_ref}"
        )


class ApproverRequiredError(PackageBuilderError):
    """Typed refusal when a proposal commit lacks an explicit human approver."""

    def __init__(self) -> None:
        super().__init__("commit_proposal requires an explicit non-empty approver")


class ProposalChoiceError(PackageBuilderError):
    """Typed refusal when a commit choice does not match the attached proposals."""


# ---------------------------------------------------------------------------
# Quality gate seam
# ---------------------------------------------------------------------------


class QualityGateInput(StrictModel):
    """Minimal QC gate summary consumed by ``build_publish_package``.

    Seam: this is a deliberately minimal local model. Task 40's real
    FinalQCReport plugs in here later — keep the field contract
    (``report_ref`` / ``all_domains_resolved`` / ``checked_at``) when
    replacing it; builder call sites stay unchanged.
    """

    report_ref: Identifier
    all_domains_resolved: bool
    checked_at: Annotated[str, Field(min_length=1, strict=True)]

    @field_validator("checked_at", mode="after")
    @classmethod
    def _check_iso8601(cls, v: str) -> str:
        candidate = v.replace("Z", "+00:00") if v.endswith("Z") else v
        try:
            datetime.datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise PydanticCustomError(
                "invalid_iso8601",
                "checked_at must be ISO-8601: {value}",
                {"value": v},
            ) from exc
        return v


# ---------------------------------------------------------------------------
# Packaging AI proposal models
# ---------------------------------------------------------------------------


class PackagingSuggestions(StrictModel):
    """LLM packaging suggestions — proposal only, never auto-committed."""

    title_alternatives: list[Annotated[str, Field(min_length=1, strict=True)]] = (
        Field(default_factory=list)
    )
    description_draft: str = ""
    chapter_suggestions: list[Chapter] = Field(default_factory=list)
    thumbnail_brief: Annotated[str, Field(min_length=1, strict=True)] | None = None


class PackagingProposalsV1(StrictModel):
    """Side model attached next to a package — provenance always ``ai-proposal``."""

    schema_version: Literal["packaging-proposals-v1"] = "packaging-proposals-v1"
    episode_id: Identifier
    package_idempotency_key: Annotated[str, Field(min_length=1, strict=True)]
    provenance: Literal["ai-proposal"] = "ai-proposal"
    suggestions: PackagingSuggestions


class PackagingChoice(StrictModel):
    """Human selection from attached proposals (input to ``commit_proposal``)."""

    selected_title: Annotated[str, Field(min_length=1, strict=True)] | None = None
    apply_description_draft: bool = False
    apply_chapter_suggestions: bool = False


class PackageWithProposals(StrictModel):
    """Bundle of the untouched package and its AI proposal side model."""

    package: PublishPackageV1
    proposals: PackagingProposalsV1


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_publish_package(
    render_ref: RenderRef | dict[str, object],
    quality_gate: QualityGateInput | dict[str, object],
    episode_metadata: dict[str, object] | None = None,
    *,
    approvals: dict[str, object] | None = None,
) -> PublishPackageV1:
    """Assemble a PublishPackageV1 from an approved render + QC-passed evidence.

    Refuses with typed ``QualityGateNotPassedException`` unless every QC
    domain is resolved. The QC report reference is recorded in
    ``approval_refs``; the idempotency key reuses task 53's deterministic
    convention (same render → same key).
    """
    gate = (
        quality_gate
        if isinstance(quality_gate, QualityGateInput)
        else QualityGateInput.model_validate(quality_gate)
    )
    if not gate.all_domains_resolved:
        raise QualityGateNotPassedException(gate.report_ref)

    appr = dict(approvals or {})
    existing: object = appr.get("approval_refs", [])
    if not isinstance(existing, list | tuple):
        raise PackageBuilderError("approvals.approval_refs must be a list")
    refs: list[object] = list(existing)
    if gate.report_ref not in refs:
        refs.append(gate.report_ref)
    appr["approval_refs"] = refs
    return build_package(render_ref, episode_metadata, approvals=appr)


def propose_packaging(
    package: PublishPackageV1,
    llm_suggest: Callable[[PublishPackageV1], PackagingSuggestions | dict[str, object]],
) -> PackageWithProposals:
    """Attach LLM packaging suggestions as a proposal-only side model.

    The package is NEVER mutated (selected title / description / chapters
    stay byte-identical). Malformed suggestion payloads raise ValidationError
    before anything attaches — no partial proposals.
    """
    raw = llm_suggest(package)
    suggestions = (
        raw
        if isinstance(raw, PackagingSuggestions)
        else PackagingSuggestions.model_validate(raw)
    )
    proposals = PackagingProposalsV1(
        episode_id=package.episode_id,
        package_idempotency_key=package.idempotency_key,
        suggestions=suggestions,
    )
    return PackageWithProposals(package=package, proposals=proposals)


def commit_proposal(
    package: PublishPackageV1,
    proposals: PackagingProposalsV1,
    choice: PackagingChoice,
    *,
    approver: str | None = None,
) -> PublishPackageV1:
    """Commit a human choice from the proposals into a NEW package version.

    Human path only: a non-empty ``approver`` is mandatory (typed
    ``ApproverRequiredError`` otherwise); ``packaging-approval:<approver>``
    is appended to ``approval_refs``. The idempotency key is preserved
    (same render → same upload identity).
    """
    if approver is None or not approver.strip():
        raise ApproverRequiredError
    if proposals.package_idempotency_key != package.idempotency_key:
        raise ProposalChoiceError(
            "proposals do not belong to this package "
            f"(expected {package.idempotency_key}, got {proposals.package_idempotency_key})"
        )
    if (
        choice.selected_title is not None
        and choice.selected_title not in proposals.suggestions.title_alternatives
    ):
        raise ProposalChoiceError(
            f"selected_title {choice.selected_title!r} is not among the proposed alternatives"
        )

    alternatives = proposals.suggestions.title_alternatives
    title_candidates = [
        *package.title_candidates,
        *(a for a in alternatives if a not in package.title_candidates),
    ]
    selected_title = (
        choice.selected_title if choice.selected_title is not None else package.selected_title
    )
    description = (
        proposals.suggestions.description_draft
        if choice.apply_description_draft
        else package.description
    )
    chapters = (
        list(proposals.suggestions.chapter_suggestions)
        if choice.apply_chapter_suggestions
        else list(package.chapters)
    )
    approval_ref = f"packaging-approval:{approver}"
    approval_refs = (
        [*package.approval_refs, approval_ref]
        if approval_ref not in package.approval_refs
        else list(package.approval_refs)
    )

    return PublishPackageV1(
        episode_id=package.episode_id,
        render_ref=package.render_ref,
        title_candidates=title_candidates,
        selected_title=selected_title,
        description=description,
        chapters=chapters,
        tags=package.tags,
        thumbnail_ref=package.thumbnail_ref,
        playlist_target=package.playlist_target,
        visibility=package.visibility,
        schedule_time=package.schedule_time,
        approval_refs=approval_refs,
        publication_approval_ref=package.publication_approval_ref,
        channel_target=package.channel_target,
        idempotency_key=package.idempotency_key,
        remote_video_id=package.remote_video_id,
    )


__all__ = [
    "ApproverRequiredError",
    "PackageBuilderError",
    "PackageWithProposals",
    "PackagingChoice",
    "PackagingProposalsV1",
    "PackagingSuggestions",
    "ProposalChoiceError",
    "QualityGateInput",
    "QualityGateNotPassedException",
    "build_publish_package",
    "commit_proposal",
    "propose_packaging",
]
