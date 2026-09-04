from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from services.contracts.edit_plan_0c import (
    EditPlan0C,
    EditPlanBody0C,
    EditPlanItem0C,
    EditSourceRef0C,
    ItemIdSelector0C,
)
from services.contracts.primitives import Producer, RationalFrameRate, SourceFrameSpan
from services.editorial_v2.editorial_pins import EditorialPinV2, EditorialRuntimeV1
from services.episode_cockpit.models import IntakeRecordV1
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.review_command.commit import commit_command
from services.review_command.models import (
    Confidence0C,
    CorrectSubtitleProposal0C,
    ProposalAmbiguity0C,
)
from services.review_command.store import OperatorDecision0C, initialize_store
from tests.review_command.support import approve_editorial_plan_proposal

if TYPE_CHECKING:
    from services.cli.v44_chapter_titles import ApprovedChapterBoundary

EPISODE_ID: Final = "ep-457dfac97989568e"
RATE: Final = RationalFrameRate(num=30, den=1)
TITLES: Final = ("カメラが動き出す瞬間", "人物撮影の舞台裏", "撮影セットの始まり")


@dataclass(frozen=True, slots=True)
class Segment:
    number: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class RuntimeSpec:
    mode: Literal["production_model", "heuristic_diagnostic"] = "production_model"
    transport: Literal["codex-exec", "openai-api"] = "codex-exec"
    purpose: str = "editorial-director-v2"
    model_id: str = "gpt-5.6-sol"


DEFAULT_RUNTIME_SPEC: Final = RuntimeSpec()


@dataclass(frozen=True, slots=True)
class RunnerCall:
    prompt: str
    model: str
    images: tuple[Path, ...]
    timeout_s: float


class RecordingRunner:
    __slots__ = ("calls", "message")

    def __init__(self, message: str) -> None:
        self.message = message
        self.calls: list[RunnerCall] = []

    def __call__(
        self, prompt: str, *, model: str, images: tuple[Path, ...], timeout_s: float
    ) -> str:
        self.calls.append(RunnerCall(prompt, model, images, timeout_s))
        return self.message


class TimeoutRunner:
    def __call__(
        self, prompt: str, *, model: str, images: tuple[Path, ...], timeout_s: float
    ) -> str:
        del prompt, model, images, timeout_s
        raise TimeoutError


@dataclass(frozen=True, slots=True)
class ChapterTitleCase:
    root: Path
    runtime: Path
    approved: ApprovedChapterBoundary


def _segments() -> tuple[Segment, ...]:
    numbers = tuple(number for number in range(1, 100) if number not in {3, 7})
    rows: list[Segment] = []
    source_cursor = 0
    for number in numbers:
        if number == 21:
            source_cursor = 1845
        if number < 20:
            length = 90
        elif number == 20:
            length = 102
        elif number < 99:
            length = 78
        else:
            length = 76
        rows.append(Segment(number, source_cursor, source_cursor + length))
        source_cursor += length
    return tuple(rows)


def _plan() -> EditPlan0C:
    items: list[EditPlanItem0C] = []
    segments = _segments()
    for segment in segments:
        span = SourceFrameSpan(start_frame=segment.start, end_frame=segment.end, rate=RATE)
        items.extend(
            (
                EditPlanItem0C(
                    item_id=f"s{segment.number}", kind="video", source_id="edit-source",
                    span=span, track_index=1, av_link_id=f"av{segment.number}",
                ),
                EditPlanItem0C(
                    item_id=f"a{segment.number}", kind="audio", source_id="edit-source",
                    span=span, track_index=2, av_link_id=f"av{segment.number}",
                ),
            )
        )
    items.extend(
        EditPlanItem0C(
            item_id=f"st{segment.number}", kind="subtitle", source_id="edit-source",
            span=SourceFrameSpan(
                start_frame=segment.start, end_frame=segment.end, rate=RATE
            ),
            track_index=3, subtitle_text=f"章タイトル用の字幕{segment.number}",
        )
        for segment in segments
    )
    return EditPlan0C(
        artifact_id=f"edit-plan-review-{EPISODE_ID}", artifact_type="edit_plan_0c",
        schema_version="edit-plan-0c-v1", content_hash="0" * 64,
        producer=Producer(name="chapter-title-test", version="1"), inputs=(), frame_rate=RATE,
        plan=EditPlanBody0C(
            plan_version="v1",
            edit_source=EditSourceRef0C(source_id="edit-source", total_frames=8100),
            items=tuple(items),
        ),
    )


