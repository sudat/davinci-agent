"""The LIVE Resolve scripting-scope probe (blocker fix; PRD 27 / plan L27).

Positively confirms the scripting scope instead of trusting an unreadable
preference: it connects through the OFFICIAL bridge module (hash-bound by
the readiness report), discovers the scripting server port from this
process's own established connection, inventories the Resolve-owned
listeners' bound address classes (lsof, no name resolution, no egress),
and connect-probes the scripting port from a NON-LOOPBACK local interface
of the same host — recording ACCEPTED vs REFUSED exactly as observed.

Classification (fail-closed on unobserved evidence):
- ``disabled``  — the official module exposes no scripting app.
- ``local-network`` — the scripting port ACCEPTS non-loopback connections
  (positive observation); readiness then additionally requires the
  explicit operator permit.
- ``loopback``  — POSITIVE evidence only: the non-loopback connect-probe
  was attempted and REFUSED and the scripting port's listener inventory
  shows loopback-only binding.
- ``unknown``   — everything else (probe not attempted, port/interface
  discovery failure, listener inventory unobserved, wildcard or
  non-loopback bindings with a refused probe). ``unknown`` keeps
  ``needs_live_verification=true``; wildcard listeners are NEVER
  downgraded to verified loopback.

Every socket this probe opens targets the LOCAL HOST only (loopback or
this machine's own interface addresses); no external egress is attempted,
and the updated report keeps ``network_access_performed = false``.
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
from pathlib import Path
from typing import Final, Literal, Protocol, cast

from services.foundation_io import atomic_write, canonical_model_bytes
from services.resolve_bridge.models import (
    ResolveHostReport,
    ScriptingScopeLiveProbe,
)

PROBE_TIMEOUT_SECONDS: Final = 3.0
LSOF_TIMEOUT_SECONDS: Final = 10.0


class ModuleSeam(Protocol):
    def scriptapp(self, name: str) -> object: ...


class ListenersSeam(Protocol):
    def __call__(self) -> tuple[tuple[int, str], ...]: ...


class InterfacesSeam(Protocol):
    def __call__(self) -> tuple[str, ...]: ...


class ConnectProbeSeam(Protocol):
    def __call__(self, address: str, port: int) -> bool: ...


class OwnTargetsSeam(Protocol):
    def __call__(self) -> tuple[tuple[str, int], ...]: ...


class ResolvePidSeam(Protocol):
    def __call__(self) -> int | None: ...


Scope = Literal["disabled", "loopback", "local-network", "network", "unknown"]

NonLoopbackObservation = Literal[
    "accepted", "refused", "not-attempted-no-non-loopback-interface"
]

_LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})


def _is_loopback_binding(address: str) -> bool:
    host, _, _port = address.rpartition(":")
    if not host:
        return False
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def _loopback_only_binding(port_bindings: tuple[str, ...]) -> bool:
    """True only when the scripting port is POSITIVELY bound loopback-only."""

    return bool(port_bindings) and all(_is_loopback_binding(item) for item in port_bindings)


def classify_scope(
    *,
    enabled: bool,
    non_loopback: NonLoopbackObservation,
    port_bindings: tuple[str, ...],
) -> Scope:
    """Fail-closed classification from the observed probe facts only.

    ``port_bindings`` are the listener inventory rows whose port equals
    the discovered scripting server port. ``loopback`` is only claimable
    from positive evidence: a REFUSED non-loopback connect-probe plus a
    loopback-only listener binding for that port.
    """

    if not enabled:
        return "disabled"
    if non_loopback == "accepted":
        return "local-network"
    if non_loopback == "refused" and _loopback_only_binding(port_bindings):
        return "loopback"
    return "unknown"


def _own_tcp_targets(pid: int) -> tuple[tuple[str, int], ...]:
    """Established TCP (host, port) pairs of this process, via lsof."""
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-a", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=LSOF_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return ()
    targets: list[tuple[str, int]] = []
    for line in result.stdout.splitlines():
        if "ESTABLISHED" not in line or "->" not in line:
            continue
        peer = line.rsplit("->", 1)[-1].split()[0]
        if ":" not in peer:
            continue
        host, _, port_text = peer.rpartition(":")
        if not port_text.isdigit():
            continue
        targets.append((host, int(port_text)))
    return tuple(targets)


def _resolve_listeners() -> tuple[tuple[int, str], ...]:
    """(pid, address) for every TCP LISTEN socket, via lsof."""
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=LSOF_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return ()
    rows: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("COMMAND") or "(LISTEN)" not in stripped:
            continue
        address = stripped[: stripped.index("(LISTEN)")].rstrip().rsplit(" ", 1)[-1]
        if ":" not in address:
            continue
        try:
            pid = int(stripped.split()[1])
        except (IndexError, ValueError):
            continue
        rows.append((pid, address))
    return tuple(rows)


def _resolve_pid(process_name: str) -> int | None:
    try:
        result = subprocess.run(
            ["pgrep", "-x", process_name],
            capture_output=True,
            text=True,
            timeout=LSOF_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    first = result.stdout.splitlines()[0].strip() if result.stdout else ""
    return int(first) if first.isdigit() else None


def _non_loopback_interfaces() -> tuple[str, ...]:
    hostname = socket.gethostname()
    addresses: list[str] = []
    try:
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            address = cast("str", info[4][0])
            if not address.startswith("127.") and address not in addresses:
                addresses.append(address)
    except OSError:
        return ()
    return tuple(addresses)


def _connect_probe(address: str, port: int) -> bool:
    """Bare TCP connect+close from ``address`` to ``address:port`` (on-host)."""
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    connection.settimeout(PROBE_TIMEOUT_SECONDS)
    try:
        connection.bind((address, 0))
        connection.connect((address, port))
    except OSError:
        return False
    else:
        return True
    finally:
        connection.close()


def _default_module_seam(report: ResolveHostReport) -> ModuleSeam:
    from services.resolve_bridge.connection import (  # noqa: PLC0415
        load_script_module,
    )

    return cast("ModuleSeam", load_script_module(report))


def probe_scripting_scope(  # noqa: PLR0913 (injectable seams are the probe contract)
    report: ResolveHostReport,
    *,
    out: Path,
    module_seam: ModuleSeam | None = None,
    listeners_seam: ListenersSeam | None = None,
    interfaces_seam: InterfacesSeam | None = None,
    connect_probe_seam: ConnectProbeSeam | None = None,
    own_targets_seam: OwnTargetsSeam | None = None,
    resolve_pid_seam: ResolvePidSeam | None = None,
) -> ResolveHostReport:
    """Probe the host live and persist the scope-verified report to ``out``."""
    module = module_seam if module_seam is not None else _default_module_seam(report)
    listeners = listeners_seam if listeners_seam is not None else _resolve_listeners
    interfaces = interfaces_seam if interfaces_seam is not None else _non_loopback_interfaces
    connect_probe = connect_probe_seam if connect_probe_seam is not None else _connect_probe
    if own_targets_seam is not None:
        own_targets = own_targets_seam
    else:
        own_targets = lambda: _own_tcp_targets(_pid())  # noqa: E731
    resolve_pid = (
        resolve_pid_seam() if resolve_pid_seam is not None else _live_resolve_pid()
    )
    scripting_app = module.scriptapp("Resolve")
    enabled = scripting_app is not None
    transport = "official-bridge-module"
    server_port: int | None = None
    if enabled:
        for host, port in own_targets():
            if host in ("127.0.0.1", "localhost", "::1"):
                server_port = port
                transport = f"tcp 127.0.0.1 -> 127.0.0.1:{port}"
                break

    bound: tuple[str, ...] = ()
    if enabled and server_port is not None and resolve_pid is not None:
        bound = tuple(
            address for pid, address in listeners() if pid == resolve_pid
        )

    local_addresses = interfaces()
    non_loopback: NonLoopbackObservation = "not-attempted-no-non-loopback-interface"
    probed_via = ""
    if enabled and server_port is not None and local_addresses:
        address = local_addresses[0]
        probed_via = f"{address}:{server_port}"
        non_loopback = "accepted" if connect_probe(address, server_port) else "refused"

    port_bindings: tuple[str, ...] = ()
    if enabled and server_port is not None and resolve_pid is not None:
        port_bindings = tuple(
            address
            for pid, address in listeners()
            if pid == resolve_pid and address.rpartition(":")[2] == str(server_port)
        )

    scope = classify_scope(
        enabled=enabled, non_loopback=non_loopback, port_bindings=port_bindings
    )
    note = (
        f"scripting_enabled={enabled} transport={transport} server_port={server_port} "
        f"listener_bound={bound or 'unobserved'} "
        f"scripting_port_bindings={port_bindings or 'unobserved'} "
        f"non_loopback_probe={non_loopback}"
        f"{f' via {probed_via}' if probed_via else ''}; probed addresses target this "
        "host only (loopback / own interfaces), no external egress"
    )
    probe = ScriptingScopeLiveProbe(
        scripting_enabled=enabled,
        transport_observed=transport,
        scripting_server_port=server_port,
        listener_bound_addresses=bound,
        non_loopback_probe=non_loopback,
        evidence_note=note,
    )
    posture = report.scripting.model_copy(
        update={
            "remote_access": scope,
            "needs_live_verification": scope == "unknown",
            "reason": (
                f"live scripting-scope probe ({scope}): {note}"
                if scope != "unknown"
                else "live scripting-scope probe left the scope unresolved "
                f"(unobserved evidence): {note}"
            ),
            "live_probe": probe,
        }
    )
    updated = report.model_copy(update={"scripting": posture})
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(out, canonical_model_bytes(updated))
    return updated


def _live_resolve_pid() -> int | None:
    return _resolve_pid("Resolve")


def _pid() -> int:
    import os  # noqa: PLC0415

    return os.getpid()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scripting_scope_probe")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = ResolveHostReport.model_validate_json(arguments.report.read_bytes())
        updated = probe_scripting_scope(report, out=arguments.out)
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2
    probe = updated.scripting.live_probe
    scope = updated.scripting.remote_access
    note = probe.evidence_note if probe else "n/a"
    unresolved = " needs_live_verification=true" if scope == "unknown" else ""
    print(f"scripting-scope: {scope} probe={note}{unresolved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "classify_scope",
    "probe_scripting_scope",
]
