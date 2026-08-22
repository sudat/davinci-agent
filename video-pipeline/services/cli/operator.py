"""The typed local operator CLI (``python -m services.cli``).

A fixed registry of pipeline operations — every command an operator needs
is a registered ``<name>`` that dispatches to one existing service module
(``python -m <module> <args>`` with an explicit argv; no shell, no PATH
lookup). Anything else is a typed refusal: arbitrary shell, arbitrary
path, network, and UI commands are refused by name with a typed code and
never executed. ``--help`` lists every operation plus the exact
FINAL_APPROVED Global Review Report v1 contract.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from typing import Final

REFUSAL_SHELL: Final = "arbitrary-shell-command"
REFUSAL_PATH: Final = "arbitrary-path-command"
REFUSAL_NETWORK: Final = "network-command"
REFUSAL_UI: Final = "ui-command"
REFUSAL_UNKNOWN: Final = "unknown-operation"

FORBIDDEN_SHELL: Final = frozenset(
    {"sh", "bash", "zsh", "fish", "shell", "exec", "execute", "spawn", "eval", "system"}
)
FORBIDDEN_PATH: Final = frozenset(
    {
        "cat", "read", "read-file", "write", "write-file", "rm", "del", "delete",
        "delete-file", "mv", "move", "cp", "copy", "touch", "mkdir", "edit",
        "edit-file", "open", "ls", "list", "list-files", "find", "path",
    }
)
FORBIDDEN_NETWORK: Final = frozenset(
    {
        "curl", "wget", "http", "https", "ftp", "ssh", "scp", "net", "network",
        "fetch", "download", "upload", "request", "post",
    }
)
FORBIDDEN_UI: Final = frozenset(
    {"ui", "gui", "serve", "server", "dashboard", "browser", "web", "window"}
)


@dataclass(frozen=True, slots=True)
class Operation:
    name: str
    module: str
    summary: str


OPERATIONS: Final[tuple[Operation, ...]] = (
    Operation("ingest", "services.ingest.cli", "register a fixture episode source"),
    Operation("normalize", "services.normalize.cli", "normalize sources to the edit mezzanine"),
    Operation("conform", "services.conform.cli", "conform coordinates for one source"),
    Operation("conform-map", "services.conform.map_cli", "emit the conform map"),
    Operation("phase1", "services.cli.phase1", "run the phase-1 editorial chain on an episode"),
    Operation("review", "services.cli.review", "apply natural-language review instructions"),
    Operation(
        "checkpoint", "services.cli.checkpoint", "show/record/export operator checkpoints"
    ),
    Operation(
        "convert-review",
        "services.approvals.review_convert",
        "convert a raw review-work/debugging response to global-review-v1",
    ),
    Operation("preview", "services.preview.cli", "render the low-resolution editorial preview"),
    Operation("qc", "services.qc.run", "run deterministic QC / build the QC policy"),
    Operation("run-gate", "services.job_runner.run_gate", "execute a frozen phase gate"),
    Operation("verify-policy", "services.gates.verify_policy", "verify a frozen gate policy"),
    Operation("gate-p1-faults", "services.job_runner.gate_p1_faults", "phase-1 fault fixtures"),
    Operation("gate-p2-faults", "services.job_runner.gate_p2_faults", "phase-2 fault fixtures"),
    Operation("gate-p3-faults", "services.job_runner.gate_p3_faults", "phase-3 fault fixtures"),
    Operation("freeze-phase", "services.fixtures.freeze_phase", "freeze a phase gate policy"),
    Operation("materialize", "services.fixtures.materialize", "materialize frozen fixtures"),
    Operation("toolchain-verify", "services.toolchain.verify", "verify the pinned toolchain"),
    Operation("mcp-doctor", "services.cli.mcp_doctor", "verify the pinned davinci-resolve-mcp"),
    Operation("episode0", "services.cli.episode0", "episode-0 longitudinal baseline tooling"),
    Operation(
        "toolchain-ledger", "services.toolchain.execution_ledger", "execution ledger events"
    ),
    Operation("retention-gc", "services.retention.gc", "plan-first retention garbage collection"),
    Operation("metrics-report", "services.metrics.report", "derive and validate metrics"),
    Operation("check-scope", "services.policy.check_scope", "enforce scope boundaries"),
    Operation("evidence-append", "services.evidence.append_event", "append an evidence event"),
    Operation("evidence-verify", "services.evidence.verify", "verify evidence bundles"),
    Operation("preflight", "services.execution.preflight", "worker preflight binding"),
    Operation("export-schemas", "services.contracts.export_schemas", "export JSON schemas"),
    Operation("qa-run-todo", "services.qa.run_todo", "run a Todo QA matrix"),
    Operation("cockpit", "services.cli.cockpit", "serve the loopback-only episode cockpit API"),
)

_REPORT_NAMES: Final = (
    "goal-constraints.json",
    "code-quality.json",
    "security.json",
    "hands-on-qa.json",
    "context-mining.json",
    "debugging.json",
)


class OperatorRefusalError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _refuse(code: str, name: str) -> OperatorRefusalError:
    return OperatorRefusalError(
        code,
        f"'{name}' is not an operator operation; this CLI never executes "
        "arbitrary shell, path, network, or UI commands — see --help for the registry",
    )


def refuse(name: str) -> OperatorRefusalError:
    if name in FORBIDDEN_SHELL:
        return _refuse(REFUSAL_SHELL, name)
    if name in FORBIDDEN_PATH:
        return _refuse(REFUSAL_PATH, name)
    if name in FORBIDDEN_NETWORK:
        return _refuse(REFUSAL_NETWORK, name)
    if name in FORBIDDEN_UI:
        return _refuse(REFUSAL_UI, name)
    return OperatorRefusalError(
        REFUSAL_UNKNOWN, f"'{name}' is not a registered operation; see --help"
    )


def help_text() -> str:
    lines = [
        "usage: python -m services.cli <operation> [operation arguments]",
        "",
        "Typed local operator CLI: every pipeline operation is a registered",
        "subcommand dispatched to its service module (no shell, no network, no UI).",
        "",
        "operations:",
    ]
    lines.extend(f"  {operation.name:<18} {operation.summary}" for operation in OPERATIONS)
    lines += [
        "",
        "FINAL_APPROVED checkpoint contract (Global Review Report v1):",
        "  checkpoint record --purpose FINAL_APPROVED --work-id --git-sha \\",
        "      --candidate-id --report-dir --decision --out",
        f"  --report-dir must hold exactly six reports: {', '.join(_REPORT_NAMES)}.",
        "  Each file is {schema_version:global-review-v1, lane, full_sha, candidate_id,",
        "  verdict, raw_response_sha256, artifact_hashes, findings}; every verdict must",
        "  be APPROVE, one identical full_sha and candidate_id across the set; the",
        "  canonical report-set sha256 is bound into the record; the operator's local",
        "  TTY and uid are captured (automation/non-TTY is refused); the candidate must",
        "  not be on the local revocation list; stale reports (sha != the declared work",
        "  state) are refused. Use 'convert-review' to build reports from raw lane",
        "  responses (an omitted STOP instruction is a typed failure).",
        "",
        "typed refusals: arbitrary-shell-command, arbitrary-path-command,",
        "  network-command, ui-command, unknown-operation (exit 2).",
    ]
    return "\n".join(lines)


def dispatch(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(help_text())
        return 0 if argv else 2
    name, rest = argv[0], argv[1:]
    registered = {operation.name: operation.module for operation in OPERATIONS}
    if name not in registered:
        print(f"refused: {refuse(name)}", file=sys.stderr)
        return 2
    completed = subprocess.run([sys.executable, "-m", registered[name], *rest], check=False)
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    return dispatch(sys.argv[1:] if argv is None else argv)


__all__ = ["OPERATIONS", "Operation", "OperatorRefusalError", "dispatch", "help_text", "main"]
