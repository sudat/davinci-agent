# allow: SIZE_OK — single-concept evidence module: the episode readers and
# the derivation they feed are one seam (splitting would separate the
# evidence lines from the loader that cites them); quality_domains precedent.
"""Episode-grounded domain evidence (PRD 12.2, deterministic slice 1).

Loads the concrete episode artifacts that carry domain evidence — the
episode protocol's ``title_intent``, the runtime chapter-title proposal,
the runtime operator domain-decisions record (the minimal not-needed
input path), and the conform ``normalize-record`` content — and derives
``QualityFactsV1`` from them. No LLM: every line cites a file that
exists. The operator decision record follows the chapter-title-proposal
runtime precedent (a runtime record, not a committed artifact family).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final, Literal

from services.creative_plan.quality_domains import (
    DomainNotNeededDecisionV1,
    QualityFactsV1,
)

EPISODE_PROTOCOL_NAME: Final = "episode.json"
PROTOCOL_SCHEMA: Final = "v44-episode-protocol-v1"
CHAPTER_PROPOSAL_RELATIVE: Final = ("runtime", "chapter-title-proposal.json")
DOMAIN_DECISIONS_RELATIVE: Final = ("runtime", "domain-decisions.json")
NORMALIZE_RECORD_RELATIVE: Final = ("run", "normalize-record.json")
TIMELINE_1080P: Final[tuple[int, int]] = (1920, 1080)

ConformScaleStatus = Literal["fit_performed", "fit_not_performed", "no_geometry_mismatch"]

_CSV_FIELDS: Final = 2


class DomainEvidenceError(ValueError):
    """Typed refusal: a present evidence file is unreadable/invalid."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class EpisodeDomainEvidence:
    """Every deterministic evidence fact the derivation consumes."""

    episode_id: str
    title_intent: str | None = None
    chapter_proposal_present: bool = False
    chapter_boundary_count: int = 0
    ir_graphics_items: int = 0
    operator_not_needed: tuple[DomainNotNeededDecisionV1, ...] = ()
    conform_filters: str | None = None
    conform_record_present: bool = False
    mezzanine_geometry: tuple[int, int] | None = None
    timeline_geometry: tuple[int, int] = TIMELINE_1080P
    cue_count: int = 0
    extras: tuple[str, ...] = field(default=())


