"""Persisted director inputs for selection re-entry (consultation slice 2).

The initial chain writes ``run/selection-inputs.json``; a selection rebuild
reloads it and re-runs the SAME ``select_and_reconcile`` seam — analyzers
never re-run. One concept (the persisted record plus its save/load), shared
by ``real_director`` (writer) and ``episode_runner_selection`` (reader).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from services.analyze.asr_models import TranscriptArtifact  # noqa: TC001 (pydantic runtime field)
from services.analyze.candidate_models import (
    AnalysisArtifact,  # noqa: TC001 (pydantic runtime field)
)
from services.analyze.orchestrator_models import (
    OrchestrationRecord,  # noqa: TC001 (pydantic runtime field)
)
from services.analyze.visual_models import (
    VisualAnalysisArtifact,  # noqa: TC001 (pydantic runtime field)
)
from services.contracts.primitives import ArtifactRef, StrictModel
from services.editorial.candidate_models import (
    CandidatePool,  # noqa: TC001 (pydantic runtime field)
)
from services.foundation_io import atomic_write, canonical_model_bytes

if TYPE_CHECKING:
    from services.cli.real_analyze import RealAnalysis
    from services.cli.real_pool import SpeechSegment

SELECTION_INPUTS_NAME: Final = "selection-inputs.json"


class PersistedSpeechSegment(StrictModel):
    """JSON mirror of the ``SpeechSegment`` dataclass (selection re-entry)."""

    segment_id: str
    text: str
    start_frame: int
    end_frame: int


class SelectionInputsV1(StrictModel):
    """Director inputs persisted for a later selection rebuild."""

    schema_version: Literal["real-selection-inputs-v1"] = "real-selection-inputs-v1"
    episode_id: str
    source_id: str
    total_frames: int
    pool: CandidatePool
    speech: tuple[PersistedSpeechSegment, ...]
    evidence: tuple[ArtifactRef, ...]
    record: OrchestrationRecord
    transcript: TranscriptArtifact
    dialogue: AnalysisArtifact
    visual: VisualAnalysisArtifact


class SelectionInputsError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def save_selection_inputs(  # noqa: PLR0913 (persisted input record: one parameter per field)
    out_dir: Path,
    *,
    episode_id: str,
    source_id: str,
    total_frames: int,
    analysis: RealAnalysis,
    pool: CandidatePool,
    speech: tuple[SpeechSegment, ...],
) -> Path:
    """Persist the director inputs for a later selection re-entry."""

    path = out_dir / SELECTION_INPUTS_NAME
    atomic_write(
        path,
        canonical_model_bytes(
            SelectionInputsV1(
                episode_id=episode_id,
                source_id=source_id,
                total_frames=total_frames,
                pool=pool,
                speech=tuple(
                    PersistedSpeechSegment(
                        segment_id=segment.segment_id,
                        text=segment.text,
                        start_frame=segment.start_frame,
                        end_frame=segment.end_frame,
                    )
                    for segment in speech
                ),
                evidence=analysis.evidence,
                record=analysis.record,
                transcript=analysis.transcript,
                dialogue=analysis.dialogue,
                visual=analysis.visual,
            )
        ),
    )
    return path


def load_selection_inputs(run_dir: Path) -> SelectionInputsV1:
    """Reload the chain-persisted director inputs (analyzers never re-run)."""

    path = run_dir / SELECTION_INPUTS_NAME
    try:
        return SelectionInputsV1.model_validate_json(path.read_bytes())
    except OSError as error:
        raise SelectionInputsError(
            "selection-inputs-missing",
            f"no persisted director inputs at {path}; the initial chain "
            "never recorded them",
        ) from error
    except ValueError as error:
        raise SelectionInputsError(
            "selection-inputs-unreadable", f"unparsable selection inputs: {error}"
        ) from error


__all__ = [
    "SELECTION_INPUTS_NAME",
    "PersistedSpeechSegment",
    "SelectionInputsError",
    "SelectionInputsV1",
    "load_selection_inputs",
    "save_selection_inputs",
]
