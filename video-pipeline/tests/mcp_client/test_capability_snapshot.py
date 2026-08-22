"""Offline verification of the committed Gate V43-0 artifacts (task 11).

These tests run WITHOUT ``-m mcp_live``: they verify the PERSISTED evidence
chain — matrix → snapshot → probe logs — stays mutually consistent after
any post-capture edit.  The defect class they lock out: touching
``mcp-fit.json`` after the snapshot was captured (or a probe log after the
matrix referenced it) must fail loudly here, not surface as a silent hash
drift in a later gate.
"""

from __future__ import annotations

import hashlib
import json
import re

from services.toolchain.mcp_fit import EXPECTED_ROW_COUNT, load_mcp_fit
from tests.mcp_client.live_support import (
    MCP_FIT_PATH,
    PROBES_DIR,
    SNAPSHOT_PATH,
    VIDEO_PIPELINE_ROOT,
    matrix_snapshot_hash,
)

_EVIDENCE_ANCHOR: re.Pattern[str] = re.compile(r"^([^#]+)#sha256=([0-9a-f]{64})$")


def _matrix_rows() -> list[dict[str, object]]:
    matrix = load_mcp_fit(MCP_FIT_PATH)
    rows = matrix["capabilities"]
    assert isinstance(rows, list)
    return rows


def test_snapshot_hash_matches_final_matrix_canonical_bytes() -> None:
    snapshot = json.loads(SNAPSHOT_PATH.read_bytes())
    assert snapshot["schema_version"] == "mcp-capability-snapshot-v1"
    assert snapshot["capability_snapshot_hash"] == matrix_snapshot_hash(
        MCP_FIT_PATH.read_bytes()
    )


def test_every_row_evidence_anchor_matches_probe_log_bytes() -> None:
    rows = _matrix_rows()
    assert len(rows) == EXPECTED_ROW_COUNT
    for row in rows:
        refs = row["evidence_refs"]
        assert isinstance(refs, list), f"{row['capability']}: evidence_refs not a list"
        assert refs, f"{row['capability']}: no evidence"
        match = _EVIDENCE_ANCHOR.match(str(refs[0]))
        assert match is not None, f"{row['capability']}: malformed anchor {refs[0]!r}"
        relative, digest = match.groups()
        log_path = VIDEO_PIPELINE_ROOT / relative
        assert log_path.is_file(), f"{row['capability']}: missing probe log {relative}"
        assert hashlib.sha256(log_path.read_bytes()).hexdigest() == digest, (
            f"{row['capability']}: probe log bytes changed after matrix capture"
        )


def test_filled_rows_carry_verdict_fallback_and_readback() -> None:
    for row in _matrix_rows():
        status = row["status"]
        readback = row["readback"]
        assert status in {"accepted", "failed", "partial", "not_available"}
        assert isinstance(readback, str), f"{row['capability']}: readback not a string"
        assert readback, f"{row['capability']}: empty readback"
        if status in {"accepted", "failed", "partial"}:
            assert row["fallback"] in {"legacy_direct", "template_external", "manual"}, (
                f"{row['capability']}: missing fallback for status={status}"
            )


def test_probe_summary_and_matrix_statuses_agree() -> None:
    summary_path = PROBES_DIR / "summary.json"
    summary = json.loads(summary_path.read_bytes())
    summary_by_capability = {entry["capability"]: entry["status"] for entry in summary}
    matrix_by_capability = {row["capability"]: row["status"] for row in _matrix_rows()}
    assert summary_by_capability == matrix_by_capability
