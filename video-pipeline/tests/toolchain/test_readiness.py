from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.resolve_bridge.models import ResolveHostReport
from services.resolve_bridge.readiness import (
    HostReadinessError,
    PermitHostFingerprint,
    ScriptingPermit,
    load_host_report,
    validate_host_report,
)

FIXTURES = Path("tests/fixtures/resolve-host")


def _repo_permit() -> ScriptingPermit:
    return ScriptingPermit.model_validate(
        json.loads(Path("config/security/scripting-scope-permit.json").read_text())
    )


def _matching_permit(**overrides: str) -> ScriptingPermit:
    """A permit that binds exactly the live-local-network fixture's reality."""

    base = {
        "schema_version": "resolve-scripting-scope-permit-v2",
        "permitted_scope": "local-network",
        "granted_by": "owner",
        "host_note": "test permit bound to the fixture host",
        "granted_date": "2026-08-01",
        "revalidate_by": "2026-12-01",
        "host_fingerprint": {
            "macos_version": "26.5.2",
            "macos_build": "25F84",
            "architecture": "arm64",
        },
        "resolve_version": "21.0.4",
        "resolve_build": "21.0.40005",
        "observed_listener_scope": "local-network",
    }
    base.update(overrides)
    return ScriptingPermit.model_validate(base)


def _local_network_report() -> ResolveHostReport:
    return ResolveHostReport.model_validate_json(
        (FIXTURES / "live-local-network.json").read_bytes()
    )


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
    report = _local_network_report()
    with pytest.raises(HostReadinessError, match="explicit operator permit"):
        validate_host_report(report, permit=None)
    validate_host_report(report, permit=_matching_permit())


def test_live_local_network_auto_permit_via_repo_config() -> None:
    report = load_host_report(FIXTURES / "live-local-network.json")
    assert report.scripting.remote_access == "local-network"


def test_repo_permit_refuses_unapproved_current_host_drift() -> None:
    permit = _repo_permit()
    report = ResolveHostReport.model_validate_json(
        Path("/Users/stc/Developer/davinci-agent/private/vendor/toolchain/resolve-host.json").read_bytes()
    )
    with pytest.raises(HostReadinessError) as excinfo:
        validate_host_report(report, permit=permit)
    assert excinfo.value.code == "permit-host-mismatch"


def test_permit_host_fingerprint_mismatch_is_typed_refusal() -> None:
    report = _local_network_report()
    permit = _matching_permit()
    drifted = permit.model_copy(
        update={
            "host_fingerprint": PermitHostFingerprint(
                macos_version="26.4.0", macos_build="25A80", architecture="arm64"
            )
        }
    )
    with pytest.raises(HostReadinessError) as excinfo:
        validate_host_report(report, permit=drifted)
    assert excinfo.value.code == "permit-host-mismatch"


def test_permit_resolve_version_mismatch_is_typed_refusal() -> None:
    report = _local_network_report()
    permit = _matching_permit(resolve_version="21.1.0")
    with pytest.raises(HostReadinessError) as excinfo:
        validate_host_report(report, permit=permit)
    assert excinfo.value.code == "permit-host-mismatch"


def test_permit_resolve_build_mismatch_is_typed_refusal() -> None:
    report = _local_network_report()
    permit = _matching_permit(resolve_build="21.1.00001")
    with pytest.raises(HostReadinessError) as excinfo:
        validate_host_report(report, permit=permit)
    assert excinfo.value.code == "permit-host-mismatch"


def test_permit_scope_binding_is_typed_to_local_network() -> None:
    """A permit cannot even be constructed for a different observed scope."""

    with pytest.raises(ValidationError):
        _matching_permit(observed_listener_scope="loopback")  # type: ignore[arg-type]


def test_permit_binding_field_drift_cannot_bless_foreign_report() -> None:
    """A loopback-drifted report is not blessed by a local-network permit."""

    report = _local_network_report()
    drifted = report.model_copy(
        update={
            "scripting": report.scripting.model_copy(
                update={"remote_access": "loopback"}
            )
        }
    )
    validate_host_report(drifted, permit=_matching_permit())
    foreign = report.model_copy(
        update={
            "host": report.host.model_copy(update={"macos_build": "25Z99"})
        }
    )
    with pytest.raises(HostReadinessError) as excinfo:
        validate_host_report(foreign, permit=_matching_permit())
    assert excinfo.value.code == "permit-host-mismatch"


def test_permit_requires_positive_accepted_observation() -> None:
    """A claimed local-network report without accepted probe evidence fails."""

    report = _local_network_report()
    unobserved = report.scripting.model_copy(
        update={"live_probe": None, "remote_access": "local-network"}
    )
    forged = report.model_copy(update={"scripting": unobserved})
    with pytest.raises(HostReadinessError) as excinfo:
        validate_host_report(forged, permit=_matching_permit())
    assert excinfo.value.code == "scope-observation-missing"


def test_expired_permit_is_typed_refusal() -> None:
    report = _local_network_report()
    permit = _matching_permit(revalidate_by="2026-08-15")
    with pytest.raises(HostReadinessError) as excinfo:
        validate_host_report(report, permit=permit, today=date(2026, 8, 21))
    assert excinfo.value.code == "permit-expired"


def test_permit_valid_through_revalidate_date() -> None:
    report = _local_network_report()
    permit = _matching_permit(revalidate_by="2026-08-21")
    validate_host_report(report, permit=permit, today=date(2026, 8, 21))


def test_permit_with_future_grant_date_is_typed_refusal() -> None:
    report = _local_network_report()
    permit = _matching_permit(granted_date="2026-09-01")
    with pytest.raises(HostReadinessError) as excinfo:
        validate_host_report(report, permit=permit, today=date(2026, 8, 21))
    assert excinfo.value.code == "permit-invalid"


def test_permit_with_malformed_dates_is_typed_refusal() -> None:
    report = _local_network_report()
    permit = _matching_permit(revalidate_by="not-a-date")
    with pytest.raises(HostReadinessError) as excinfo:
        validate_host_report(report, permit=permit)
    assert excinfo.value.code == "permit-invalid"


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
