"""The live scripting-scope probe: positive, honest scope classification.

The probe connects through the official bridge module, discovers the
scripting server port from the machine's own established connection,
inventories the listener's bound address class, and connect-probes the
scripting port from a non-loopback local interface — recording ACCEPTED
vs REFUSED honestly. Classification: disabled / loopback / local-network.
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


def test_loopback_scope_when_non_loopback_probe_refused(tmp_path: Path) -> None:
    updated = probe_scripting_scope(
        _report(),
        out=tmp_path / "probed.json",
        module_seam=_FakeModule(),
        listeners_seam=lambda: ((42237, "*:49152"), (42237, "*:15000")),
        interfaces_seam=lambda: ("192.168.1.4",),
        connect_probe_seam=lambda address, port: False,
        own_targets_seam=lambda: (("127.0.0.1", 49152),),
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
    probe = updated.scripting.live_probe
    assert probe is not None
    assert probe.non_loopback_probe == "accepted"


def test_no_non_loopback_interface_records_not_attempted(tmp_path: Path) -> None:
    updated = probe_scripting_scope(
        _report(),
        out=tmp_path / "probed.json",
        module_seam=_FakeModule(),
        listeners_seam=lambda: ((42237, "127.0.0.1:52525"),),
        interfaces_seam=lambda: (),
        connect_probe_seam=lambda address, port: True,
        own_targets_seam=lambda: (("127.0.0.1", 49152),),
    )
    assert updated.scripting.remote_access == "loopback"
    probe = updated.scripting.live_probe
    assert probe is not None
    assert probe.non_loopback_probe == "not-attempted-no-non-loopback-interface"


def test_classify_scope_is_pure_and_honest() -> None:
    assert classify_scope(enabled=False, accepted=False, attempted=False) == "disabled"
    assert classify_scope(enabled=True, accepted=False, attempted=True) == "loopback"
    assert classify_scope(enabled=True, accepted=True, attempted=True) == "local-network"
    assert (
        classify_scope(enabled=True, accepted=True, attempted=False) == "loopback"
    )


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
