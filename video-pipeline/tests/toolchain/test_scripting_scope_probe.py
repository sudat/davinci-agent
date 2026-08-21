"""The live scripting-scope probe: positive, honest scope classification.

The probe connects through the official bridge module, discovers the
scripting server port from the machine's own established connection,
inventories the listener's bound address class, and connect-probes the
scripting port from a non-loopback local interface — recording ACCEPTED
vs REFUSED honestly. Classification is fail-closed: ``loopback`` requires
POSITIVE evidence (non-loopback probe REFUSED and the scripting port's
listener inventory shows loopback-only binding); wildcard listeners and
unattempted/failed probes classify as ``unknown`` with
``needs_live_verification=true``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from services.resolve_bridge.models import ResolveHostReport
from services.resolve_bridge.scripting_scope_probe import classify_scope, probe_scripting_scope


class ListenersSeam(Protocol):
    def __call__(self) -> tuple[tuple[int, str], ...]: ...


class InterfacesSeam(Protocol):
    def __call__(self) -> tuple[str, ...]: ...


class ProbeSeam(Protocol):
    def __call__(self, address: str, port: int) -> bool: ...


def _report() -> ResolveHostReport:
    return ResolveHostReport.model_validate_json(
        Path("tests/fixtures/resolve-host/safe-readonly.json").read_bytes()
    )


class _FakeModule:
    def scriptapp(self, name: str) -> object:
        class _Resolve:
            @staticmethod
            def GetProductName() -> str:  # noqa: N802
                return "DaVinci Resolve Studio"

            @staticmethod
            def GetVersion() -> list[int | str]:  # noqa: N802
                return [21, 0, 4, 21_004_005]

            @staticmethod
            def GetVersionString() -> str:  # noqa: N802
                return "21.0.4.5"

        return _Resolve() if name == "Resolve" else None


class _NoAppModule:
    def scriptapp(self, name: str) -> object:
        return None


def test_disabled_scope_when_scriptapp_unavailable(tmp_path: Path) -> None:
    updated = probe_scripting_scope(
        _report(),
        out=tmp_path / "probed.json",
        module_seam=_NoAppModule(),
        listeners_seam=lambda: (),
        interfaces_seam=lambda: ("192.168.1.4",),
        connect_probe_seam=lambda address, port: True,
    )
    assert updated.scripting.remote_access == "disabled"
    assert updated.scripting.needs_live_verification is False
    assert updated.scripting.live_probe is not None
    assert updated.scripting.live_probe.scripting_enabled is False


def test_loopback_scope_requires_positive_refused_and_loopback_binding(
    tmp_path: Path,
) -> None:
    updated = probe_scripting_scope(
        _report(),
        out=tmp_path / "probed.json",
        module_seam=_FakeModule(),
        listeners_seam=lambda: ((42237, "127.0.0.1:49152"), (42237, "127.0.0.1:15000")),
        interfaces_seam=lambda: ("192.168.1.4",),
        connect_probe_seam=lambda address, port: False,
        own_targets_seam=lambda: (("127.0.0.1", 49152),),
        resolve_pid_seam=lambda: 42237,
    )
    assert updated.scripting.remote_access == "loopback"
    assert updated.scripting.needs_live_verification is False
    probe = updated.scripting.live_probe
    assert probe is not None
    assert probe.non_loopback_probe == "refused"
    assert "192.168.1.4" in probe.evidence_note
    assert (tmp_path / "probed.json").is_file()
    reloaded = ResolveHostReport.model_validate_json((tmp_path / "probed.json").read_bytes())
    assert reloaded.scripting.remote_access == "loopback"
    assert reloaded.scripting.needs_live_verification is False


def test_wildcard_listener_refused_is_unknown_not_loopback(tmp_path: Path) -> None:
    """Wildcard bindings must never be downgraded to verified loopback."""

    updated = probe_scripting_scope(
        _report(),
        out=tmp_path / "probed.json",
        module_seam=_FakeModule(),
        listeners_seam=lambda: ((42237, "*:49152"), (42237, "*:15000")),
        interfaces_seam=lambda: ("192.168.1.4",),
        connect_probe_seam=lambda address, port: False,
        own_targets_seam=lambda: (("127.0.0.1", 49152),),
        resolve_pid_seam=lambda: 42237,
    )
    assert updated.scripting.remote_access == "unknown"
    assert updated.scripting.needs_live_verification is True


def test_refused_with_unobserved_listener_inventory_is_unknown(tmp_path: Path) -> None:
    updated = probe_scripting_scope(
        _report(),
        out=tmp_path / "probed.json",
        module_seam=_FakeModule(),
        listeners_seam=lambda: (),
        interfaces_seam=lambda: ("192.168.1.4",),
        connect_probe_seam=lambda address, port: False,
        own_targets_seam=lambda: (("127.0.0.1", 49152),),
    )
    assert updated.scripting.remote_access == "unknown"
    assert updated.scripting.needs_live_verification is True


def test_local_network_scope_when_non_loopback_accepted(tmp_path: Path) -> None:
    updated = probe_scripting_scope(
        _report(),
        out=tmp_path / "probed.json",
        module_seam=_FakeModule(),
        listeners_seam=lambda: ((42237, "*:49152"),),
        interfaces_seam=lambda: ("192.168.1.4",),
        connect_probe_seam=lambda address, port: True,
        own_targets_seam=lambda: (("127.0.0.1", 49152),),
    )
    assert updated.scripting.remote_access == "local-network"
    assert updated.scripting.needs_live_verification is False
    probe = updated.scripting.live_probe
    assert probe is not None
    assert probe.non_loopback_probe == "accepted"


def test_no_non_loopback_interface_is_unknown_unverified(tmp_path: Path) -> None:
    """Not-attempted (no non-loopback interface) must NOT claim loopback."""

    updated = probe_scripting_scope(
        _report(),
        out=tmp_path / "probed.json",
        module_seam=_FakeModule(),
        listeners_seam=lambda: ((42237, "127.0.0.1:49152"),),
        interfaces_seam=lambda: (),
        connect_probe_seam=lambda address, port: True,
        own_targets_seam=lambda: (("127.0.0.1", 49152),),
    )
    assert updated.scripting.remote_access == "unknown"
    assert updated.scripting.needs_live_verification is True
    probe = updated.scripting.live_probe
    assert probe is not None
    assert probe.non_loopback_probe == "not-attempted-no-non-loopback-interface"


def test_port_discovery_failure_is_unknown_unverified(tmp_path: Path) -> None:
    """No discovered scripting port -> no positive evidence -> unknown."""

    updated = probe_scripting_scope(
        _report(),
        out=tmp_path / "probed.json",
        module_seam=_FakeModule(),
        listeners_seam=lambda: ((42237, "127.0.0.1:49152"),),
        interfaces_seam=lambda: ("192.168.1.4",),
        connect_probe_seam=lambda address, port: False,
        own_targets_seam=lambda: (),
    )
    assert updated.scripting.remote_access == "unknown"
    assert updated.scripting.needs_live_verification is True


def test_classify_scope_is_pure_fail_closed() -> None:
    not_attempted = "not-attempted-no-non-loopback-interface"
    loopback_binding = ("127.0.0.1:49152",)
    wildcard_binding = ("*:49152",)

    assert classify_scope(enabled=False, non_loopback=not_attempted, port_bindings=()) == (
        "disabled"
    )
    assert classify_scope(enabled=True, non_loopback="accepted", port_bindings=()) == (
        "local-network"
    )
    assert classify_scope(
        enabled=True, non_loopback="refused", port_bindings=loopback_binding
    ) == "loopback"
    # Wildcard listener never downgraded to loopback.
    assert classify_scope(
        enabled=True, non_loopback="refused", port_bindings=wildcard_binding
    ) == "unknown"
    # Refused but listener inventory unobserved.
    assert classify_scope(enabled=True, non_loopback="refused", port_bindings=()) == "unknown"
    # Unattempted is never loopback.
    assert classify_scope(
        enabled=True, non_loopback=not_attempted, port_bindings=loopback_binding
    ) == "unknown"
    # Mixed loopback + wildcard bindings stay unknown.
    assert classify_scope(
        enabled=True,
        non_loopback="refused",
        port_bindings=("127.0.0.1:49152", "0.0.0.0:49152"),
    ) == "unknown"


def test_probed_report_json_round_trips(tmp_path: Path) -> None:
    updated = probe_scripting_scope(
        _report(),
        out=tmp_path / "probed.json",
        module_seam=_FakeModule(),
        listeners_seam=lambda: ((42237, "*:49152"),),
        interfaces_seam=lambda: ("192.168.1.4",),
        connect_probe_seam=lambda address, port: False,
        own_targets_seam=lambda: (("127.0.0.1", 49152),),
    )
    payload = json.loads((tmp_path / "probed.json").read_text())
    assert payload["scripting"]["live_probe"]["schema_version"] == (
        "resolve-scripting-scope-probe-v1"
    )
    assert updated.scripting.live_probe is not None
