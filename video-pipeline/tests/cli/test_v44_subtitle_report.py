"""Behavior lock: subtitle proof report models coerce JSON arrays to tuples.

``StrictModel`` runs strict, so the array-valued fields of a JSON-shaped
report payload (``qc_rule_ids`` on the subtitle block, ``notes`` on the
report) are rejected unless their ``BeforeValidator`` coerces them to
tuples first. Locks that coercion ahead of consolidating the duplicated
``_to_tuple`` helpers into ``services.contracts.primitives``.
"""

from __future__ import annotations

from services.cli._v44_subtitle_report import SubtitleProofReport

_SHA = "0" * 64  # the Sha256 primitive wants exactly 64 lowercase hex chars


def test_report_payload_coerces_json_arrays_to_tuples() -> None:
    # given: a JSON-shaped report payload — qc_rule_ids and notes are lists
    payload = {
        "schema_version": "v44-subtitle-proof-v1",
        "episode_id": "ep-lock",
        "evidence_verdict": "measured",
        "subtitle_verdict": "issues-found",
        "asr": {
            "mode": "replay",
            "artifact_path": "jobs/ep-lock/asr/transcript-artifact.json",
            "artifact_sha256": _SHA,
            "segment_count": 3,
            "skipped_empty_segments": 0,
        },
        "evidence": {
            "metrics_source": "services.metrics.v44_product_proof",
            "transcript_cer": 0.12,
            "proper_noun_recall": 1.0,
            "omitted_utterances": 0,
            "duplicated_utterances": 0,
        },
        "subtitle": {
            "cue_count": 3,
            "matrix_status": "accepted",
            "capability_path": "capabilities/v4.3/direct/subtitle.json",
            "snippet_render": "external_srt",
            "snippet_path": "jobs/ep-lock/out/preview-snippet.srt",
            "snippet_sha256": _SHA,
            "qc_issue_count": 1,
            "qc_rule_ids": ["subtitle_max_chars"],
            "reading_speed_violations": 0,
            "legibility_note": "3 cues; max 2 line(s)/cue and 13 chars/line",
        },
        "sample": {
            "path": "tests/fixtures/v44/sample-corrected.json",
            "sha256": _SHA,
            "entries": 3,
            "proper_nouns": 2,
        },
        "notes": ["replay artifact: transcript-artifact.json", "proof run locked"],
    }

    # when: the strict model validates the payload
    report = SubtitleProofReport.model_validate(payload)

    # then: both array fields landed as tuples (strict mode rejects the lists)
    assert report.subtitle.qc_rule_ids == ("subtitle_max_chars",)
    assert isinstance(report.subtitle.qc_rule_ids, tuple)
    assert report.notes == (
        "replay artifact: transcript-artifact.json",
        "proof run locked",
    )
    assert isinstance(report.notes, tuple)
