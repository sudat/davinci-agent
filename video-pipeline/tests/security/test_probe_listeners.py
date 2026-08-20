"""Attack class 4: network exposure — the listener probe is honest.

Unit coverage for the probe's classification and verdict logic plus an
honest live smoke: the scan runs against the REAL machine state, records
whatever it sees, and never judges third-party listeners. The acceptance
run (``probe_local_listeners --require-loopback``) executes the same code
against the real state; these tests additionally prove the failure modes:
a non-loopback pipeline listener fails the verdict, and a broken scanner
can never fabricate a pass (exit 2).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from services.security import probe_local_listeners as probe
from services.security.probe_local_listeners import (
    ProbeError,
    _classify_address,
    _parse_lsof_line,
    build_report,
    is_pipeline_command,
    main,
    scan_listeners,
)
from services.security.probe_models import ListenerRecord


def record(
    *,
    process: str = "python -m services.resolve_bridge",
    address: str = "127.0.0.1:8432",
    loopback: bool = True,
    pipeline: bool = True,
) -> ListenerRecord:
    return ListenerRecord(
        pid=4242,
        process=process,
        address=address,
        port=8432,
        family="ipv4",
        loopback=loopback,
        pipeline=pipeline,
    )


@pytest.mark.parametrize(
    ("address", "family", "loopback"),
    [
        ("127.0.0.1:8432", "ipv4", True),
        ("[::1]:8432", "ipv6", True),
        ("::1:8432", "ipv6", True),
        ("[::ffff:127.0.0.1]:8432", "ipv6", True),
        ("0.0.0.0:8432", "ipv4", False),
        ("*:8432", "ipv4", False),
        ("[::]:8432", "ipv6", False),
        ("192.168.1.5:8432", "ipv4", False),
    ],
)
def test_10_address_classification(
    address: str, family: str, loopback: bool  # noqa: FBT001 (parametrized bool)
) -> None:
    assert _classify_address(address) == (family, loopback)  # type: ignore[comparison-overlap]


def test_20_lsof_line_parsing() -> None:
    line = "python  4242 user   7u  IPv4  0x...  0t0  TCP 127.0.0.1:8432 (LISTEN)"
    parsed = _parse_lsof_line(line)
    assert parsed == (4242, "127.0.0.1:8432", 8432, True, "ipv4")
    assert _parse_lsof_line("COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME") is None
    assert _parse_lsof_line("") is None
    assert _parse_lsof_line("python  1 u  IPv4  0t0  TCP x:notaport (LISTEN)") is not None


def test_30_pipeline_command_classification() -> None:
    assert is_pipeline_command("python -m services.resolve_bridge --port 1")
    assert is_pipeline_command(str(probe.PIPELINE_ROOT) + "/services/cli/__main__.py")
    assert not is_pipeline_command("/Applications/DaVinci Resolve.app/Contents/MacOS/Resolve")
    assert not is_pipeline_command("Google Chrome helper")


def test_40_exposed_pipeline_listener_fails_the_verdict() -> None:
    listeners = (record(address="0.0.0.0:8432", loopback=False),)
    report = build_report(listeners, require_loopback=True, scanned_at_unix=1)
    assert report.pass_verdict is False
    assert "non-loopback-pipeline-listener" in report.verdict


def test_41_loopback_pipeline_listener_passes() -> None:
    report = build_report((record(),), require_loopback=True, scanned_at_unix=1)
    assert report.pass_verdict is True
    assert report.verdict.startswith("loopback-safe")


def test_42_unrelated_non_loopback_listener_never_judged() -> None:
    chrome = record(
        process="/Applications/Chrome.app/Contents/MacOS/Google Chrome",
        address="0.0.0.0:9222",
        loopback=False,
        pipeline=False,
    )
    report = build_report((chrome, record()), require_loopback=True, scanned_at_unix=1)
    assert report.pass_verdict is True
    assert chrome in report.listeners


def test_43_empty_state_passes_with_no_listeners() -> None:
    report = build_report((), require_loopback=True, scanned_at_unix=1)
    assert report.pass_verdict is True
    assert report.verdict == "no-pipeline-listeners: nothing exposed"


def test_50_main_writes_honest_report_and_exits_by_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "nested" / "listeners.json"
    monkeypatch.setattr(probe, "scan_listeners", lambda timeout=10: (record(),))
    assert main(["--require-loopback", "--out", str(out)]) == 0
    payload = json.loads(out.read_bytes())
    assert payload["pass_verdict"] is True
    assert payload["require_loopback"] is True
    assert payload["schema_version"] == 1

    exposed = record(address="0.0.0.0:8432", loopback=False)
    monkeypatch.setattr(probe, "scan_listeners", lambda timeout=10: (exposed,))
    assert main(["--require-loopback", "--out", str(out)]) == 1


def test_51_broken_scanner_is_an_error_never_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(timeout: int = 10) -> tuple[ListenerRecord, ...]:
        raise ProbeError("lsof: command not found")

    monkeypatch.setattr(probe, "scan_listeners", broken)
    out = tmp_path / "listeners.json"
    assert main(["--require-loopback", "--out", str(out)]) == 2
    assert not out.exists()


def test_52_scanner_subprocess_failure_raises_probe_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("lsof")

    monkeypatch.setattr(probe.subprocess, "run", boom)
    with pytest.raises(ProbeError):
        scan_listeners(timeout=1)

    def nonzero(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=13, stderr="boom")

    monkeypatch.setattr(probe.subprocess, "run", nonzero)
    with pytest.raises(ProbeError):
        scan_listeners(timeout=1)


def test_60_live_scan_runs_bounded_against_real_state() -> None:
    listeners = scan_listeners(timeout=15)
    for entry in listeners:
        assert entry.family in ("ipv4", "ipv6", "unix")
        assert isinstance(entry.pipeline, bool)
        assert isinstance(entry.loopback, bool)
    report = build_report(listeners, require_loopback=True, scanned_at_unix=1)
    exposed = [r for r in listeners if r.pipeline and not r.loopback]
    assert report.pass_verdict == (not exposed)
