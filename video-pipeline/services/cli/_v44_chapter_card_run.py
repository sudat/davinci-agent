"""Chapter-card insertion run: orchestrates gates, render, proofs, publication."""

from __future__ import annotations

import shutil
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.cli import _v44_chapter_card_files as files
from services.cli._v44_chapter_card_evidence import EvidenceRequest, build_evidence
from services.cli._v44_chapter_card_gates import (
    ChapterCardInsertError,
    audio_proof,
    common_root,
    hash_protected,
    protected_paths,
    require_master_facts,
    require_plan_v3,
    require_protection_unchanged,
    require_source_pcm_canonical,
)
from services.cli._v44_chapter_card_media import decode_pcm_s16le
from services.cli._v44_chapter_card_pair_qa import verify_source_pairs
from services.cli._v44_chapter_card_plan import (
    CARD_FRAMES,
    FPS,
    RECORD_FRAME,
    VIDEO_BITRATE,
    VIDEO_TIMESCALE,
    build_output_pcm,
    load_approved_card,
)
from services.cli._v44_chapter_card_preflight import (
    FontFacts,
    require_approved_font,
    require_pins_current,
    require_source_video_intact,
)
from services.cli._v44_chapter_card_probe import probe_master_facts
from services.cli._v44_chapter_card_publish import Publication, publish
from services.cli._v44_chapter_card_qa import verify_card_span
from services.cli._v44_chapter_card_render import (
    MasterGeometry,
    SpliceInputs,
    render_master,
    write_card_raw,
)
from services.presentation.chapter_card import (
    CANVAS_H,
    CANVAS_W,
    ChapterCardError,
    ChapterCardFrame,
    render_chapter_card,
)

if TYPE_CHECKING:
    from services.cli._v44_chapter_card_plan import ApprovedCard
    from services.preview.tools import PinnedTools

MASTER_NAME: Final = "v44-real-01-theme-chapter-pcm.mov"
EVIDENCE_NAME: Final = "chapter-card-insertion-evidence.json"


@dataclass(frozen=True, slots=True)
class InsertionRequest:
    """The caller-supplied contract for one insertion run."""

    source: Path
    proposal: Path
    font: Path
    output_dir: Path
    episode_root: Path
    diag_finishing_root: Path
    protected_before: dict[str, str]


@dataclass(frozen=True, slots=True)
class _StagedRun:
    """Everything the staged render-and-prove phase needs, bound once."""

    tools: PinnedTools
    source: Path
    proposal: Path
    plan_v3: Path
    font: Path
    output_dir: Path
    episode_root: Path
    diag_finishing_root: Path
    staging: Path
    master: Path
    card: ApprovedCard
    font_facts: FontFacts
    frame: ChapterCardFrame
    subtitle_items: int
    source_pcm: bytes
    constructed: bytes
    pcm_file: Path
    card_raw: Path
    protected_before: dict[str, str]


def run_insertion(tools: PinnedTools, request: InsertionRequest) -> Path:
    """Render the approved master MOV, prove every gate, publish as one set.

    The master, evidence, and QA PNGs are staged in a run-unique exclusive
    directory; every gate and the protection check run against the staged set
    before the publication transaction replaces the prior set with
    exception-level rollback. A failed rerun never touches published bytes.
    """

    require_pins_current(tools)
    require_source_video_intact(request.source)
    card = load_approved_card(request.proposal)
    plan_v3 = request.episode_root / "review" / "store" / "plan-v3.json"
    subtitle_items = require_plan_v3(plan_v3)
    font_facts = require_approved_font(request.font)
    source_pcm = decode_pcm_s16le(tools, request.source)
    require_source_pcm_canonical(source_pcm)
    constructed = build_output_pcm(source_pcm)
    work = request.output_dir / "work"
    files.require_dir(work)
    pcm_file = work / "master.pcm"
    card_raw = work / "card.rgb"
    files.write_file(pcm_file, constructed)
    try:
        frame = render_chapter_card(card.title, font_path=request.font)
    except ChapterCardError as error:
        raise ChapterCardInsertError("card-render-failed", str(error)) from error
    write_card_raw(frame.image, frames=CARD_FRAMES, path=card_raw)
    master = request.output_dir / MASTER_NAME
    staging = files.exclusive_dir(request.output_dir, "staging-")
    run = _StagedRun(
        tools=tools, source=request.source, proposal=request.proposal,
        plan_v3=plan_v3, font=request.font, output_dir=request.output_dir,
        episode_root=request.episode_root,
        diag_finishing_root=request.diag_finishing_root, staging=staging,
        master=master, card=card, font_facts=font_facts, frame=frame,
        subtitle_items=subtitle_items, source_pcm=source_pcm, constructed=constructed,
        pcm_file=pcm_file, card_raw=card_raw,
        protected_before=request.protected_before,
    )
    try:
        _render_and_publish(run)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        for leftover in (card_raw, pcm_file):
            with suppress(OSError):
                leftover.unlink(missing_ok=True)
    return master


def _render_and_publish(run: _StagedRun) -> None:
    staged_master = run.staging / "master.mov"
    staged_evidence = run.staging / "evidence.json"
    staged_qa = run.staging / "qa"
    argv = render_master(
        run.tools,
        SpliceInputs(
            source=run.source,
            card_raw=run.card_raw,
            pcm=run.pcm_file,
            output=staged_master,
        ),
        MasterGeometry(
            width=CANVAS_W,
            height=CANVAS_H,
            fps=FPS,
            record_frame=RECORD_FRAME,
            card_frames=CARD_FRAMES,
            bitrate=VIDEO_BITRATE,
            video_timescale=VIDEO_TIMESCALE,
        ),
    )
    facts = probe_master_facts(run.tools, staged_master)
    require_master_facts(facts, staged_master)
    card_span = verify_card_span(
        run.tools,
        staged_master,
        record_frame=RECORD_FRAME,
        card_frames=CARD_FRAMES,
        qa_dir=staged_qa,
    )
    pairs = verify_source_pairs(
        run.tools, run.source, staged_master, qa_dir=staged_qa
    )
    proof = audio_proof(run.tools, staged_master, run.source_pcm, run.constructed)
    protected_after = hash_protected(
        protected_paths(
            episode_root=run.episode_root, diag_finishing_root=run.diag_finishing_root
        ),
        root=common_root(run.episode_root, run.diag_finishing_root),
    )
    require_protection_unchanged(run.protected_before, protected_after)
    evidence = build_evidence(
        EvidenceRequest(
            tools=run.tools,
            source=run.source,
            proposal=run.proposal,
            plan_v3=run.plan_v3,
            master=run.master,
            rendered_master=staged_master,
            mux_argv=argv,
            facts=facts,
            card_span=card_span,
            pairs=pairs,
            audio=proof,
            card=run.card,
            font=run.font,
            font_facts=run.font_facts,
            font_size_px=run.frame.font_size,
            subtitle_items=run.subtitle_items,
            protected_before=run.protected_before,
            protected_after=protected_after,
        )
    )
    files.write_synced(staged_evidence, evidence)
    publish(
        Publication(
            output_dir=run.output_dir,
            master=run.master,
            evidence=run.output_dir / EVIDENCE_NAME,
            qa_dir=run.output_dir / "qa",
            staged_master=staged_master,
            staged_evidence=staged_evidence,
            staged_qa=staged_qa,
        )
    )


__all__ = [
    "EVIDENCE_NAME",
    "MASTER_NAME",
    "ChapterCardInsertError",
    "InsertionRequest",
    "run_insertion",
]
