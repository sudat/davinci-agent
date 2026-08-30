"""mcp-doctor contract: every check section carries an explicit typed verdict.

The doctor inspects the pinned davinci-resolve-mcp deployment (clone SHA,
venv separation, entry point, ffmpeg, stdio server liveness, Resolve
reachability) against the pin file. Fixtures are fully fake (tmp_path,
no network, no real server): the stub server answers JSON-RPC over stdio.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from services.cli.mcp_doctor import DoctorPaths, run_doctor
from services.cli.mcp_doctor import main as doctor_main
from services.release.manifest import build_manifest, manifest_bytes
from services.toolchain.mcp_coverage import schema_sha256
from services.toolchain.mcp_pin import McpPinError, load_mcp_pin
from tests.toolchain.mcp_surface_support import (
    static_server_source,
    write_coverage_pair,
)

PINNED_SHA = "132e134d3aa25d3d0df6bdf38f051bd29d128211"
OTHER_SHA = "1111111111111111111111111111111111111111"
DOCTOR_TOOLS: dict[str, tuple[str, ...]] = {
    "resolve_control": ("get_version", "delete_everything"),
    "echo": ("back",),
}

STUB_SERVER_TEMPLATE = """\
import json
import sys

REACHABLE = {reachable!r}


def _respond(request_id, result):
    sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": request_id, "result": result}}) + "\\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        request = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = request.get("method")
    if method == "initialize":
        _respond(request.get("id"), {{
            "protocolVersion": "2024-11-05",
            "capabilities": {{"tools": {{}}}},
            "serverInfo": {{"name": "stub-davinci-resolve-mcp", "version": "0.0.0"}},
        }})
    elif method == "tools/call":
        if REACHABLE:
            _respond(request.get("id"), {{
                "content": [{{"type": "text", "text": "DaVinci Resolve Studio 21.0.4.5"}}],
                "isError": False,
            }})
        else:
            _respond(request.get("id"), {{
                "content": [{{"type": "text", "text": "Resolve not reachable"}}],
                "isError": True,
            }})
