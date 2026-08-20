"""Local TTY approval ingress.

``record_operation`` is the ONLY path that can produce operator records
(``fixture_only=false``). It enforces, in order:

1. ``runner_class`` is validated against the frozen Literal at the
   function boundary (typed ``invalid-runner-class`` refusal — never an
   unvalidated string compare), and the value ``automation`` is refused
   outright (``automation-refused``).
2. ``tty_fd`` must be a tty AND must BE this process's controlling
   terminal: the process must be able to open ``/dev/tty`` (typed
   ``no-controlling-terminal`` refusal when it cannot — a daemon or
   test runner has none), and the descriptor's terminal identity must
   equal that controlling terminal (typed
   ``controlling-terminal-mismatch`` refusal otherwise). On Linux the
   identity is ``ttyname`` equality; on macOS, where ``ttyname`` on the
   ``/dev/tty`` alias reports the alias itself, the kernel-recorded
   controlling terminal of this pid (``ps -o tty=``) is compared. A
   self-owned pty (``pty.openpty`` plus a programmatic ``confirm``
   written to the master by the same process) therefore CANNOT mint an
   operator record: the slave is never the controlling terminal.
3. The purpose and target bundle hash are displayed on that terminal and
   an explicit ``confirm`` token is read from it (``operator-aborted``
   otherwise). No record is written on any refusal.

``record_fixture_operation`` is the fixture seam: tests and the automated
gate may create fixture-MARKED records programmatically; automation can
never produce an unmarked operator record (enforced again at the model
layer).

Honest limits (unchanged from the PRD's H1 model): a caller that
deliberately re-plumbs its own session (setsid plus controlling-terminal
acquisition) is mechanically indistinguishable from a terminal login;
this ingress is an auditable single-user action record, NOT
cryptographic identity or non-repudiation.
"""

from __future__ import annotations

import os
import subprocess
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
_RUNNER_CLASS_ADAPTER: Final[TypeAdapter[RunnerClass]] = TypeAdapter(RunnerClass)
_PROMPT_MAX_BYTES: Final = 4096
_PS_TIMEOUT_SECONDS: Final = 5
_ALIAS_NAME: Final = "/dev/tty"
_UNATTACHED_TTY_NAMES: Final = frozenset({"", "?", "??"})


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


def _validated_runner_class(runner_class: RunnerClass) -> RunnerClass:
    try:
        return _RUNNER_CLASS_ADAPTER.validate_python(runner_class)
    except ValidationError as error:
        raise IngressRefusalError(
            "invalid-runner-class",
            "runner_class must be exactly 'operator' or 'automation': "
            f"{runner_class!r}",
        ) from error


def _kernel_controlling_tty() -> str | None:
    """The kernel-recorded controlling terminal name of this pid, or None."""

    try:
        result = subprocess.run(
            ["ps", "-o", "tty=", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return None if name in _UNATTACHED_TTY_NAMES else name


def controlling_terminal_name() -> str | None:
    """Terminal identity of this process's controlling terminal, or None.

    None means the process has no controlling terminal. On Linux this is
    ``ttyname`` of ``/dev/tty``; on macOS, where ``ttyname`` on the
    alias reports the alias itself, the kernel-recorded controlling
    terminal of this pid is used.
    """

    try:
        dev_fd = os.open(_ALIAS_NAME, os.O_RDONLY)
    except OSError:
        return None
    try:
        alias_name = os.ttyname(dev_fd)
        if alias_name != _ALIAS_NAME:
            return alias_name
    finally:
        os.close(dev_fd)
    kernel_tty = _kernel_controlling_tty()
    return None if kernel_tty is None else f"/dev/{kernel_tty}"


def _require_controlling_terminal(tty_fd: int) -> None:
    if not os.isatty(tty_fd):
        raise IngressRefusalError(
            "not-a-tty",
            "operator ingress requires a real controlling terminal (isatty failed)",
        )
    controlling = controlling_terminal_name()
    if controlling is None:
        raise IngressRefusalError(
            "no-controlling-terminal",
            "this process has no controlling terminal; an operator record can "
            "only be minted from the operator's real controlling terminal",
        )
    fd_name = os.ttyname(tty_fd)
    if fd_name != controlling:
        raise IngressRefusalError(
            "controlling-terminal-mismatch",
            f"descriptor {tty_fd} ({fd_name}) is not this process's controlling "
            f"terminal ({controlling}); a self-owned pty can never mint an "
            "operator record",
        )


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
    _validated_runner_class(runner_class)
    if runner_class == "automation":
        raise IngressRefusalError(
            "automation-refused",
            "automation-class runners can never invoke the operator TTY path",
        )
    if tty_fd is None:
        raise IngressRefusalError(
            "not-a-tty",
            "operator ingress requires a real controlling terminal (no fd given)",
        )
    _require_controlling_terminal(tty_fd)
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
    _validated_runner_class(runner_class)
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
    "controlling_terminal_name",
    "record_fixture_operation",
    "record_operation",
]
