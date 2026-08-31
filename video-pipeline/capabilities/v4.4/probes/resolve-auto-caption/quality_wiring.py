# noqa: INP001 (evidence tree is not an importable package by design)
"""Services composition for Task 2: score the saved private r1/r2 readbacks.

The SAME path the Whisper readiness measurement used — sample load +
pairing + scoring via ``services.cli._v44_jp_metrics`` (proper nouns
reference-only), thresholds and gate decision via
``services.cli._v44_arm_transcript`` (imported, never redeclared). Only the
hypothesis side differs: Resolve cues under the plan-frozen rule.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable

    from services.metrics.v44_product_proof import EvidenceQualityMetrics

EVIDENCE = Path(__file__).resolve().parent
VIDEO_PIPELINE = EVIDENCE.parents[3]
REPO = VIDEO_PIPELINE.parent
for _path in (VIDEO_PIPELINE, EVIDENCE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import caption_logic as caption  # noqa: E402 (path bootstrap first — task4 pattern)
import quality_logic as qlogic  # noqa: E402 (path bootstrap first)

from services.cli._v44_arm_transcript import (  # noqa: E402 (path bootstrap first)
    CER_MAX,
    DUPLICATED_MAX,
    OMITTED_MAX,
    TIMESTAMP_P95_MS_MAX,
    enforce_system_asr_thresholds,
)
from services.cli._v44_jp_metrics import (  # noqa: E402 (path bootstrap first)
    TranscriptSegment,
    _normalize,
    compute_evidence_quality,
    hypothesis_proper_nouns,
    load_transcript_sample,
    utterance_diffs_ms,
)
from services.cli._v44_subtitle_build import merged_proper_nouns  # noqa: E402
from services.cli.v44_arm_stages import ArmPipelineError  # noqa: E402

EPISODE_ROOT = REPO / "private" / "reference-episodes" / "v44-real-01"
SAMPLE_PATH = EPISODE_ROOT / "transcript-sample-corrected.json"
PROPER_NOUNS_PATH = EPISODE_ROOT / "proper-nouns.json"
RUNS_DIR = EPISODE_ROOT / "runs"
RUNS: Final = ("r1", "r2")
REPORT_NAME: Final = "quality-metrics.json"
PROVIDER_ID: Final = "resolve-auto-caption"
THRESHOLD_SOURCE: Final = "services.cli._v44_arm_transcript"
THRESHOLD_LIMITS = qlogic.ThresholdLimits(
    cer_max=CER_MAX, timestamp_p95_ms_max=TIMESTAMP_P95_MS_MAX,
    omitted_max=OMITTED_MAX, duplicated_max=DUPLICATED_MAX,
)


@dataclass(frozen=True, slots=True)
class RunFacts:
    """One validated private readback + its committed capability record."""

    run: str
    canonical_sha256: str
    timeline_start_frame: int
    input_media_sha256: str
    resolve_version: str
    mcp_version: str
    pin_commit: str
    triples: tuple[qlogic.MsTriple, ...]


def _read_json(path: Path, label: str) -> dict[str, object]:
    if not path.is_file():
        raise qlogic.QualityRefusalError("readback-missing", f"{label} missing")
    try:
        payload: object = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError):
        raise qlogic.QualityRefusalError(
            "readback-unreadable", f"{label} unreadable") from None
    if not isinstance(payload, dict):
        raise qlogic.QualityRefusalError(
            "readback-unreadable", f"{label} not an object")
    return payload


def load_run(run: str) -> RunFacts:
    """Load one private readback and validate it against its capability JSON."""
    if run not in RUNS:
        raise qlogic.QualityRefusalError("readback-invalid", "run not in closed set")
    run_dir = RUNS_DIR / f"resolve-auto-caption-{run}"
    payload = _read_json(run_dir / "readback.json", "readback")
    rows = payload.get("cues")
    if not isinstance(rows, list):
        raise qlogic.QualityRefusalError("readback-invalid", "readback cues not a list")
    try:
        cues, canonical_sha = caption.canonical_cues(rows)
    except caption.ProbeRefusalError as error:
        raise qlogic.QualityRefusalError("readback-invalid", error.detail) from error
    context = _read_json(run_dir / "readback-context.json", "readback-context")
    raw_start = context.get("timeline_start_frame")
    if not isinstance(raw_start, int) or isinstance(raw_start, bool):
        raise qlogic.QualityRefusalError(
            "timeline-start-invalid", "context timeline start not integer")
    qlogic.verify_hash(canonical_sha, str(context.get("canonical_sha256_absolute")),
                       "readback-context")
    capability = _read_json(EVIDENCE / f"capability-{run}.json", f"capability-{run}")
    readback_block = capability.get("readback")
    if not isinstance(readback_block, dict):
        raise qlogic.QualityRefusalError("capability-mismatch", "capability readback block missing")
    qlogic.verify_hash(canonical_sha, str(readback_block.get("canonical_sha256")),
                       f"capability-{run}")
    if capability.get("timeline_start_frame") != raw_start:
        raise qlogic.QualityRefusalError(
            "timeline-start-mismatch", f"capability-{run} timeline start differs")
    return RunFacts(
        run=run, canonical_sha256=canonical_sha, timeline_start_frame=raw_start,
        input_media_sha256=str(capability.get("input_media_sha256", "")),
        resolve_version=str(capability.get("resolve_version", "")),
        mcp_version=str(capability.get("mcp_version", "")),
        pin_commit=str(capability.get("pin_commit", "")),
        triples=tuple(qlogic.convert_cues(cues, raw_start)),
    )


def _cross_run_checks(first: RunFacts, second: RunFacts) -> None:
    if first.timeline_start_frame != second.timeline_start_frame:
        raise qlogic.QualityRefusalError("timeline-start-mismatch", "r1/r2 timeline start differs")
    if (not first.input_media_sha256 or first.input_media_sha256 != second.input_media_sha256):
        raise qlogic.QualityRefusalError("input-mismatch", "r1/r2 input media sha differs")
    if (first.resolve_version, first.mcp_version, first.pin_commit) != (
            second.resolve_version, second.mcp_version, second.pin_commit):
        raise qlogic.QualityRefusalError("capability-mismatch", "r1/r2 versions differ")
    qlogic.verify_hash(second.canonical_sha256, first.canonical_sha256, "r2-vs-r1")
    qlogic.stability_check(first.triples, second.triples)


def evaluate_thresholds(
    metrics: EvidenceQualityMetrics,
    enforce: Callable[[EvidenceQualityMetrics], None] = enforce_system_asr_thresholds,
) -> tuple[dict[str, str], str]:
    """Per-metric labels from the imported constants + the production gate
    (its typed refusal label ``asr-alignment-failed`` is recorded verbatim)."""
    values = qlogic.MetricValues(
        cer=metrics.transcript_cer, p95_ms=metrics.timestamp_error_p95_ms,
        omitted=metrics.omitted_utterances, duplicated=metrics.duplicated_utterances)
    checks = qlogic.gate_checks(values, THRESHOLD_LIMITS)
    decision = "thresholds-passed"
    try:
        enforce(metrics)
    except ArmPipelineError:
        decision = "asr-alignment-failed"
    return checks, decision


def measure() -> tuple[dict[str, object], list[str]]:
    """Score r1 (r2 must agree exactly); write the sanitized report."""
    first, second = load_run("r1"), load_run("r2")
    _cross_run_checks(first, second)
    sample, sample_sha = load_transcript_sample(SAMPLE_PATH)
    dictionary = merged_proper_nouns(PROPER_NOUNS_PATH)
    hypothesis = tuple(
        TranscriptSegment(start_ms=start, end_ms=end, text=text)
        for start, end, text in first.triples)
    hypothesis_text = "".join(text for _, _, text in first.triples)
    metrics = compute_evidence_quality(
        corrected=sample, hypothesis_segments=hypothesis,
        hypothesis_proper_nouns=hypothesis_proper_nouns(
            hypothesis_text, sample, dictionary),
        timestamp_diffs_ms=[
            float(diff) for diff in utterance_diffs_ms(hypothesis, sample.segments)],
    )
    checks, decision = evaluate_thresholds(metrics)
    reference_triples = tuple(
        (s.start_ms, s.end_ms, s.text) for s in sample.segments)
    diagnosis = qlogic.diagnose_segmentation(
        first.triples, reference_triples, _normalize)
    if (diagnosis["omitted_total"] != metrics.omitted_utterances
            or diagnosis["duplicated_total"] != metrics.duplicated_utterances):
        raise qlogic.QualityRefusalError(
            "diagnosis-inconsistent", "diagnosis counts differ from metrics")
    report: dict[str, object] = {
        "schema_version": "resolve-auto-caption-quality-v1",
        "provider": PROVIDER_ID, "runs": list(RUNS), "stable": True,
        "readback": {
            "item_count": len(first.triples),
            "r1_canonical_sha256": first.canonical_sha256,
            "r2_canonical_sha256": second.canonical_sha256,
            "timeline_start_frame": first.timeline_start_frame,
            "input_media_sha256": first.input_media_sha256,
        },
        "reference": {
            "sample_sha256": sample_sha, "segment_count": len(sample.segments),
            "proper_noun_count": len(sample.proper_nouns)},
        "versions": {
            "resolve_version": first.resolve_version, "mcp_version": first.mcp_version,
            "pin_commit": first.pin_commit},
        "metrics": {
            "transcript_cer": metrics.transcript_cer,
            "timestamp_error_p95_ms": metrics.timestamp_error_p95_ms,
            "omitted_utterances": metrics.omitted_utterances,
            "duplicated_utterances": metrics.duplicated_utterances,
        },
        "reference_only": {"proper_noun_recall": metrics.proper_noun_recall},
        "thresholds": {
            "source": THRESHOLD_SOURCE, "cer_max": CER_MAX,
            "timestamp_p95_ms_max": TIMESTAMP_P95_MS_MAX, "omitted_max": OMITTED_MAX,
            "duplicated_max": DUPLICATED_MAX},
        "threshold_checks": checks, "threshold_decision": decision,
        "diagnosis_counts": diagnosis,
    }
    qlogic.assert_quality_sanitized(report)
    try:
        (EVIDENCE / REPORT_NAME).write_text(
            json.dumps(report, indent=1, sort_keys=True) + "\n")
    except OSError:
        raise qlogic.QualityRefusalError("write-error", "report write failed") from None
    lines = [
        f"provider={PROVIDER_ID} runs=r1,r2 stable=true",
        (f"readback: items={len(first.triples)} "
         f"timeline_start_frame={first.timeline_start_frame}"),
        (f"reference: segments={len(sample.segments)} "
         f"proper_nouns={len(sample.proper_nouns)}"),
        (f"metrics: cer={metrics.transcript_cer} "
         f"p95_ms={metrics.timestamp_error_p95_ms} "
         f"omitted={metrics.omitted_utterances} "
         f"duplicated={metrics.duplicated_utterances} "
         f"pn_recall_ref_only={metrics.proper_noun_recall}"),
        f"thresholds source={THRESHOLD_SOURCE}",
        "threshold checks: " + " ".join(f"{key}={value}" for key, value in checks.items()),
        f"threshold decision: {decision}",
        *qlogic.diagnosis_lines(diagnosis),
        f"readback sha256: r1={first.canonical_sha256} r2={second.canonical_sha256}",
        f"sample sha256: {sample_sha}", f"wrote: {REPORT_NAME}",
    ]
    return report, lines


__all__ = [
    "REPORT_NAME", "THRESHOLD_LIMITS", "THRESHOLD_SOURCE", "RunFacts", "evaluate_thresholds",
    "load_run", "measure",
]