def write_runtime(root: Path, spec: RuntimeSpec = DEFAULT_RUNTIME_SPEC) -> Path:
    pins = root / "config" / "toolchains" / "pins"
    pins.mkdir(parents=True, exist_ok=True)
    atomic_write(
        pins / "editorial-director-v2.json",
        canonical_model_bytes(
            EditorialPinV2(
                schema_version="editorial-pin-v2", purpose=spec.purpose,
                model_id=spec.model_id, api_surface="codex-exec",
            )
        ),
    )
    for name in ("moment-review.json", "moment-specialist.json", "review-interpreter.json"):
        atomic_write(pins / name, b"{}")
    runtime = root / "config" / "editorial-runtime.json"
    atomic_write(
        runtime,
        canonical_model_bytes(
            EditorialRuntimeV1(
                schema_version="editorial-runtime-v1", mode=spec.mode,
                transport=spec.transport,
                director_pin_path="config/toolchains/pins/editorial-director-v2.json",
                moment_review_pin_path="config/toolchains/pins/moment-review.json",
                moment_review_specialist_pin_path="config/toolchains/pins/moment-specialist.json",
                review_interpreter_pin_path="config/toolchains/pins/review-interpreter.json",
            )
        ),
    )
    return runtime


def chapter_title_workspace(root: Path) -> tuple[Path, str]:
    episode = root / EPISODE_ID
    store = episode / "review" / "store"
    store.mkdir(parents=True)
    log = episode / "review" / "events.jsonl"
    initialize_store(_plan(), log, store)
    correction = CorrectSubtitleProposal0C(
        proposal_id="prop-chapter-v2", command_kind="correct_subtitle",
        base_plan_version="v1", actor_intent="model", sequence=1,
        confidence=Confidence0C(num=1, den=1),
        ambiguity=ProposalAmbiguity0C(status="clear"), evidence=(),
        target=ItemIdSelector0C(kind="item_id", item_id="st99"),
        new_text="章タイトル用の字幕九十九", language="ja",
    )
    commit_command(
        correction, OperatorDecision0C(decision_id="chapter-v2", actor_intent="operator"),
        log, store,
    )
    approval = approve_editorial_plan_proposal().model_copy(
        update={"proposal_id": "prop-chapter-v3", "base_plan_version": "v2", "sequence": 2}
    )
    commit_command(
        approval, OperatorDecision0C(decision_id="chapter-v3", actor_intent="operator"),
        log, store,
    )
    atomic_write(
        episode / "intake.json",
        canonical_model_bytes(
            IntakeRecordV1(
                episode_id=EPISODE_ID, source_folder="/private/not-read",
                brief_text="章タイトル候補", created_at="2026-09-02T00:00:00Z",
            )
        ),
    )
    for relative in ("previews/preview.mp4", "finishing/final-resolve-render/approved.mp4"):
        atomic_write(episode / relative, b"approved-render-bytes")
    return episode, sha256_file(store / "plan-v3.json")


def runner(titles: tuple[str, str, str] = TITLES) -> RecordingRunner:
    payload = json.dumps({"titles": titles}, ensure_ascii=False)
    return RecordingRunner(f"result\n```json\n{payload}\n```")


def protected_snapshot(root: Path) -> tuple[tuple[Path, bytes], ...]:
    files = tuple(
        sorted(
            path
            for parent in (root / "review", root / "previews", root / "finishing")
            for path in parent.rglob("*")
            if path.is_file()
        )
    )
    return tuple((path.relative_to(root), path.read_bytes()) for path in files)