"""


def _git(clone: Path, *args: str) -> None:
    env = {
        "PATH": __import__("os").environ["PATH"],
        "HOME": str(clone.parent),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    completed = subprocess.run(
        ["git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _fake_clone(tmp_path: Path, *, reachable: bool = True) -> tuple[Path, str]:
    clone = tmp_path / "vendor-clone"
    (clone / "src").mkdir(parents=True)
    # Dual-purpose entry point: the stdio loop answers the doctor probe; the
    # block appended AFTER the loop is parse-only surface for the gate (the
    # loop only ends when stdin closes, so the block never executes).
    (clone / "src" / "server.py").write_text(
        STUB_SERVER_TEMPLATE.format(reachable=reachable)
        + "\n\n"
        + static_server_source(DOCTOR_TOOLS),
        encoding="utf-8",
    )
    (clone / "src" / "granular").mkdir()
    (clone / "src" / "granular" / "common.py").write_text("mcp = None\n")
    (clone / "src" / "granular" / "misc.py").write_text(
        "@mcp.tool()\ndef probe() -> str:\n    return 'probe'\n"
    )
    (clone / "docs" / "kernels").mkdir(parents=True)
    (clone / "docs" / "kernels" / "README.md").write_text(
        "Current kernel coverage: **1 actions** across **1 compound MCP tools**.\n\n"
        "| Kernel | MCP Tool | Actions |\n|---|---|---|\n"
        "| Edit | `resolve_control` | `get_version` |\n",
        encoding="utf-8",
    )
    (clone / "bin").mkdir()
    (clone / "bin" / "davinci-resolve-advanced-mcp.mjs").write_text("// stub\n", encoding="utf-8")
    (clone / "logs").mkdir()
    (clone / "logs" / "update-check.json").write_text(
        json.dumps({"update_mode": "never"}), encoding="utf-8"
    )
    _git(clone, "init", "-q")
    _git(clone, "commit", "--allow-empty", "-q", "-m", "init")
    head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    write_coverage_pair(
        tmp_path,
        pin_commit=head,
        schemas={
            tool: schema_sha256({"type": "object"}) for tool in DOCTOR_TOOLS
        },
        actions=DOCTOR_TOOLS,
    )
    return clone, head


def _pin_payload(
    *, commit: str, venv_python: str, entry_point: str = "src/server.py"
) -> dict[str, Any]:
    return {
        "schema_version": "mcp-pin-v1",
        "source_url": "https://github.com/samuelgursky/davinci-resolve-mcp",
        "commit": commit,
        "server_mode": "compound",
        "server_entry_point": entry_point,
        "server_protocol": "stdio",
        "venv_python": venv_python,
        "venv_python_version": "3.12.10",
        "repo_python": "3.12.10",
        "advanced_server": {"package": "davinci-resolve-advanced-mcp", "enabled": True},
        "optional_deps": {"ffmpeg": "required"},
        "resolve_preference": "Local",
        "update_check": {
            "enabled": False,
            "mechanism": "install.py --update-policy never + DAVINCI_RESOLVE_MCP_UPDATE_CHECK=0",
        },
        "pinned_date": "2026-08-22T00:40:27Z",
    }


def _happy_fixture(
    tmp_path: Path, *, reachable: bool = True, commit: str | None = None
) -> DoctorPaths:
    clone, head = _fake_clone(tmp_path, reachable=reachable)
    pin_path = tmp_path / "davinci-resolve-mcp.pin.json"
    pin_path.write_text(
        json.dumps(
            _pin_payload(commit=commit or head, venv_python=str(Path(sys.executable).resolve()))
        ),
        encoding="utf-8",
    )
    repo_venv = tmp_path / "repo-venv" / "bin" / "python"
    return DoctorPaths(
        clone_dir=clone,
        pin_path=pin_path,
        repo_venv_python=repo_venv,
        coverage_dir=tmp_path / "coverage",
    )


def _section(report_json: dict[str, Any], check: str) -> dict[str, Any]:
    matches = [s for s in report_json["sections"] if s["check"] == check]
    assert len(matches) == 1, f"expected exactly one {check} section"
    return matches[0]


def _found_ffmpeg() -> str | None:
    return "/opt/homebrew/bin/ffmpeg"


def test_pin_loader_accepts_the_fixture_payload(tmp_path: Path) -> None:
    pin_path = tmp_path / "pin.json"
    pin_path.write_text(json.dumps(_pin_payload(commit=PINNED_SHA, venv_python="/x/y")), "utf-8")
    pin = load_mcp_pin(pin_path)
    assert pin.commit == PINNED_SHA
    assert pin.server_mode == "compound"
    assert pin.update_check.enabled is False


def test_pin_loader_rejects_enabled_update_check(tmp_path: Path) -> None:
    payload = _pin_payload(commit=PINNED_SHA, venv_python="/x/y")
    payload["update_check"]["enabled"] = True
    pin_path = tmp_path / "pin.json"
    pin_path.write_text(json.dumps(payload), "utf-8")
    with pytest.raises(McpPinError):
        load_mcp_pin(pin_path)


def test_happy_path_fixture_all_sections_ok(tmp_path: Path) -> None:
    paths = _happy_fixture(tmp_path)
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg)
    assert report.ok is True
    assert report.exit_code == 0
    codes = {section.check: section.code for section in report.sections}
    assert set(codes.values()) == {"ok"}
    assert "resolve_connection" in codes


def test_sha_mismatch_detected_and_nonzero(tmp_path: Path) -> None:
    paths = _happy_fixture(tmp_path, commit=OTHER_SHA)
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg)
    assert report.ok is False
    assert report.exit_code != 0
    section = _section(json.loads(report.to_json()), "head_sha")
    assert section["status"] == "fail"
    assert section["code"] == "sha-mismatch"


def test_venv_missing_detected(tmp_path: Path) -> None:
    paths = _happy_fixture(tmp_path)
    clone = paths.clone_dir
    pin_path = paths.pin_path
    payload = json.loads(pin_path.read_text("utf-8"))
    payload["venv_python"] = str(clone / "venv" / "bin" / "python")
    pin_path.write_text(json.dumps(payload), "utf-8")
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg)
    assert report.ok is False
    section = _section(json.loads(report.to_json()), "venv_python")
    assert section["code"] == "venv-missing"


def test_venv_equal_to_repo_venv_reports_overlap(tmp_path: Path) -> None:
    paths = _happy_fixture(tmp_path)
    payload = json.loads(paths.pin_path.read_text("utf-8"))
    payload["venv_python"] = str(paths.repo_venv_python)
    paths.pin_path.write_text(json.dumps(payload), "utf-8")
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg)
    section = _section(json.loads(report.to_json()), "venv_python")
    assert section["status"] == "fail"
    assert section["code"] == "venv-overlaps-repo-venv"


def test_venv_sharing_base_binary_but_distinct_roots_is_ok(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo-venv"
    (repo_root / "bin").mkdir(parents=True)
    repo_link = repo_root / "bin" / "python"
    repo_link.symlink_to(Path(sys.executable).resolve())
    paths = _happy_fixture(tmp_path)
    report = run_doctor(
        DoctorPaths(
            clone_dir=paths.clone_dir, pin_path=paths.pin_path, repo_venv_python=repo_link
        ),
        ffmpeg_lookup=_found_ffmpeg,
    )
    section = _section(json.loads(report.to_json()), "venv_python")
    assert section["status"] == "ok"
    assert section["code"] == "ok"


def test_clone_absent_reports_clone_failed(tmp_path: Path) -> None:
    paths = _happy_fixture(tmp_path)
    absent_paths = DoctorPaths(
        clone_dir=tmp_path / "absent",
        pin_path=paths.pin_path,
        repo_venv_python=paths.repo_venv_python,
    )
    report = run_doctor(absent_paths, ffmpeg_lookup=_found_ffmpeg)
    assert report.ok is False
    assert report.exit_code != 0
    section = _section(json.loads(report.to_json()), "clone_exists")
    assert section["code"] == "clone-failed"


def test_entry_point_missing_detected(tmp_path: Path) -> None:
    paths = _happy_fixture(tmp_path)
    payload = json.loads(paths.pin_path.read_text("utf-8"))
    payload["server_entry_point"] = "src/nope.py"
    paths.pin_path.write_text(json.dumps(payload), "utf-8")
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg)
    assert report.ok is False
    section = _section(json.loads(report.to_json()), "server_entry_point")
    assert section["code"] == "entry-point-missing"


def test_invalid_pin_reports_pin_invalid(tmp_path: Path) -> None:
    paths = _happy_fixture(tmp_path)
    payload = json.loads(paths.pin_path.read_text("utf-8"))
    del payload["repo_python"]
    paths.pin_path.write_text(json.dumps(payload), "utf-8")
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg)
    assert report.ok is False
    section = _section(json.loads(report.to_json()), "pin_json")
    assert section["code"] == "pin-invalid"


def test_main_emits_json_report_and_remedy_hint_on_unreachable_resolve(tmp_path, capsys) -> None:
    paths = _happy_fixture(tmp_path, reachable=False)
    exit_code = doctor_main(
        [
            "--clone",
            str(paths.clone_dir),
            "--pin",
            str(paths.pin_path),
            "--repo-venv",
            str(paths.repo_venv_python),
        ]
    )
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert exit_code != 0
    assert parsed["ok"] is False
    section = _section(parsed, "resolve_connection")
    assert section["code"] == "resolve-unreachable"
    assert "External scripting using = Local" in captured.err


# ─── Task 11 — committed tool_surface section ────────────────────────────────


def test_tool_surface_section_ok_on_consistent_artifacts(tmp_path: Path) -> None:
    paths = _happy_fixture(tmp_path)
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg)
    section = _section(json.loads(report.to_json()), "tool_surface")
    assert section["status"] == "ok"
    assert section["code"] == "ok"


def test_tool_surface_fails_typed_on_stale_dispositions_seal(tmp_path: Path) -> None:
    paths = _happy_fixture(tmp_path)
    dispositions = paths.coverage_dir / "dispositions.json"
    payload = json.loads(dispositions.read_text("utf-8"))
    payload["inventory_sha256"] = "0" * 64
    dispositions.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    manifest = paths.coverage_dir / "manifest.json"
    manifest.write_bytes(manifest_bytes(build_manifest(paths.coverage_dir)))
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg)
    section = _section(json.loads(report.to_json()), "tool_surface")
    assert section["status"] == "fail"
    assert section["code"] == "surface-inventory-hash-stale"


def test_tool_surface_fails_typed_on_missing_inventory(tmp_path: Path) -> None:
    paths = _happy_fixture(tmp_path)
    (paths.coverage_dir / "inventory.json").unlink()
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg)
    section = _section(json.loads(report.to_json()), "tool_surface")
    assert section["status"] == "fail"
    assert section["code"] == "surface-inventory-missing"


def test_tool_surface_reports_drift_on_wrong_checkout(tmp_path: Path) -> None:
    paths = _happy_fixture(tmp_path, commit=OTHER_SHA)
    # Artifacts must agree with the (wrong) pin; only the checkout HEAD drifts.
    write_coverage_pair(
        tmp_path,
        pin_commit=OTHER_SHA,
        schemas={tool: schema_sha256({"type": "object"}) for tool in DOCTOR_TOOLS},
        actions=DOCTOR_TOOLS,
    )
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg)
    parsed = json.loads(report.to_json())
    assert _section(parsed, "head_sha")["code"] == "sha-mismatch"
    section = _section(parsed, "tool_surface")
    assert section["status"] == "fail"
    assert section["code"] == "mcp-tool-surface-drift"
    assert "checkout-head" in section["detail"]


def test_tool_surface_reports_action_drift_on_edited_vendor_source(tmp_path: Path) -> None:
    paths = _happy_fixture(tmp_path)
    server = paths.clone_dir / "src" / "server.py"
    server.write_text(
        server.read_text("utf-8").replace(
            'return _unknown(action, ["get_version", "delete_everything"])',
            'return _unknown(action, ["get_version", "delete_everything", "restart_app"])',
        ),
        encoding="utf-8",
    )
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg)
    section = _section(json.loads(report.to_json()), "tool_surface")
    assert section["status"] == "fail"
    assert section["code"] == "mcp-tool-surface-drift"
    assert "action-added=[resolve_control.restart_app]" in section["detail"]
