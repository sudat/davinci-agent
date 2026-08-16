"""Stdout contract and evidence-file writing for the fixed-presentation spike."""

from __future__ import annotations

import json
from pathlib import Path

from services.foundation_io import atomic_write, canonical_model_bytes
from services.resolve_bridge.fixed_presentation_models import (
    FFPROBE_NAME,
    MARKER,
    READBACK_NAME,
    REPORT_NAME,
    SHA_NAME,
    STRATEGY_NAME,
    FixedPresentationReport,
    SlateEvidence,
)


def slate_status(row: SlateEvidence) -> str:
    return "verified" if row.media_ok and row.span_ok and row.link_ok else "partial"


def stdout_lines(report: FixedPresentationReport, render_summary: str) -> str:
    subtitle_row = report.subtitle.strategy
    in_timeline = str(report.subtitle.in_timeline).lower()
    lines = [
        (
            f"{MARKER} element=subtitle strategy={subtitle_row.strategy} "
            f"status={subtitle_row.status} placement=in-timeline:{in_timeline}"
        ),
        *(
            (
                f"{MARKER} ladder rung={row.rung} "
                f"available={str(row.available).lower()} reason={row.reason}"
            )
            for row in subtitle_row.ladder
        ),
        *(
            (
                f"{MARKER} element={row.item_id} "
                f"status={slate_status(row)} "
                f"media-match={str(row.media_ok).lower()} "
                f"duration-frames={row.duration_frames} link={str(row.link_ok).lower()}"
            )
            for row in report.slates
        ),
    ]
    if report.subtitle.artifact is not None:
        artifact = report.subtitle.artifact
        lines.append(
            f"{MARKER} subtitle-artifact paired={artifact.paired_path} "
            f"sha256={artifact.paired_sha256} cue={artifact.cue.text!r} "
            f"pts={artifact.packet_pts_seconds} duration={artifact.packet_duration_seconds}"
        )
    audio_row = next(row for row in report.strategy_table if row.element == "audio-preset")
    lines.append(f"{MARKER} element=audio-preset strategy=direct status={audio_row.status}")
    lines.append(
        f"{MARKER} render status={report.render.status} {render_summary} "
        f"sha256={report.render.output_sha256}"
    )
    lines.extend(
        f"{MARKER} mismatch code={row.code} detail={row.detail}" for row in report.mismatches
    )
    verdict = "PASS" if report.passed else "FAIL"
    lines.append(f"{MARKER} {verdict} fixture={report.fixture_id}")
    return "\n".join(lines) + "\n"


def write_evidence(evidence: Path, report: FixedPresentationReport) -> None:
    evidence.mkdir(parents=True, exist_ok=True)
    atomic_write(evidence / REPORT_NAME, canonical_model_bytes(report))
    table = {
        row.element: {
            "strategy": row.strategy,
            "status": row.status,
            "reason": row.reason,
            "limitations": row.limitations,
        }
        for row in report.strategy_table
    }
    atomic_write(evidence / STRATEGY_NAME, json.dumps(table, indent=2, sort_keys=True).encode())
    readback = {
        "base_cut_passed": report.base_cut_passed,
        "slates": [row.model_dump(mode="json") for row in report.slates],
        "render": report.render.model_dump(mode="json"),
    }
    atomic_write(evidence / READBACK_NAME, json.dumps(readback, indent=2, sort_keys=True).encode())
    probe_payload = json.dumps(
        report.render.report.model_dump(mode="json"), indent=2, sort_keys=True
    ).encode()
    atomic_write(evidence / FFPROBE_NAME, probe_payload)
    sha_line = f"{report.render.output_sha256}  {report.render.output_path}\n".encode()
    atomic_write(evidence / SHA_NAME, sha_line)
