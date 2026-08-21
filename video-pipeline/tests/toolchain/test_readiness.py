from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.resolve_bridge.models import ResolveHostReport
from services.resolve_bridge.readiness import (
    HostReadinessError,
    ScriptingPermit,
    load_host_report,
    validate_host_report,
)

FIXTURES = Path("tests/fixtures/resolve-host")


def test_unverified_scope_fails_closed() -> None:
    """remote_access=unknown + needs_live_verification=true must FAIL readiness."""
    with pytest.raises(HostReadinessError, match="unverified"):
        load_host_report(FIXTURES / "safe-readonly.json")


def test_live_verified_loopback_scope_passes() -> None:
    report = load_host_report(FIXTURES / "live-loopback.json")

    assert report.scripting.network_access_performed is False
    assert report.scripting.runtime_policy == "loopback-only"
    assert report.scripting.remote_access == "loopback"
    assert report.scripting.needs_live_verification is False
    assert report.scripting.live_probe is not None
    assert report.scripting.live_probe.scripting_enabled is True


def test_live_local_network_requires_explicit_permit() -> None:
    report = ResolveHostReport.model_validate_json(
        (FIXTURES / "live-local-network.json").read_bytes()
    )
    with pytest.raises(HostReadinessError, match="explicit operator permit"):
        validate_host_report(report, permit=None)
    permit = ScriptingPermit.model_validate(
        json.loads(Path("config/security/scripting-scope-permit.json").read_text())
    )
    validate_host_report(report, permit=permit)


def test_live_local_network_auto_permmit_via_repo_config() -> None:
    report = load_host_report(FIXTURES / "live-local-network.json")
    assert report.scripting.remote_access == "local-network"


def test_unsafe_network_fixture_fails_closed() -> None:
    with pytest.raises(HostReadinessError, match="unsafe Resolve scripting exposure"):
        load_host_report(FIXTURES / "unsafe-network.json")


def test_static_unknowns_remain_explicit_in_live_reports() -> None:
    report = load_host_report(FIXTURES / "live-loopback.json")

    assert report.application.edition.value == "unverified"
    assert report.application.edition.needs_live_verification is True


def test_host_report_schema_matches_strict_model() -> None:
    schema = json.loads(Path("schemas/resolve-host-report.schema.json").read_text())

    assert schema == ResolveHostReport.model_json_schema()
