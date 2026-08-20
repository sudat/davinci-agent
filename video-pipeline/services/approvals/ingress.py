"""Local TTY approval ingress.

``record_operation`` is the ONLY path that can produce operator records
(``fixture_only=false``): it refuses automation-class runners outright,
refuses any file descriptor that is not a real controlling terminal,
displays the purpose and target bundle hash to the operator, and reads
an explicit confirmation token. ``record_fixture_operation`` is the
fixture seam: tests and the automated gate may create fixture-MARKED
records programmatically; automation can never produce an unmarked
operator record (enforced again at the model layer).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

from pydantic import TypeAdapter, ValidationError

from services.approvals.models import (
    PURPOSE_TARGET_TYPES,
    ApprovalPurpose,
    ApprovalTargetType,
    OperationDraft,
    RecordDecision,
    RunnerClass,
)
from services.contracts.primitives import Sha256

if TYPE_CHECKING:
    from services.approvals.global_review_models import FinalReviewBinding

OPERATOR_CONFIRMATION: Final = "confirm"
_SHA256_ADAPTER: Final[TypeAdapter[Sha256]] = TypeAdapter(Sha256)
_PROMPT_MAX_BYTES: Final = 4096


class IngressRefusalError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _validated_target_hash(target_bundle_hash: str) -> Sha256:
    try:
        return _SHA256_ADAPTER.validate_python(target_bundle_hash)
    except ValidationError as error:
        raise IngressRefusalError(
            "invalid-target-hash",
            f"target bundle hash must be a sha256 hex digest: {target_bundle_hash!r}",
        ) from error


def _target_type_for(purpose: ApprovalPurpose) -> ApprovalTargetType:
    return next(iter(PURPOSE_TARGET_TYPES[purpose]))  # type: ignore[return-value]


def _display_and_confirm(
    tty_fd: int,
    *,
    purpose: str,
    target_hash: str,
    decision: str,
    actor_id: str,
) -> tuple[int, str]:
    try:
        tty_path = os.ttyname(tty_fd)
    except OSError as error:
        raise IngressRefusalError(
            "not-a-tty", f"descriptor {tty_fd} has no terminal path: {error}"
        ) from error
    uid = os.getuid()
    prompt = (
        "\n=== LOCAL OPERATOR APPROVAL ===\n"
        f"purpose: {purpose}\n"
        f"target bundle sha256: {target_hash}\n"
        f"decision to confirm: {decision}\n"
        f"actor: {actor_id} (uid={uid}, tty={tty_path})\n"
        f"type '{OPERATOR_CONFIRMATION}' to record this decision: "
    )
    os.write(tty_fd, prompt.encode())
    line = os.read(tty_fd, _PROMPT_MAX_BYTES).decode(errors="replace").strip().lower()
    if line != OPERATOR_CONFIRMATION:
        raise IngressRefusalError(
            "operator-aborted",
            f"operator did not type '{OPERATOR_CONFIRMATION}'; no record written",
        )
    return uid, tty_path


def record_operation(  # noqa: PLR0913 (TTY ingress contract from the Todo 13 brief)
    *,
    purpose: ApprovalPurpose,
    target_bundle_hash: str,
    decision: RecordDecision,
    actor_id: str,
    tty_fd: int | None,
    runner_class: RunnerClass = "operator",
    fixture: bool = False,
    wall_time_unix: int | None = None,
    final_binding: FinalReviewBinding | None = None,
) -> OperationDraft:
    """Interactive operator ingress; refuses automation and non-TTY callers."""

    target_hash = _validated_target_hash(target_bundle_hash)
    if runner_class == "automation":
        raise IngressRefusalError(
            "automation-refused",
            "automation-class runners can never invoke the operator TTY path",
        )
    if tty_fd is None or not os.isatty(tty_fd):
        raise IngressRefusalError(
            "not-a-tty",
            "operator ingress requires a real controlling terminal (isatty failed)",
        )
    uid, tty_path = _display_and_confirm(
        tty_fd,
        purpose=purpose,
        target_hash=target_hash,
        decision=decision,
        actor_id=actor_id,
    )
    return OperationDraft(
        purpose=purpose,
        target_type=_target_type_for(purpose),
        target_hash=target_hash,
        decision=decision,
        actor_id=actor_id,
        uid=uid,
        tty=tty_path,
        wall_time_unix=wall_time_unix,
        fixture_only=fixture,
        runner_class="operator",
        final_binding=final_binding,
    )


def record_fixture_operation(  # noqa: PLR0913 (fixture seam mirrors the ingress contract)
    *,
    purpose: ApprovalPurpose,
    target_bundle_hash: str,
    decision: RecordDecision,
    actor_id: str,
    runner_class: RunnerClass = "automation",
    uid: int | None = None,
    tty: str | None = None,
    wall_time_unix: int | None = None,
    final_binding: FinalReviewBinding | None = None,
) -> OperationDraft:
    """Programmatic fixture-marked records for tests and the automated gate."""

    target_hash = _validated_target_hash(target_bundle_hash)
    return OperationDraft(
        purpose=purpose,
        target_type=_target_type_for(purpose),
        target_hash=target_hash,
        decision=decision,
        actor_id=actor_id,
        uid=uid,
        tty=tty,
        wall_time_unix=wall_time_unix,
        fixture_only=True,
        runner_class=runner_class,
        final_binding=final_binding,
    )


__all__ = [
    "OPERATOR_CONFIRMATION",
    "IngressRefusalError",
    "record_fixture_operation",
    "record_operation",
]
