from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.resolve_bridge.models import ResolveHostReport
from services.resolve_bridge.readiness import HostReadinessError, load_host_report

FIXTURES = Path("tests/fixtures/resolve-host")


def test_readonly_report_passes_when_network_posture_is_safe() -> None:
    report = load_host_report(FIXTURES / "safe-readonly.json")

    assert report.scripting.network_access_performed is False
    assert report.scripting.runtime_policy == "loopback-only"


def test_unsafe_network_fixture_fails_closed() -> None:
    with pytest.raises(HostReadinessError, match="unsafe Resolve scripting exposure"):
        load_host_report(FIXTURES / "unsafe-network.json")


def test_static_unknowns_remain_explicit() -> None:
    report = load_host_report(FIXTURES / "safe-readonly.json")

    assert report.application.edition.value == "unverified"
    assert report.application.edition.needs_live_verification is True
    assert report.scripting.needs_live_verification is True


def test_host_report_schema_matches_strict_model() -> None:
    schema = json.loads(Path("schemas/resolve-host-report.schema.json").read_text())

    assert schema == ResolveHostReport.model_json_schema()
