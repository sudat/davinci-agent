"""Task 59: legacy removal decision harness (report only — NO deletion).

Machine-judges the three removal conditions per legacy area
(impl-plan 1229-1235): MCP production use >= 3, fallback no longer
required, and no unique conformance/guard behavior. Unknown NEVER
recommends removal; deletion itself is out of scope by design.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.cli.legacy_report import main as legacy_cli_main
from services.cli.legacy_report import render_report_table
from services.foundation_io import canonical_model_bytes
from services.mcp_client.call_models import McpExecutionCallV1, append_call_record
from services.release.legacy_removal import (
    ConformanceGuardEntry,
    LedgerDirSource,
    LegacyCandidateV1,
    LegacyRemovalReportV1,
    evaluate_legacy_removal,
)
from services.toolchain.mcp_fit import load_mcp_fit

HEX = "a" * 64
ADAPTER = "resolve-adapter-package"
REAL_FIT = Path("capabilities/v4.3/mcp-fit.json")
REAL_LEDGER = "capabilities/v4.3/runs/probes/ledger"


def _call(tool: str, action: str, status: str = "ok") -> McpExecutionCallV1:
    return McpExecutionCallV1(
        provider_version="2.98.3",
        resolve_version="21.0.4.5",
        server_mode="compound",
        tool_name=tool,
        action=action,
        normalized_params_sha256=HEX,
        request_sha256=HEX,
        response_sha256=HEX,
        started_at=100,
        finished_at=100,
        status=status,  # type: ignore[arg-type]
    )


def _ledger_dir(tmp_path: Path, records: tuple[McpExecutionCallV1, ...]) -> str:
    directory = tmp_path / "ledger"
    for record in records:
        append_call_record(record, directory)
    return str(directory)


def _source(tmp_path: Path, records: tuple[McpExecutionCallV1, ...]) -> LedgerDirSource:
    return LedgerDirSource(kind="ledger_dir", path=_ledger_dir(tmp_path, records))


def _fit(*rows: dict[str, str]) -> dict[str, object]:
    return {
        "schema_version": "mcp-fit-v1",
        "capabilities": [
            {
                "capability": row["capability"],
                "status": row.get("status", "accepted"),
                "fallback": row.get("fallback", "legacy_direct"),
            }
            for row in rows
        ],
    }


ADAPTER_FIT = _fit(
    {"capability": "exact-source-range-placement"},
    {"capability": "source-record-readback"},
    {"capability": "render-configuration"},
)


def _adapter_guards() -> tuple[ConformanceGuardEntry, ...]:
    return (
        ConformanceGuardEntry(
            guard_id="pkg:exact-frame-span",
            candidate_id=ADAPTER,
            evidence_ref="tests/resolve_adapter/test_package.py",
            mcp_capability="exact-source-range-placement",
        ),
        ConformanceGuardEntry(
            guard_id="pkg:render-echo",
            candidate_id=ADAPTER,
            evidence_ref="tests/resolve_adapter/test_package.py",
            mcp_capability="render-configuration",
        ),
    )


def _real_source() -> LedgerDirSource:
    return LedgerDirSource(kind="ledger_dir", path=REAL_LEDGER)


def _four_use_records() -> tuple[McpExecutionCallV1, ...]:
    return (
        _call("media_pool", "append_to_timeline"),
        _call("media_pool", "append_to_timeline"),
        _call("timeline", "source_range_report"),
        _call("timeline", "source_range_report"),
    )


def _verdict(report: LegacyRemovalReportV1, candidate: str, index: int) -> str:
    entry = next(c for c in report.candidates if c.candidate_id == candidate)
    return entry.conditions[index].verdict


def test_all_favorable_fixture_is_remove_ready(tmp_path: Path) -> None:
    report = evaluate_legacy_removal(
        mcp_fit=ADAPTER_FIT,
        call_ledgers=(_source(tmp_path, _four_use_records()),),
        conformance_registry=_adapter_guards(),
    )
    entry = next(c for c in report.candidates if c.candidate_id == ADAPTER)
    assert entry.recommendation == "remove-ready"
    assert entry.mcp_production_use_count == 4
    assert entry.unique_guard_ids == ()
    assert _verdict(report, ADAPTER, 0) == "true"
    assert _verdict(report, ADAPTER, 1) == "true"
    assert _verdict(report, ADAPTER, 2) == "true"


def test_two_uses_is_not_ready_and_shows_false(tmp_path: Path) -> None:
    records = (
        _call("media_pool", "append_to_timeline"),
        _call("timeline", "source_range_report"),
        _call("render", "add_job"),  # a different area's action: not counted here
        _call("media_pool", "append_to_timeline", status="error"),  # not production use
    )
    report = evaluate_legacy_removal(
        mcp_fit=ADAPTER_FIT,
        call_ledgers=(_source(tmp_path, records),),
        conformance_registry=_adapter_guards(),
    )
    entry = next(c for c in report.candidates if c.candidate_id == ADAPTER)
    assert entry.mcp_production_use_count == 2
    assert entry.conditions[0].verdict == "false"
    assert entry.recommendation == "not-ready"


def test_failed_legacy_direct_fallback_is_not_ready(tmp_path: Path) -> None:
    fit = _fit(
        {"capability": "exact-source-range-placement"},
        {"capability": "source-record-readback"},
        {"capability": "render-configuration", "status": "failed", "fallback": "legacy_direct"},
    )
    report = evaluate_legacy_removal(
        mcp_fit=fit,
        call_ledgers=(_source(tmp_path, _four_use_records()),),
        conformance_registry=_adapter_guards(),
    )
    entry = next(c for c in report.candidates if c.candidate_id == ADAPTER)
    assert entry.conditions[1].verdict == "false"
    assert entry.fallback_required_capabilities == ("render-configuration",)
    assert entry.recommendation == "not-ready"


def test_missing_registry_entries_keep_guards_unknown_and_unrecommended(
    tmp_path: Path,
) -> None:
    report = evaluate_legacy_removal(
        mcp_fit=ADAPTER_FIT,
        call_ledgers=(_source(tmp_path, _four_use_records()),),
        conformance_registry=(),
    )
    entry = next(c for c in report.candidates if c.candidate_id == ADAPTER)
    assert entry.conditions[0].verdict == "true"
    assert entry.conditions[1].verdict == "true"
    assert entry.conditions[2].verdict == "unknown"
    assert entry.recommendation == "unknown"


def test_remove_ready_requires_all_three_conditions_true() -> None:
    base = {
        "candidate_id": "x",
        "label": "ラベル",
        "area_paths": ("services/x/*.py",),
        "area_files_found": (),
        "mcp_capabilities": ("cap-a",),
        "mcp_production_use_count": 5,
        "fallback_required_capabilities": (),
        "unique_guard_ids": (),
        "conditions": [
            {"condition": "mcp_production_use", "verdict": "true", "evidence": "ok_calls=5"},
            {"condition": "fallback_not_required", "verdict": "false", "evidence": "x"},
            {"condition": "no_unique_conformance_guards", "verdict": "true", "evidence": "y"},
        ],
    }
    with pytest.raises(ValidationError, match="recommendation"):
        LegacyCandidateV1.model_validate({**base, "recommendation": "remove-ready"})


def test_report_rejects_duplicate_candidate_ids() -> None:
    candidate = LegacyCandidateV1.model_validate(
        {
            "candidate_id": "x",
            "label": "ラベル",
            "area_paths": ("services/x/*.py",),
            "area_files_found": (),
            "mcp_capabilities": ("cap-a",),
            "mcp_production_use_count": 5,
            "fallback_required_capabilities": (),
            "unique_guard_ids": (),
            "conditions": [
                {"condition": "mcp_production_use", "verdict": "true", "evidence": "ok_calls=5"},
                {"condition": "fallback_not_required", "verdict": "true", "evidence": "x"},
                {"condition": "no_unique_conformance_guards", "verdict": "true", "evidence": "y"},
            ],
            "recommendation": "remove-ready",
        }
    )
    with pytest.raises(ValidationError):
        LegacyRemovalReportV1(candidates=(candidate, candidate))


def test_table_renders_japanese_headers_and_all_conditions(tmp_path: Path) -> None:
    report = evaluate_legacy_removal(
        mcp_fit=ADAPTER_FIT,
        call_ledgers=(_source(tmp_path, _four_use_records()),),
        conformance_registry=(),
    )
    table = render_report_table(report)
    assert "候補" in table
    assert "MCP実使用" in table
    assert "フォールバック不要" in table
    assert "固有conformanceガードなし" in table
    assert "推奨" in table
    assert ADAPTER in table
    assert "unknown" in table
    assert "true" in table


def test_report_round_trips_through_canonical_bytes(tmp_path: Path) -> None:
    report = evaluate_legacy_removal(
        mcp_fit=ADAPTER_FIT,
        call_ledgers=(_source(tmp_path, _four_use_records()),),
        conformance_registry=_adapter_guards(),
    )
    raw = canonical_model_bytes(report)
    restored = LegacyRemovalReportV1.model_validate(json.loads(raw.decode("utf-8")))
    assert restored == report


def test_real_repo_smoke_generates_report_from_committed_evidence() -> None:
    assert REAL_FIT.is_file()
    report = evaluate_legacy_removal(
        mcp_fit=load_mcp_fit(REAL_FIT),
        call_ledgers=(_real_source(),),
    )
    ids = [candidate.candidate_id for candidate in report.candidates]
    assert len(ids) == 5
    assert len(set(ids)) == 5
    for candidate in report.candidates:
        assert tuple(condition.condition for condition in candidate.conditions) == (
            "mcp_production_use",
            "fallback_not_required",
            "no_unique_conformance_guards",
        )
        assert candidate.recommendation in {"remove-ready", "not-ready", "unknown"}
        # no registry on disk: guards stay unknown, so removal is NEVER recommended
        assert candidate.recommendation != "remove-ready"


def test_real_repo_cli_prints_decision_table(capsys: pytest.CaptureFixture[str]) -> None:
    code = legacy_cli_main(
        ["--mcp-fit", str(REAL_FIT), "--ledger-dir", REAL_LEDGER],
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "候補" in out
    assert "resolve-bridge-base-cut" in out
    assert "unknown" in out
