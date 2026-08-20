"""Local-listener security probe (Todo 66).

``probe_local_listeners --require-loopback --out report.json`` inventories
every TCP listen socket on the local machine via ``lsof -nP -iTCP
-sTCP:LISTEN`` and reports, against real state, whether pipeline-owned
processes bind loopback addresses only.

Honesty rules:
- The scan is read-only and performs no network egress (``lsof -n`` disables
  name resolution, ``ps`` is local). It never starts or rebinds services.
- Only pipeline-owned listeners (classified by command line: this repo's
  path, a ``-m services.`` module, or the ``services/`` layout) count toward
  the ``--require-loopback`` verdict. Unrelated machine listeners are
  recorded in the report but never judged.
- The report records ``pass_verdict`` and the raw listener set as observed,
  so a caller can inspect exactly what was seen.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Final

from services.security.probe_models import (
    PROBE_SCHEMA_VERSION,
    ListenerFamily,
    ListenerRecord,
    ListenerReport,
)

PIPELINE_ROOT = Path(__file__).resolve().parents[2]
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "[::1]", "localhost"})
LSOF_NO_LISTENERS_EXIT: Final = 1
MIN_IPV6_COLONS: Final = 2
WILDCARD_HOSTS: Final = frozenset({"*", "0.0.0.0", "::", "[::]", ""})  # noqa: S104 (parsed lsof data)
LISTEN_SUFFIX: Final = " (LISTEN)"


class ProbeError(Exception):
    """The scanner itself failed; no honest verdict can be produced."""


def _classify_address(address: str) -> tuple[ListenerFamily, bool]:
    """Return (family, loopback) for a parsed lsof host:port address."""
    if address.startswith("["):
        host = address[1 : address.index("]")]
        family: ListenerFamily = "ipv6"
    elif address.count(":") >= MIN_IPV6_COLONS:
        host = address.rsplit(":", 1)[0]
        family = "ipv6"
    else:
        host = address.rsplit(":", 1)[0]
        family = "ipv4"
    if host.startswith("::ffff:") and host.endswith("127.0.0.1"):
        return family, True
    if host in WILDCARD_HOSTS:
        return family, False
    return family, host in LOOPBACK_HOSTS


def _parse_lsof_line(line: str) -> tuple[int, str, int | None, bool, ListenerFamily] | None:
    """Parse one lsof TCP line into (pid, address, port, loopback, family).

    Returns None for the header line or any unparseable line.
    """
    if not line.strip() or line.startswith("COMMAND"):
        return None
    if not line.endswith(LISTEN_SUFFIX):
        return None
    address = line[: -len(LISTEN_SUFFIX)].rsplit(" ", 1)[-1]
    if not address or ":" not in address:
        return None
    try:
        pid = int(line.split()[1])
    except (IndexError, ValueError):
        return None
    port_text = address.rsplit(":", 1)[-1]
    try:
        port = int(port_text)
    except ValueError:
        port = None
    family, loopback = _classify_address(address)
    return pid, address, port, loopback, family


def is_pipeline_command(command: str) -> bool:
    """True if a process command line belongs to this pipeline.

    Classification is conservative and scoped to our own services and
    bridge: the probe repo root path, a ``-m services.`` module invocation,
    or the ``services/`` layout under the probe root. Third-party processes
    (including the Resolve host application) are never counted.
    """
    return (
        str(PIPELINE_ROOT) in command
        or "-m services." in command
        or " services/" in command
        or command.startswith("services/")
    )


def _command_for_pid(pid: int, timeout: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return result.stdout.strip()

def scan_listeners(timeout: int = 10) -> tuple[ListenerRecord, ...]:
    """Inventory TCP listen sockets and classify their owners.

    Runs ``lsof -nP -iTCP -sTCP:LISTEN`` (no name resolution, no egress) and
    ``ps -o command=`` for each PID to decide pipeline ownership. A process
    that cannot be classified is recorded as ``pipeline=False`` — it never
    counts against a ``--require-loopback`` verdict. A scanner failure
    (tool missing, nonzero exit other than "no listeners") raises
    ``ProbeError`` instead of fabricating an empty pass.
    """

    try:
        result = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as error:
        raise ProbeError(f"listener scanner failed to run: {error}") from error
    if result.returncode not in (0, LSOF_NO_LISTENERS_EXIT):
        raise ProbeError(
            f"listener scanner exited {result.returncode}: {result.stderr.strip()}"
        )
    records: list[ListenerRecord] = []
    for line in result.stdout.splitlines():
        parsed = _parse_lsof_line(line)
        if parsed is None:
            continue
        pid, address, port, loopback, family = parsed
        command = _command_for_pid(pid, timeout=timeout)
        process = command or line.split()[0]
        records.append(
            ListenerRecord(
                pid=pid,
                process=process,
                address=address,
                port=port,
                family=family,
                loopback=loopback,
                pipeline=is_pipeline_command(process),
            )
        )
    return tuple(records)


def build_report(
    listeners: tuple[ListenerRecord, ...],
    *,
    require_loopback: bool,
    scanned_at_unix: int,
) -> ListenerReport:
    pipeline_listeners = [r for r in listeners if r.pipeline]
    exposed = [r for r in pipeline_listeners if not r.loopback]
    if exposed:
        detail = ", ".join(f"{r.process} pid={r.pid} {r.address}" for r in exposed)
        verdict = f"non-loopback-pipeline-listener: {detail}"
        return ListenerReport(
            schema_version=PROBE_SCHEMA_VERSION,
            scanned_at_unix=scanned_at_unix,
            require_loopback=require_loopback,
            pass_verdict=False,
            verdict=verdict,
            listeners=listeners,
        )
    if require_loopback:
        if pipeline_listeners:
            verdict = (
                f"loopback-safe: {len(pipeline_listeners)} pipeline listener(s) "
                "bound to loopback only"
            )
        else:
            verdict = "no-pipeline-listeners: nothing exposed"
    else:
        verdict = f"informational: {len(pipeline_listeners)} pipeline listener(s) observed"
    return ListenerReport(
        schema_version=PROBE_SCHEMA_VERSION,
        scanned_at_unix=scanned_at_unix,
        require_loopback=require_loopback,
        pass_verdict=True,
        verdict=verdict,
        listeners=listeners,
    )


def write_report(report: ListenerReport, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="probe_local_listeners")
    parser.add_argument("--require-loopback", action="store_true", default=False)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=10)
    arguments = parser.parse_args(argv)

    try:
        listeners = scan_listeners(timeout=arguments.timeout)
    except ProbeError as error:
        print(f"probe-error: {error}", file=sys.stderr)
        return 2
    report = build_report(
        listeners,
        require_loopback=arguments.require_loopback,
        scanned_at_unix=int(time.time()),
    )
    write_report(report, arguments.out)
    print(f"probe: {report.verdict}")
    print(f"report written to {arguments.out}")
    return 0 if report.pass_verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