def probe_geometry(mezzanine: Path, ffprobe_bin: Path) -> tuple[int, int] | None:
    """ffprobe the mezzanine's video geometry; None when unverifiable."""

    import subprocess  # noqa: PLC0415

    try:
        result = subprocess.run(
            (str(ffprobe_bin), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(mezzanine)),
            capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split(",")
    if len(parts) != _CSV_FIELDS:
        return None
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return None


def _read_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DomainEvidenceError(
            f"{label}-invalid", f"cannot read {path}: {error}") from error


def _filters_from_normalize(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    argv = payload.get("argv")
    if not isinstance(argv, list):
        return None
    for index, token in enumerate(argv):
        if token in ("-vf", "-filter:v") and index + 1 < len(argv):
            return str(argv[index + 1])
    return None


def _decisions_from_record(
    payload: object, episode_id: str
) -> tuple[DomainNotNeededDecisionV1, ...]:
    if not isinstance(payload, dict):
        return ()
    rows = payload.get("decisions")
    if not isinstance(rows, list):
        return ()
    parsed = [
        DomainNotNeededDecisionV1.model_validate(row) for row in rows
    ]
    if payload.get("episode_id") not in (None, episode_id):
        raise DomainEvidenceError(
            "domain-decisions-episode-mismatch",
            f"decisions episode {payload.get('episode_id')!r} != {episode_id!r}")
    return tuple(parsed)


def _protocol_payload(
    episode_root: Path, protocol_path: Path | None
) -> dict[str, object] | None:
    """Strict when the operator named the file; lenient schema-guarded
    discovery otherwise (a cockpit chain manifest sharing the filename must
    not masquerade as the protocol)."""

    if protocol_path is not None:
        if not protocol_path.is_file():
            raise DomainEvidenceError(
                "protocol-missing", f"episode protocol absent: {protocol_path}")
        payload = _read_json(protocol_path, "episode-protocol")
        if not isinstance(payload, dict) or payload.get("schema_version") != PROTOCOL_SCHEMA:
            raise DomainEvidenceError(
                "protocol-schema-mismatch",
                f"{protocol_path} is not a {PROTOCOL_SCHEMA} document")
        return payload
    candidate = episode_root / EPISODE_PROTOCOL_NAME
    if not candidate.is_file():
        return None
    discovered = _read_json(candidate, "episode-protocol")
    if isinstance(discovered, dict) and discovered.get("schema_version") == PROTOCOL_SCHEMA:
        return discovered
    return None


def load_episode_domain_evidence(  # noqa: PLR0913 (the episode inputs are irreducible read facts)
    episode_root: Path,
    *,
    protocol_path: Path | None = None,
    episode_id: str | None = None,
    ir_graphics_items: int = 0,
    mezzanine_geometry: tuple[int, int] | None = None,
    timeline_geometry: tuple[int, int] = TIMELINE_1080P,
    cue_count: int = 0,
) -> EpisodeDomainEvidence:
    """Read the episode's evidence files; missing files are absent evidence.

    ``mezzanine_geometry`` is injected by callers that already probed the
    mezzanine (the finishing run holds the path); ``None`` leaves geometry
    unknown, which the derivation treats honestly as unverified.
    """

    title_intent: str | None = None
    resolved_id = episode_id
    protocol = _protocol_payload(episode_root, protocol_path)
    if protocol is not None:
        raw_title = protocol.get("title_intent")
        title_intent = raw_title if isinstance(raw_title, str) else None
        raw_id = protocol.get("episode_id")
        resolved_id = resolved_id or (
            raw_id if isinstance(raw_id, str) and raw_id else None)
    chapter_path = episode_root.joinpath(*CHAPTER_PROPOSAL_RELATIVE)
    chapter_present = False
    boundary_count = 0
    if chapter_path.is_file():
        chapter = _read_json(chapter_path, "chapter-title-proposal")
        if isinstance(chapter, dict) and chapter.get("boundary") is not None:
            chapter_present = True
            boundary_count = 1
    decisions: tuple[DomainNotNeededDecisionV1, ...] = ()
    decisions_path = episode_root.joinpath(*DOMAIN_DECISIONS_RELATIVE)
    if decisions_path.is_file():
        decisions = _decisions_from_record(
            _read_json(decisions_path, "domain-decisions"), resolved_id or "")
    conform_filters: str | None = None
    conform_present = False
    normalize_path = episode_root.joinpath(*NORMALIZE_RECORD_RELATIVE)
    if normalize_path.is_file():
        conform_present = True
        conform_filters = _filters_from_normalize(
            _read_json(normalize_path, "normalize-record"))
    return EpisodeDomainEvidence(
        episode_id=resolved_id or "",
        title_intent=title_intent,
        chapter_proposal_present=chapter_present,
        chapter_boundary_count=boundary_count,
        ir_graphics_items=ir_graphics_items,
        operator_not_needed=decisions,
        conform_filters=conform_filters,
        conform_record_present=conform_present,
        mezzanine_geometry=mezzanine_geometry,
        timeline_geometry=timeline_geometry,
        cue_count=cue_count,
    )


def conform_scale_status(
    *, filters: str | None, mezzanine: tuple[int, int] | None,
    timeline: tuple[int, int],
) -> ConformScaleStatus:
    """Content check over the conform record: applied ONLY on proof.

    Fit is performed only when the record's filter chain carries an
    explicit scale and the record's output geometry equals the timeline's.
    A crop (any kind — it achieves geometry by discarding content), a bare
    rate conversion, an absent record, or unknown geometry never count as
    performed (PRD 12.2: fabricating ``applied`` is forbidden).
    """

    chain = filters or ""
    if mezzanine is None:
        return "fit_not_performed"
    if mezzanine != timeline:
        return "fit_not_performed"
    if "crop=" in chain:
        return "fit_not_performed"
    if "scale=" in chain:
        return "fit_performed"
    return "no_geometry_mismatch"


type _Derivation = tuple[bool, tuple[str, ...], tuple[str, ...]]


def _graphics_derivation(evidence: EpisodeDomainEvidence) -> _Derivation:
    lines: list[str] = []
    needed = False
    if evidence.title_intent and evidence.title_intent.strip():
        title = evidence.title_intent[:24]
        lines.append(f"episode-protocol: title_intent present ({title}…)")
        needed = True
    else:
        lines.append("episode-protocol: no title_intent")
    if evidence.chapter_proposal_present:
        lines.append(
            f"chapter-title-proposal: {evidence.chapter_boundary_count} boundary "
            "(chapter graphic material)")
        needed = True
    else:
        lines.append("runtime: no chapter-title-proposal")
    lines.append(
        f"committed subtitle cues: {evidence.cue_count} (on-screen text idiom; "
        "the subtitle domain owns this evidence)")
    lines.extend(evidence.extras)
    if evidence.extras:
        needed = True
    committed: tuple[str, ...] = (
        (f"timeline-ir-v2: {evidence.ir_graphics_items} graphic/still items",)
        if evidence.ir_graphics_items > 0
        else ()
    )
    if evidence.ir_graphics_items == 0:
        lines.append("committed IR: no graphic/still items")
    return needed, tuple(lines), committed


def _framing_derivation(evidence: EpisodeDomainEvidence) -> _Derivation:
    mezz = evidence.mezzanine_geometry
    timeline = evidence.timeline_geometry
    if mezz is None:
        record_note = (
            "normalize-record absent (no conform evidence)"
            if not evidence.conform_record_present
            else f"normalize-record filters '{evidence.conform_filters}'"
        )
        return True, (record_note, "mezzanine geometry unverified (mezzanine unavailable)"), ()
    scale = conform_scale_status(
        filters=evidence.conform_filters, mezzanine=mezz, timeline=timeline)
    if scale == "fit_performed":
        note = (
            f"normalize-record: scale-to-fit performed ({evidence.conform_filters}) "
            f"-> mezzanine {mezz[0]}x{mezz[1]} == timeline"
        )
        return True, (note,), (note,)
    if scale == "no_geometry_mismatch":
        note = (
            f"mezzanine {mezz[0]}x{mezz[1]} geometry matches timeline "
            f"{timeline[0]}x{timeline[1]}; no reframing evidence needed"
        )
        return False, (note,), ()
    filters_note = (
        f"normalize-record filters '{evidence.conform_filters}' carry no scale-to-fit"
        if evidence.conform_record_present
        else "normalize-record absent (no conform evidence)"
    )
    geometry_note = (
        f"mezzanine {mezz[0]}x{mezz[1]} != timeline {timeline[0]}x{timeline[1]}"
    )
    return True, (filters_note, geometry_note), ()


def derive_domain_facts(  # noqa: PLR0913 (the run-report inputs the §12.2 facts need)
    evidence: EpisodeDomainEvidence,
    *,
    episode_id: str,
    head_version: int,
    cue_count: int,
    run_report: object,
    qc_verdict_passed: bool,
) -> QualityFactsV1:
    """Deterministic §12.2 facts: every need line cites a read artifact."""

    evidence = replace(evidence, cue_count=cue_count)
    graphics_intended, graphics_basis, graphics_committed = _graphics_derivation(evidence)
    framing_intended, framing_basis, framing_committed = _framing_derivation(evidence)
    delivery_passed = (
        run_report is not None
        and getattr(run_report, "outcome", None) == "completed"
        and qc_verdict_passed
    )
    return QualityFactsV1(
        episode_id=episode_id,
        has_dialogue=cue_count > 0,
        editorial_blocking_defect=False,
        editorial_evidence=(
            f"review-store head v{head_version}: committed edit plan + IR",
            f"committed subtitle cues: {cue_count}",
        ),
        framing_motion_intended=framing_intended,
        framing_motion_evidence=framing_basis,
        framing_motion_committed_evidence=framing_committed,
        graphics_intended=graphics_intended,
        graphics_evidence=graphics_basis,
        graphics_committed_evidence=graphics_committed,
        operator_not_needed=evidence.operator_not_needed,
        delivery_qc_passed=delivery_passed,
        delivery_qc_evidence=(
            (
                "mcp-execution-run-report-v1: outcome=completed",
                "qc-report-v1: verdict=passed",
            )
            if delivery_passed
            else ()
        ),
    )


__all__ = [
    "TIMELINE_1080P",
    "ConformScaleStatus",
    "DomainEvidenceError",
    "EpisodeDomainEvidence",
    "conform_scale_status",
    "derive_domain_facts",
    "load_episode_domain_evidence",
    "probe_geometry",
]
