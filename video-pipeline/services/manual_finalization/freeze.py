"""Freeze Package assembly: gated by a valid MANUAL_FREEZE TTY record.

``assemble_freeze_package`` is the ONLY assembly path and it refuses to
run without an authorizing MANUAL_FREEZE purpose record for the exact
freeze target (the Todo-13 approvals ingress produced that record; here
we only evaluate the chain). Automation can never authorize a production
freeze, and a fixture-marked record never freezes for real — the QA seam
mirrors the Todo-45 checkpoint pattern. Incomplete inputs (missing
DRT/DRP commands, missing operator record binding material, missing
rights/privacy/QC references) are typed refusals, never partially-filled
packages.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from pydantic import ValidationError

from services.approvals.verify import evaluate_authorization, validate_supersession_chain
from services.contracts.serialization import GENESIS_SHA256
from services.manual_finalization.freeze_models import (
    DrtDrp,
    FreezeInputs,
    FreezePackage,
    FreezeReasonCode,
    InputHash,
    OperatorRecordBinding,
    ReportRef,
    StructuredReason,
)

if TYPE_CHECKING:
    from services.approvals.models import ChainedOperationRecord


class FreezeRefusal(Exception):  # noqa: N818 (typed-refusal vocabulary, not an error kind)
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _authorizing_record(
    records: tuple[ChainedOperationRecord, ...], record_id: str | None
) -> ChainedOperationRecord | None:
    if record_id is None:
        return None
    return next((record for record in records if record.record_id == record_id), None)


def assemble_freeze_package(  # noqa: PLR0913 (authorization boundary: records + inputs + chain key)
    *,
    records: tuple[ChainedOperationRecord, ...],
    inputs: FreezeInputs | Mapping[str, object],
    reason_detail: str,
    chain_key: bytes,
    fixture_mode: bool = False,
    reason_code: FreezeReasonCode = "unsupported_capability",
) -> FreezePackage:
    frozen_inputs = (
        inputs
        if isinstance(inputs, FreezeInputs)
        else _validated_inputs(inputs)
    )
    try:
        validate_supersession_chain(records, chain_key=chain_key)
    except ValueError as error:
        raise FreezeRefusal(
            "records-chain-invalid",
            f"the operation-record chain does not verify under the store key: {error}",
        ) from error
    verdict = evaluate_authorization(
        records,
        purpose="manual_freeze",
        target_hash=frozen_inputs.target_set_hash,
        target_type="frozen-timeline",
        operator_gate=not fixture_mode,
    )
    if not verdict.authorized:
        code = verdict.refusal_code or "unauthorized"
        if code == "no-record":
            code = "missing-manual-freeze-record"
        raise FreezeRefusal(
            code,
            f"a purpose=manual_freeze operation record for freeze target "
            f"{frozen_inputs.target_set_hash} is required before any freeze "
            f"(refusal: {verdict.refusal_code})",
        )
    authorizing = _authorizing_record(records, verdict.record_id)
    if authorizing is None:
        raise FreezeRefusal("authorizing-record-missing", verdict.record_id or "")
    if not fixture_mode and authorizing.runner_class == "automation":
        raise FreezeRefusal(
            "automation-refused",
            "an automation-class record can never authorize a production freeze",
        )
    reason = StructuredReason(code=reason_code, detail=reason_detail)
    operator_record = OperatorRecordBinding(
        purpose="manual_freeze",
        record_id=authorizing.record_id,
        fixture_only=authorizing.fixture_only,
    )
    try:
        unsealed = FreezePackage.model_construct(
            schema_version="freeze-package-v1",
            episode_id=frozen_inputs.episode_id,
            automation_frozen=True,
            fixture_only=frozen_inputs.fixture_only,
            drt_drp=frozen_inputs.drt_drp,
            render=frozen_inputs.render,
            timeline_fingerprint=frozen_inputs.timeline_fingerprint,
            conformance_fingerprint=frozen_inputs.conformance_fingerprint,
            change_log=frozen_inputs.change_log,
            last_committed_plan=frozen_inputs.last_committed_plan,
            reason=reason,
            operator_record=operator_record,
            reports=frozen_inputs.reports,
            target_set_hash=frozen_inputs.target_set_hash,
            package_sha256=GENESIS_SHA256,
        )
        sealed = unsealed.model_copy(
            update={"package_sha256": unsealed.computed_package_hash()}
        )
        return FreezePackage.model_validate_json(sealed.model_dump_json())
    except ValidationError as error:
        raise FreezeRefusal(
            "incomplete-freeze-package",
            f"the freeze package is incomplete: {error}",
        ) from error


def _validated_inputs(inputs: Mapping[str, object]) -> FreezeInputs:
    try:
        return FreezeInputs.model_validate(dict(inputs))
    except ValidationError as error:
        raise FreezeRefusal(
            "incomplete-freeze-package",
            f"freeze inputs are incomplete (DRT/DRP, change log, reports): {error}",
        ) from error


__all__ = [
    "DrtDrp",
    "FreezeInputs",
    "FreezePackage",
    "FreezeRefusal",
    "InputHash",
    "ReportRef",
    "StructuredReason",
    "assemble_freeze_package",
]
