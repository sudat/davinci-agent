# allow: SIZE_OK — task 59 pins the commit scope to this one report module:
# ~150 lines are the pure-data LEGACY_CANDIDATES table (read-only inventory
# of the five legacy areas); the rest is strict models whose validators
# enforce the unknown-never-recommends-removal contract.
"""Legacy removal decision harness (task 59) — REPORT ONLY, never deletes.

Machine-judges the three removal conditions from impl-plan 1229-1235 per
legacy direct-build area (read-only inventory of the actual repo tree):
(1) MCP production capability used >= 3 times (call ledgers plus
run-report aggregates count the calls exercising the area's MCP
counterpart), (2) fallback no longer required (any failed/partial/
not_available mcp-fit capability whose fallback is ``legacy_direct``
keeps the area required), (3) no unique conformance/guard behavior would
be lost (the conformance-guard registry maps guards to MCP counterparts;
no entries for an area means UNKNOWN, never "no guards"). ``unknown``
NEVER yields a removal recommendation — 証拠不足は削除推奨しない.
Deletion itself is a separate operator-approved task outside this plan.
Capabilities whose legacy implementation lives outside the enumerated
areas (e.g. audio mix in ``services/presentation``) are intentionally
not mapped to any candidate.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Final, Literal, NamedTuple

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticCustomError

from services.contracts.primitives import StrictModel, to_tuple
from services.mcp_client.call_models import (
    McpExecutionCallV1,
    McpExecutionReportV1,
    read_call_records,
)

MIN_PRODUCTION_USES: Final = 3
NOT_ACCEPTED: Final[frozenset[str]] = frozenset({"failed", "partial", "not_available"})
LEGACY_DIRECT: Final = "legacy_direct"
RUN_REPORT_SCHEMA: Final = "mcp-execution-run-report-v1"
EXPECTED_CONDITIONS: Final[tuple[str, ...]] = (
    "mcp_production_use",
    "fallback_not_required",
    "no_unique_conformance_guards",
)

ConditionKind = Literal[
    "mcp_production_use", "fallback_not_required", "no_unique_conformance_guards"
]
ConditionVerdict = Literal["true", "false", "unknown"]
Recommendation = Literal["remove-ready", "not-ready", "unknown"]
FitStatus = Literal["accepted", "failed", "partial", "not_available"]
FitFallback = Literal["legacy_direct", "template_external", "manual"]


class LegacyRemovalError(Exception):
    """Typed base for legacy-removal harness failures."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class McpFitRowInput(StrictModel):
    """The three mcp-fit fields the decision needs (extra keys ignored)."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    capability: str = Field(min_length=1)
    status: FitStatus
    fallback: FitFallback


class LedgerDirSource(StrictModel):
    """A directory holding the append-only ``mcp-call-ledger.jsonl``."""

    kind: Literal["ledger_dir"]
    path: str = Field(min_length=1)


class RunReportSource(StrictModel):
    """A ``mcp-execution-run-report-v1`` JSON (tool-level call counts)."""

    kind: Literal["run_report"]
    path: str = Field(min_length=1)


CallLedgerSource = LedgerDirSource | RunReportSource


class ConformanceGuardEntry(StrictModel):
    """One legacy guard behavior and the MCP capability that would replace it."""

    guard_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    evidence_ref: str = Field(min_length=1)
    mcp_capability: str | None = None


class ConditionAssessmentV1(StrictModel):
    condition: ConditionKind
    verdict: ConditionVerdict
    evidence: str = Field(min_length=1)


class LegacyCandidateV1(StrictModel):
    """One legacy area with its three machine-judged conditions."""

    candidate_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    area_paths: Annotated[tuple[str, ...], BeforeValidator(to_tuple)] = Field(
        min_length=1
    )
    area_files_found: Annotated[tuple[str, ...], BeforeValidator(to_tuple)]
    mcp_capabilities: Annotated[tuple[str, ...], BeforeValidator(to_tuple)]
    mcp_production_use_count: int = Field(ge=0, strict=True)
    fallback_required_capabilities: Annotated[
        tuple[str, ...], BeforeValidator(to_tuple)
    ]
    unique_guard_ids: Annotated[tuple[str, ...], BeforeValidator(to_tuple)]
    conditions: Annotated[
        tuple[ConditionAssessmentV1, ...], BeforeValidator(to_tuple)
    ]
    recommendation: Recommendation

    @model_validator(mode="after")
    def _verdicts_support_recommendation(self) -> LegacyCandidateV1:
        kinds = tuple(condition.condition for condition in self.conditions)
        if kinds != EXPECTED_CONDITIONS:
            raise PydanticCustomError(
                "conditions_inconsistent",
                "{candidate_id}: conditions must be exactly {expected}, got {got}",
                {
                    "candidate_id": self.candidate_id,
                    "expected": list(EXPECTED_CONDITIONS),
                    "got": list(kinds),
                },
            )
        verdicts = tuple(condition.verdict for condition in self.conditions)
        if all(verdict == "true" for verdict in verdicts):
            expected: str = "remove-ready"
        elif "false" in verdicts:
            expected = "not-ready"
        else:
            expected = "unknown"
        if self.recommendation != expected:
            raise PydanticCustomError(
                "recommendation_inconsistent",
                "{candidate_id}: recommendation {recommendation} is not supported "
                "by verdicts {verdicts}",
                {
                    "candidate_id": self.candidate_id,
                    "recommendation": self.recommendation,
                    "verdicts": list(verdicts),
                },
            )
        return self


class LegacyRemovalReportV1(StrictModel):
    schema_version: Literal["legacy-removal-report-v1"] = "legacy-removal-report-v1"
    candidates: Annotated[
        tuple[LegacyCandidateV1, ...], BeforeValidator(to_tuple)
    ] = Field(min_length=1)

    @model_validator(mode="after")
    def _candidate_ids_unique(self) -> LegacyRemovalReportV1:
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise PydanticCustomError(
                "duplicate_candidate", "candidate_id values must be unique"
            )
        return self


class CandidateSpec(NamedTuple):
    """Declared mapping from a legacy area to its MCP counterpart surface."""

    candidate_id: str
    label: str
    area_globs: tuple[str, ...]
    mcp_capabilities: tuple[str, ...]
    mcp_tool_actions: tuple[tuple[str, str], ...]


# allow: SIZE_OK — pure data table: the five coherent legacy areas enumerated
# read-only from the repo tree with their MCP counterpart surfaces.
LEGACY_CANDIDATES: Final[tuple[CandidateSpec, ...]] = (
    CandidateSpec(
        candidate_id="resolve-bridge-base-cut",
        label="resolve_bridge base-cut 経路",
        area_globs=("services/resolve_bridge/base_cut*.py",),
        mcp_capabilities=(
            "project-timeline-creation",
            "import-media",
            "exact-source-range-placement",
            "source-record-readback",
            "conform-source-ranges",
            "gaps-overlaps-missing-media",
        ),
        mcp_tool_actions=(
            ("project_manager", "create"),
            ("project_settings", "set_setting"),
            ("media_pool", "create_timeline"),
            ("media_pool", "safe_import_media"),
            ("media_pool", "append_to_timeline"),
            ("timeline", "set_current"),
            ("timeline", "probe_timeline_structure"),
            ("timeline", "source_range_report"),
            ("timeline", "detect_gaps_overlaps"),
            ("timeline", "detect_missing_media"),
        ),
    ),
    CandidateSpec(
        candidate_id="resolve-bridge-presentation",
        label="resolve_bridge 固定プレゼンテーション経路",
        area_globs=("services/resolve_bridge/fixed_presentation*.py",),
        mcp_capabilities=(
            "subtitle-capability",
            "title-text-plus",
            "fusion-template-insertion",
            "clip-transform-punch-in",
            "transition-path",
            "color-grade-preset-drx",
        ),
        mcp_tool_actions=(
            ("timeline", "subtitle_generation_probe"),
            ("timeline", "set_title_text"),
            ("timeline", "insert_fusion_title"),
            ("timeline", "insert_fusion_composition"),
            ("timeline_item", "set_transform"),
            ("timeline", "duplicate_clips"),
            ("timeline_item_fusion", "get_comp_count"),
            ("timeline_item_color", "safe_apply_drx"),
        ),
    ),
    CandidateSpec(
        candidate_id="resolve-bridge-build-report",
        label="resolve_bridge ビルド報告経路",
        area_globs=("services/resolve_bridge/build_report*.py",),
        mcp_capabilities=(
            "source-record-readback",
            "advanced-delivery-qc",
            "render-configuration",
            "render-job-lifecycle",
        ),
        mcp_tool_actions=(
            ("timeline", "source_range_report"),
            ("render", "export_render_boundary_report"),
            ("render", "validate_render_settings"),
            ("render", "set_format_and_codec"),
            ("render", "set_settings"),
            ("render", "get_format_and_codec"),
            ("render", "add_job"),
            ("render", "start"),
            ("render", "get_job_status"),
            ("render", "list_jobs"),
            ("render", "delete_job"),
        ),
    ),
    CandidateSpec(
        candidate_id="resolve-adapter-package",
        label="resolve_adapter パッケージコンパイル",
        area_globs=("services/resolve_adapter/*.py",),
        mcp_capabilities=(
            "exact-source-range-placement",
            "source-record-readback",
            "render-configuration",
        ),
        mcp_tool_actions=(
            ("media_pool", "append_to_timeline"),
            ("timeline", "source_range_report"),
            ("render", "set_format_and_codec"),
        ),
    ),
    CandidateSpec(
        candidate_id="build-clean-builder",
        label="build CleanBuilder(クリーンビルド)",
        area_globs=("services/build/clean_builder.py", "services/build/builder_*.py"),
        mcp_capabilities=(
            "render-configuration",
            "render-job-lifecycle",
            "gaps-overlaps-missing-media",
        ),
        mcp_tool_actions=(
            ("render", "set_format_and_codec"),
            ("render", "set_settings"),
            ("render", "add_job"),
            ("render", "start"),
            ("render", "get_job_status"),
            ("render", "list_jobs"),
            ("render", "delete_job"),
            ("timeline", "detect_gaps_overlaps"),
        ),
    ),
)

_REPO_ROOT: Final = Path(__file__).resolve().parents[2]


def _fit_rows(mcp_fit: Mapping[str, Any]) -> dict[str, McpFitRowInput]:
    raw_rows = mcp_fit.get("capabilities")
    if not isinstance(raw_rows, list):
        raise LegacyRemovalError("fit-shape", "mcp_fit must carry a 'capabilities' list")
    rows: dict[str, McpFitRowInput] = {}
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, dict):
            raise LegacyRemovalError(
                "fit-shape", f"capabilities[{index}] is not an object"
            )
        try:
            row = McpFitRowInput(
                capability=raw["capability"],
                status=raw["status"],
                fallback=raw["fallback"],
            )
        except (KeyError, ValidationError) as error:
            raise LegacyRemovalError(
                "fit-row", f"capabilities[{index}]: {error}"
            ) from error
        rows[row.capability] = row
    return rows


def _run_report_by_tool(path: Path) -> dict[str, int]:
    try:
        payload: Any = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError) as error:
        raise LegacyRemovalError(
            "run-report-unreadable", f"{path}: {error}"
        ) from error
    if not isinstance(payload, dict) or payload.get("schema_version") != RUN_REPORT_SCHEMA:
        raise LegacyRemovalError("run-report-schema", f"{path}: not {RUN_REPORT_SCHEMA}")
    try:
        calls = McpExecutionReportV1.model_validate(payload["calls"])
    except (KeyError, ValidationError) as error:
        raise LegacyRemovalError("run-report-calls", f"{path}: {error}") from error
    return dict(calls.by_tool)


def _collect_calls(
    sources: Sequence[CallLedgerSource],
) -> tuple[tuple[McpExecutionCallV1, ...], dict[str, int]]:
    records: list[McpExecutionCallV1] = []
    run_by_tool: dict[str, int] = {}
    for source in sources:
        if isinstance(source, LedgerDirSource):
            try:
                records.extend(read_call_records(Path(source.path)))
            except (OSError, ValidationError) as error:
                raise LegacyRemovalError(
                    "ledger-unreadable", f"{source.path}: {error}"
                ) from error
        else:
            for tool, count in _run_report_by_tool(Path(source.path)).items():
                run_by_tool[tool] = run_by_tool.get(tool, 0) + count
    return tuple(records), run_by_tool


def _inventory(globs: Sequence[str]) -> tuple[str, ...]:
    """Read-only glob of the legacy area (proof the inventory is real)."""
    if not (_REPO_ROOT / "services").is_dir():
        return ()
    found: set[str] = set()
    for pattern in globs:
        found.update(
            path.relative_to(_REPO_ROOT).as_posix()
            for path in _REPO_ROOT.glob(pattern)
            if path.is_file()
        )
    return tuple(sorted(found))


def _recommendation(verdicts: tuple[str, ...]) -> Recommendation:
    if all(verdict == "true" for verdict in verdicts):
        return "remove-ready"
    if "false" in verdicts:
        return "not-ready"
    return "unknown"


def _assess(
    spec: CandidateSpec,
    *,
    rows: Mapping[str, McpFitRowInput],
    records: Sequence[McpExecutionCallV1],
    run_by_tool: Mapping[str, int],
    guards: Sequence[ConformanceGuardEntry],
) -> LegacyCandidateV1:
    actions = frozenset(spec.mcp_tool_actions)
    ledger_ok = sum(
        1
        for record in records
        if record.status == "ok" and (record.tool_name, record.action) in actions
    )
    run_uses = sum(run_by_tool.get(tool, 0) for tool in {tool for tool, _ in actions})
    count = ledger_ok + run_uses
    if actions:
        use = ConditionAssessmentV1(
            condition="mcp_production_use",
            verdict="true" if count >= MIN_PRODUCTION_USES else "false",
            evidence=(
                f"ok_calls={ledger_ok} run_report_by_tool={run_uses} "
                f"threshold={MIN_PRODUCTION_USES}"
            ),
        )
    else:
        use = ConditionAssessmentV1(
            condition="mcp_production_use",
            verdict="unknown",
            evidence="no_mcp_counterpart_defined",
        )
    missing = tuple(cap for cap in spec.mcp_capabilities if cap not in rows)
    required = tuple(
        cap
        for cap in spec.mcp_capabilities
        if cap in rows
        and rows[cap].status in NOT_ACCEPTED
        and rows[cap].fallback == LEGACY_DIRECT
    )
    if missing:
        fallback = ConditionAssessmentV1(
            condition="fallback_not_required",
            verdict="unknown",
            evidence=f"capabilities_missing_from_fit={','.join(missing)}",
        )
    else:
        fallback = ConditionAssessmentV1(
            condition="fallback_not_required",
            verdict="false" if required else "true",
            evidence=(
                f"fallback_required={','.join(required)}"
                if required
                else "no_failed_legacy_direct_fallback"
            ),
        )
    area_guards = tuple(
        guard for guard in guards if guard.candidate_id == spec.candidate_id
    )
    if not area_guards:
        guard_condition = ConditionAssessmentV1(
            condition="no_unique_conformance_guards",
            verdict="unknown",
            evidence="no_registry_entries",
        )
        unique_ids: tuple[str, ...] = ()
    else:
        unique_ids = tuple(
            guard.guard_id
            for guard in area_guards
            if guard.mcp_capability is None
            or rows.get(guard.mcp_capability) is None
            or rows[guard.mcp_capability].status != "accepted"
        )
        guard_condition = ConditionAssessmentV1(
            condition="no_unique_conformance_guards",
            verdict="false" if unique_ids else "true",
            evidence=(
                f"unique_guards={','.join(unique_ids)}"
                if unique_ids
                else f"guards_covered={len(area_guards)}"
            ),
        )
    conditions = (use, fallback, guard_condition)
    return LegacyCandidateV1(
        candidate_id=spec.candidate_id,
        label=spec.label,
        area_paths=spec.area_globs,
        area_files_found=_inventory(spec.area_globs),
        mcp_capabilities=spec.mcp_capabilities,
        mcp_production_use_count=count,
        fallback_required_capabilities=required,
        unique_guard_ids=unique_ids,
        conditions=conditions,
        recommendation=_recommendation(tuple(c.verdict for c in conditions)),
    )


def evaluate_legacy_removal(
    *,
    mcp_fit: Mapping[str, Any],
    call_ledgers: Sequence[CallLedgerSource] = (),
    conformance_registry: Sequence[ConformanceGuardEntry] = (),
) -> LegacyRemovalReportV1:
    """Judge every legacy candidate against the three removal conditions."""
    rows = _fit_rows(mcp_fit)
    records, run_by_tool = _collect_calls(call_ledgers)
    guards = tuple(conformance_registry)
    return LegacyRemovalReportV1(
        candidates=tuple(
            _assess(
                spec,
                rows=rows,
                records=records,
                run_by_tool=run_by_tool,
                guards=guards,
            )
            for spec in LEGACY_CANDIDATES
        )
    )


__all__ = [
    "LEGACY_CANDIDATES",
    "CallLedgerSource",
    "CandidateSpec",
    "ConditionAssessmentV1",
    "ConformanceGuardEntry",
    "LedgerDirSource",
    "LegacyCandidateV1",
    "LegacyRemovalError",
    "LegacyRemovalReportV1",
    "McpFitRowInput",
    "RunReportSource",
    "evaluate_legacy_removal",
]
