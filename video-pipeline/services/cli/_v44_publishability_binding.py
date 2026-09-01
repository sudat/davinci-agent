"""Current-render binding truth for publishability records.

ONE helper shared by the writer (``record-publishability``) and the reader
(``gate-summary``/current): current render truth is the output sha256 of the
single ``native-render-meta-v1`` record under
``finishing/final-resolve-render/``, verified against the actual render
BYTES at ``output_path`` (symlinks/non-regular files fail; episode-root
containment enforced) and against the ``render`` input binding of
``finishing/qc-report.json``. Runtime history such as
``finishing-run.json.native_render`` is never consulted — stale entries must
not become present-operator truth. Every failure is a typed FinishingError;
nothing here writes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import TypeAdapter, ValidationError

from services.cli._v44_finishing_build import FinishingError, FinishingMalformedError
from services.contracts.primitives import Sha256
from services.final_review.publishability import PublishabilityReviewV1
from services.foundation_io import sha256_file
from services.mcp_execution.native_render_meta import NativeRenderMeta
from services.qc.models import QcReport

if TYPE_CHECKING:
    from services.cli._v44_finishing_report import FinishingRunReportV1

PUBLISHABILITY_RELATIVE: Final = ("review", "publishability.json")
RENDER_META_DIR_RELATIVE: Final = ("finishing", "final-resolve-render")
QC_REPORT_RELATIVE: Final = ("finishing", "qc-report.json")
_SHA256: Final = TypeAdapter(Sha256)


def _render_file_within_root(episode_root: Path, output_path: str) -> Path:
    """The meta-named output path, verified to stay inside the episode root.

    Containment is decided on the fully resolved path (an outside symlink
    target is outside), but the UNRESOLVED path is returned so the caller's
    O_NOFOLLOW open still rejects a symlinked final component.
    """

    root = episode_root.resolve()
    candidate = Path(output_path)
    open_path = (
        candidate if candidate.is_absolute() else episode_root / candidate
    )
    if not open_path.resolve().is_relative_to(root):
        raise FinishingError(
            "render-output-outside-episode-root",
            f"render output {open_path} resolves outside the episode root {root}",
        )
    return open_path


def validate_viewed_render_sha256(value: str) -> str:
    """64-hex or a typed malformed refusal; the value itself on success."""

    try:
        return _SHA256.validate_python(value)
    except ValidationError as error:
        raise FinishingMalformedError(
            "viewed-render-sha-invalid",
            f"{value!r} is not a 64-hex sha256 render binding: {error}",
        ) from error


def _verify_render_bytes(episode_root: Path, meta: NativeRenderMeta) -> None:
    """Hash the actual render bytes; typed refusal on any divergence."""

    render_file = _render_file_within_root(episode_root, meta.output_path)
    try:
        actual = sha256_file(render_file)
    except FileNotFoundError as error:
        raise FinishingError(
            "render-output-missing",
            f"the bound render bytes are gone: {render_file}",
        ) from error
    except OSError as error:
        raise FinishingError(
            "render-output-unreadable",
            f"{render_file} is unreadable (symlink or not a regular file): {error}",
        ) from error
    if actual != meta.output_sha256:
        raise FinishingError(
            "render-bytes-mismatch",
            f"{render_file} now hashes {actual}, not the bound {meta.output_sha256}; "
            "the render bytes changed after the record/meta/QC were written",
        )


def resolve_current_render_sha256(episode_root: Path) -> str:
    """The one unambiguous current render sha256 (meta + QC report agree)."""

    render_dir = episode_root.joinpath(*RENDER_META_DIR_RELATIVE)
    metas = sorted(render_dir.glob("*.meta.json")) if render_dir.is_dir() else []
    if not metas:
        raise FinishingError(
            "render-meta-missing",
            f"no native-render-meta-v1 record under {render_dir}; the current "
            "render identity is unknown, so publishability cannot bind",
        )
    if len(metas) > 1:
        raise FinishingError(
            "render-meta-ambiguous",
            f"{len(metas)} native-render-meta records under {render_dir}: "
            f"{[meta.name for meta in metas]}",
        )
    try:
        meta = NativeRenderMeta.model_validate_json(metas[0].read_bytes())
    except (OSError, ValidationError) as error:
        raise FinishingMalformedError(
            "render-meta-invalid", f"{metas[0]} is not native-render-meta-v1: {error}"
        ) from error
    qc_path = episode_root.joinpath(*QC_REPORT_RELATIVE)
    try:
        report = QcReport.model_validate_json(qc_path.read_bytes())
    except (OSError, ValidationError) as error:
        raise FinishingMalformedError(
            "qc-report-invalid", f"{qc_path} is not a qc-report-v1 record: {error}"
        ) from error
    renders = [binding.sha256 for binding in report.inputs if binding.kind == "render"]
    if not renders:
        raise FinishingError(
            "qc-render-input-missing",
            f"{qc_path} binds no render input; the QC target is unknown",
        )
    if len(renders) > 1:
        raise FinishingError(
            "qc-render-input-ambiguous",
            f"{qc_path} binds {len(renders)} render inputs: {renders}",
        )
    if renders[0] != meta.output_sha256:
        raise FinishingError(
            "render-truth-disagreement",
            f"meta binds {meta.output_sha256} but the QC report binds {renders[0]}",
        )
    _verify_render_bytes(episode_root, meta)
    return meta.output_sha256


def require_matching_current_render(episode_root: Path, viewed_render_sha256: str) -> None:
    """Writer-side guard: the viewed sha must BE the current render truth."""

    viewed = validate_viewed_render_sha256(viewed_render_sha256)
    current = resolve_current_render_sha256(episode_root)
    if viewed != current:
        raise FinishingError(
            "viewed-render-sha-mismatch",
            f"viewed render {viewed} is not the current render {current}; "
            "watch the current render and record again",
        )


def load_bound_publishability(
    episode_root: Path, *, report: FinishingRunReportV1
) -> PublishabilityReviewV1:
    """Reader-side load: refuse unbound, render-mismatched, or foreign-run
    records (fail closed on the stale pre-binding verdict)."""

    review_path = episode_root.joinpath(*PUBLISHABILITY_RELATIVE)
    if not review_path.is_file():
        raise FinishingError(
            "publishability-missing",
            f"no publishability record at {review_path}; the operator must watch "
            f"the final preview and record a verdict via `record-publishability` "
            f"before a gate summary can exist",
        )
    try:
        review = PublishabilityReviewV1.model_validate_json(review_path.read_bytes())
    except (OSError, ValidationError) as error:
        raise FinishingMalformedError(
            "evidence-invalid",
            f"{review_path} is not a publishability-review-v1 record: {error}",
        ) from error
    if review.viewed_render_sha256 is None:
        raise FinishingError(
            "publishability-unbound",
            f"{review_path} carries no viewed_render_sha256 (a pre-binding "
            "record); the operator must re-record the verdict against the "
            "current render",
        )
    current = resolve_current_render_sha256(episode_root)
    if review.viewed_render_sha256 != current:
        raise FinishingError(
            "publishability-render-mismatch",
            f"record binds viewed render {review.viewed_render_sha256}, but the "
            f"current render is {current}",
        )
    if review.run_id != report.run_id:
        raise FinishingError(
            "publishability-run-mismatch",
            f"record binds run {review.run_id}, but the current finishing run "
            f"is {report.run_id}",
        )
    return review


__all__ = [
    "PUBLISHABILITY_RELATIVE",
    "load_bound_publishability",
    "require_matching_current_render",
    "resolve_current_render_sha256",
    "validate_viewed_render_sha256",
]
