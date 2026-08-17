from __future__ import annotations

import argparse
from pathlib import Path

from services.fixtures.prepare import PrepareError, PrepareRequest, prepare_errors, prepare_freeze
from services.fixtures.prepare_control_plane import (
    ControlPlanePrepareRequest,
    prepare_control_plane,
    prepare_control_plane_errors,
)
from services.fixtures.prepare_phase0b import (
    Phase0BPrepareRequest,
    prepare_phase0b,
    prepare_phase0b_errors,
)
from services.fixtures.prepare_phase0c import (
    Phase0CPrepareRequest,
    prepare_phase0c,
    prepare_phase0c_errors,
)
from services.fixtures.publish import publish_errors, publish_freeze


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument(
        "phase", choices=("phase-0a", "phase-0b", "phase-0c", "control-plane")
    )
    prepare.add_argument("--toolchain-lock", type=Path, required=True)
    prepare.add_argument("--fixture-ids", required=True)
    prepare.add_argument("--parent-result", type=Path)
    prepare.add_argument("--pre-source-snapshot", type=Path, required=True)
    prepare.add_argument("--execution-contract", type=Path, required=True)
    prepare.add_argument("--out", type=Path, required=True)
    prepare.add_argument("--freeze-receipt", type=Path, required=True)
    prepare.add_argument("--staging", type=Path, required=True)
    prepare.add_argument("--intent", type=Path, required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("--intent", type=Path, required=True)
    publish.add_argument("--ledger", type=Path, required=True)
    publish.add_argument("--recover", action="store_true", required=True)
    return parser


def _prepare(arguments: argparse.Namespace) -> int:
    if arguments.phase == "phase-0a":
        if arguments.parent_result is not None:
            raise PrepareError("phase-0a takes no parent result")
        request = PrepareRequest(
            toolchain_lock=arguments.toolchain_lock,
            fixture_ids=tuple(arguments.fixture_ids.split(",")),
            pre_source_snapshot=arguments.pre_source_snapshot,
            execution_contract=arguments.execution_contract,
            policy_out=arguments.out,
            freeze_receipt=arguments.freeze_receipt,
            staging=arguments.staging,
            intent=arguments.intent,
        )
        prepare_freeze(request)
        print("freeze prepared: phase-0a v1")
        return 0
    if arguments.phase == "phase-0b":
        if arguments.parent_result is None:
            raise PrepareError("phase-0b requires the parent phase-0a gate result")
        request = Phase0BPrepareRequest(
            toolchain_lock=arguments.toolchain_lock,
            fixture_ids=tuple(arguments.fixture_ids.split(",")),
            parent_result=arguments.parent_result,
            pre_source_snapshot=arguments.pre_source_snapshot,
            execution_contract=arguments.execution_contract,
            policy_out=arguments.out,
            freeze_receipt=arguments.freeze_receipt,
            staging=arguments.staging,
            intent=arguments.intent,
        )
        prepare_phase0b(request)
        print("freeze prepared: phase-0b v1")
        return 0
    if arguments.phase == "control-plane":
        if arguments.parent_result is None:
            raise PrepareError("control-plane requires the parent phase-0c gate result")
        request = ControlPlanePrepareRequest(
            toolchain_lock=arguments.toolchain_lock,
            fixture_ids=tuple(arguments.fixture_ids.split(",")),
            parent_result=arguments.parent_result,
            pre_source_snapshot=arguments.pre_source_snapshot,
            execution_contract=arguments.execution_contract,
            policy_out=arguments.out,
            freeze_receipt=arguments.freeze_receipt,
            staging=arguments.staging,
            intent=arguments.intent,
        )
        prepare_control_plane(request)
        print("freeze prepared: control-plane-baseline v1")
        return 0
    if arguments.parent_result is None:
        raise PrepareError("phase-0c requires the parent phase-0b gate result")
    request = Phase0CPrepareRequest(
        toolchain_lock=arguments.toolchain_lock,
        fixture_ids=tuple(arguments.fixture_ids.split(",")),
        parent_result=arguments.parent_result,
        pre_source_snapshot=arguments.pre_source_snapshot,
        execution_contract=arguments.execution_contract,
        policy_out=arguments.out,
        freeze_receipt=arguments.freeze_receipt,
        staging=arguments.staging,
        intent=arguments.intent,
    )
    prepare_phase0c(request)
    print("freeze prepared: phase-0c v1")
    return 0


def _publish(arguments: argparse.Namespace) -> int:
    publish_freeze(arguments.intent, arguments.ledger, recover=arguments.recover)
    print("freeze published")
    return 0


def main() -> int:
    arguments = _parser().parse_args()
    try:
        if arguments.command == "prepare":
            return _prepare(arguments)
        return _publish(arguments)
    except (
        *prepare_errors(),
        *prepare_phase0b_errors(),
        *prepare_phase0c_errors(),
        *prepare_control_plane_errors(),
        *publish_errors()
    ) as error:
        print(error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
