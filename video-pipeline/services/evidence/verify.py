from __future__ import annotations

import argparse
from pathlib import Path
from typing import Final, assert_never

from pydantic import ValidationError

from services.evidence.ledger import GENESIS_HASH, hash_event
from services.evidence.models import (
    EVENT_ADAPTER,
    AssertionResult,
    AttemptEvent,
    CompletionEvent,
    EvidenceEvent,
    ExitCodeAssertion,
    RetainedHead,
    StderrContainsAssertion,
    StdoutContainsAssertion,
    VerificationReport,
)
from services.foundation_io import canonical_model_bytes, sha256_file

OUTSIDE_GUARANTEE: Final = (
    "unrecorded executions cannot be detected",
    "full ledger and retained-head replacement cannot be detected",
    "owner or root rewrites cannot be prevented",
)


class EvidenceVerificationError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


def _unretained_tail(lines: list[bytes], retained_count: int) -> bool:
    suffix = lines[retained_count:]
    if suffix and (len(suffix) != 1 or suffix[0].endswith((b"\n", b"\r"))):
        raise EvidenceVerificationError("unretained complete or interior ledger data")
    return bool(suffix)


def _load_events(bundle: Path) -> tuple[tuple[EvidenceEvent, ...], RetainedHead, bool]:
    try:
        head = RetainedHead.model_validate_json((bundle / "head.json").read_bytes())
    except (OSError, ValidationError) as error:
        raise EvidenceVerificationError(f"invalid retained head: {error}") from error
    try:
        lines = (bundle / "ledger.jsonl").read_bytes().splitlines(keepends=True)
    except OSError as error:
        raise EvidenceVerificationError(f"cannot read ledger: {error}") from error
    if len(lines) < head.sequence:
        raise EvidenceVerificationError("ledger is shorter than retained head")
    events: list[EvidenceEvent] = []
    previous = GENESIS_HASH
    for index, line in enumerate(lines[: head.sequence], start=1):
        if not line.endswith((b"\n", b"\r")):
            raise EvidenceVerificationError(f"ledger row {index} is incomplete but retained")
        try:
            event = EVENT_ADAPTER.validate_json(line.rstrip(b"\r\n"))
        except ValidationError as error:
            raise EvidenceVerificationError(f"invalid ledger row {index}: {error}") from error
        if event.sequence != index:
            raise EvidenceVerificationError(f"ledger row {index} has invalid sequence")
        if event.previous_event_hash != previous or event.event_hash != hash_event(event):
            raise EvidenceVerificationError(f"ledger row {index} breaks hash chain")
        previous = event.event_hash
        events.append(event)
    if previous != head.event_hash:
        raise EvidenceVerificationError("retained head does not match ledger")
    return tuple(events), head, _unretained_tail(lines, head.sequence)


def _assertion_results(event: CompletionEvent) -> tuple[AssertionResult, ...]:
    stdout = Path(event.stdout_path).read_bytes()
    stderr = Path(event.stderr_path).read_bytes()
    if sha256_file(Path(event.stdout_path)) != event.stdout_sha256:
        raise EvidenceVerificationError(f"stdout hash mismatch for {event.attempt_id}")
    if sha256_file(Path(event.stderr_path)) != event.stderr_sha256:
        raise EvidenceVerificationError(f"stderr hash mismatch for {event.attempt_id}")
    results: list[AssertionResult] = []
    for assertion in event.assertions:
        match assertion:
            case ExitCodeAssertion():
                passed = event.exit_code == assertion.expected
            case StdoutContainsAssertion():
                passed = assertion.expected.encode() in stdout
            case StderrContainsAssertion():
                passed = assertion.expected.encode() in stderr
            case unreachable:
                assert_never(unreachable)
        results.append(
            AssertionResult(
                attempt_id=event.attempt_id,
                kind=assertion.kind,
                authored_passed=assertion.passed,
                recomputed_passed=passed,
            )
        )
    return tuple(results)


def verify_bundle(bundle: Path, *, recompute: bool) -> VerificationReport:
    if not recompute:
        raise EvidenceVerificationError("--recompute is required")
    events, head, partial_tail = _load_events(bundle)
    pending: set[str] = set()
    assertions: list[AssertionResult] = []
    for event in events:
        match event:
            case AttemptEvent(attempt_id=attempt_id):
                if attempt_id in pending:
                    raise EvidenceVerificationError(f"duplicate pending attempt: {attempt_id}")
                pending.add(attempt_id)
            case CompletionEvent(attempt_id=attempt_id):
                if attempt_id not in pending:
                    raise EvidenceVerificationError(f"completion without attempt: {attempt_id}")
                pending.remove(attempt_id)
                assertions.extend(_assertion_results(event))
            case unreachable:
                assert_never(unreachable)
    return VerificationReport(
        chain_valid=True,
        retained_head=head.event_hash,
        recomputed_passed=bool(assertions) and all(item.recomputed_passed for item in assertions),
        assertions=tuple(assertions),
        incomplete_attempt_ids=tuple(sorted(pending)),
        partial_tail_ignored=partial_tail,
        outside_guarantee=OUTSIDE_GUARANTEE,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--recompute", action="store_true")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        report = verify_bundle(arguments.directory, recompute=arguments.recompute)
    except (EvidenceVerificationError, OSError) as error:
        print(error)
        return 2
    print(canonical_model_bytes(report).decode())
    return 0 if report.recomputed_passed and not report.incomplete_attempt_ids else 1


if __name__ == "__main__":
    raise SystemExit(main())
