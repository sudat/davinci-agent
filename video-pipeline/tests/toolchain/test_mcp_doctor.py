"""mcp-doctor contract: every check section carries an explicit typed verdict.

The doctor inspects the vendored davinci-resolve-mcp deployment (checkout
present, venv separation, entry point, ffmpeg, stdio server liveness,
handshake server name with the version REPORTED not enforced, tools/list,
Resolve reachability, advanced binary, node, update-check policy).
Fixtures are fully fake (tmp_path, no network, no real server): the stub
server answers JSON-RPC over stdio.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from services.cli.mcp_doctor import DoctorPaths, run_doctor
from services.cli.mcp_doctor import main as doctor_main
from services.cli.mcp_doctor_node import parse_node_version

FOREIGN_NAME = "rogue-server"
STUB_VERSION = "9.9.9"
STUB_TOOLS = ("resolve_control", "echo")

STUB_SERVER_TEMPLATE = """\
import json
import sys

NAME = {name!r}
VERSION = {version!r}
REACHABLE = {reachable!r}
TOOLS = {tools!r}


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
            "serverInfo": {{"name": NAME, "version": VERSION}},
        }})
    elif method == "tools/list":
        _respond(request.get("id"), {{
            "tools": [{{"name": name, "inputSchema": {{"type": "object"}}}} for name in TOOLS],
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


def _fake_clone(
    tmp_path: Path, *, reachable: bool = True, name: str = "DaVinciResolveMCP"
) -> Path:
    clone = tmp_path / "vendor-clone"
    (clone / "src").mkdir(parents=True)
    (clone / "src" / "server.py").write_text(
        STUB_SERVER_TEMPLATE.format(
            name=name, version=STUB_VERSION, reachable=reachable, tools=list(STUB_TOOLS)
        ),
        encoding="utf-8",
    )
    (clone / "bin").mkdir()
    (clone / "bin" / "davinci-resolve-advanced-mcp.mjs").write_text("// stub\n", encoding="utf-8")
    (clone / "logs").mkdir()
    (clone / "logs" / "update-check.json").write_text(
        json.dumps({"update_mode": "never"}), encoding="utf-8"
    )
    return clone


def _happy_paths(
    tmp_path: Path, *, reachable: bool = True, name: str = "DaVinciResolveMCP"
) -> DoctorPaths:
    clone = _fake_clone(tmp_path, reachable=reachable, name=name)
    repo_venv = tmp_path / "repo-venv" / "bin" / "python"
    return DoctorPaths(
        clone_dir=clone,
        repo_venv_python=repo_venv,
        venv_python=Path(sys.executable),
    )


def _section(report_json: dict[str, Any], check: str) -> dict[str, Any]:
    matches = [s for s in report_json["sections"] if s["check"] == check]
    assert len(matches) == 1, f"expected exactly one {check} section"
    return matches[0]


def _found_ffmpeg() -> str | None:
    return "/opt/homebrew/bin/ffmpeg"


def _found_node() -> str | None:
    return "v24.19.0\n"


def test_happy_path_fixture_all_sections_ok(tmp_path: Path) -> None:
    paths = _happy_paths(tmp_path)
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg, node_lookup=_found_node)
    assert report.ok is True
    assert report.exit_code == 0
    codes = {section.check: section.code for section in report.sections}
    assert set(codes.values()) == {"ok"}
    assert set(codes) == {
        "clone_exists",
        "venv_python",
        "server_entry_point",
        "ffmpeg",
        "server_alive",
        "server_identity",
        "tool_list",
        "resolve_connection",
        "advanced_binary",
        "node_version",
        "update_check",
    }


def test_reported_version_is_surfaced_not_enforced(tmp_path: Path) -> None:
    paths = _happy_paths(tmp_path)
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg, node_lookup=_found_node)
    section = _section(json.loads(report.to_json()), "server_identity")
    assert section["status"] == "ok"
    assert STUB_VERSION in section["detail"]
    assert "reported, not enforced" in section["detail"]


def test_tool_list_reports_count(tmp_path: Path) -> None:
    paths = _happy_paths(tmp_path)
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg, node_lookup=_found_node)
    section = _section(json.loads(report.to_json()), "tool_list")
    assert section["status"] == "ok"
    assert section["detail"] == f"{len(STUB_TOOLS)} tools"


def test_foreign_server_name_refuses_identity(tmp_path: Path) -> None:
    paths = _happy_paths(tmp_path, name=FOREIGN_NAME)
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg, node_lookup=_found_node)
    assert report.ok is False
    assert report.exit_code != 0
    section = _section(json.loads(report.to_json()), "server_identity")
    assert section["status"] == "fail"
    assert section["code"] == "server-identity-mismatch"
    assert FOREIGN_NAME in section["detail"]


def test_venv_missing_detected(tmp_path: Path) -> None:
    paths = _happy_paths(tmp_path)
    missing = DoctorPaths(
        clone_dir=paths.clone_dir,
        repo_venv_python=paths.repo_venv_python,
        venv_python=paths.clone_dir / "venv" / "bin" / "python",
    )
    report = run_doctor(missing, ffmpeg_lookup=_found_ffmpeg, node_lookup=_found_node)
    assert report.ok is False
    section = _section(json.loads(report.to_json()), "venv_python")
    assert section["code"] == "venv-missing"


def test_venv_equal_to_repo_venv_reports_overlap(tmp_path: Path) -> None:
    paths = _happy_paths(tmp_path)
    overlap = DoctorPaths(
        clone_dir=paths.clone_dir,
        repo_venv_python=paths.repo_venv_python,
        venv_python=paths.repo_venv_python,
    )
    report = run_doctor(overlap, ffmpeg_lookup=_found_ffmpeg, node_lookup=_found_node)
    section = _section(json.loads(report.to_json()), "venv_python")
    assert section["status"] == "fail"
    assert section["code"] == "venv-overlaps-repo-venv"


def test_venv_sharing_base_binary_but_distinct_roots_is_ok(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo-venv"
    (repo_root / "bin").mkdir(parents=True)
    repo_link = repo_root / "bin" / "python"
    repo_link.symlink_to(Path(sys.executable).resolve())
    clone = _fake_clone(tmp_path)
    paths = DoctorPaths(
        clone_dir=clone,
        repo_venv_python=repo_link,
        venv_python=Path(sys.executable),
    )
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg, node_lookup=_found_node)
    section = _section(json.loads(report.to_json()), "venv_python")
    assert section["status"] == "ok"
    assert section["code"] == "ok"


def test_clone_absent_reports_clone_failed(tmp_path: Path) -> None:
    paths = _happy_paths(tmp_path)
    absent_paths = DoctorPaths(
        clone_dir=tmp_path / "absent",
        repo_venv_python=paths.repo_venv_python,
        venv_python=paths.venv_python,
    )
    report = run_doctor(absent_paths, ffmpeg_lookup=_found_ffmpeg, node_lookup=_found_node)
    assert report.ok is False
    assert report.exit_code != 0
    section = _section(json.loads(report.to_json()), "clone_exists")
    assert section["code"] == "clone-failed"


def test_entry_point_missing_detected(tmp_path: Path) -> None:
    paths = _happy_paths(tmp_path)
    (paths.clone_dir / "src" / "server.py").unlink()
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg, node_lookup=_found_node)
    assert report.ok is False
    section = _section(json.loads(report.to_json()), "server_entry_point")
    assert section["code"] == "entry-point-missing"


def test_update_check_enabled_fails_typed(tmp_path: Path) -> None:
    paths = _happy_paths(tmp_path)
    (paths.clone_dir / "logs" / "update-check.json").write_text(
        json.dumps({"update_mode": "daily"}), encoding="utf-8"
    )
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg, node_lookup=_found_node)
    assert report.ok is False
    section = _section(json.loads(report.to_json()), "update_check")
    assert section["code"] == "update-check-enabled"


def test_advanced_binary_missing_fails_typed(tmp_path: Path) -> None:
    paths = _happy_paths(tmp_path)
    (paths.clone_dir / "bin" / "davinci-resolve-advanced-mcp.mjs").unlink()
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg, node_lookup=_found_node)
    assert report.ok is False
    section = _section(json.loads(report.to_json()), "advanced_binary")
    assert section["code"] == "advanced-binary-missing"


def test_main_emits_json_report_and_remedy_hint_on_unreachable_resolve(
    tmp_path, capsys
) -> None:
    paths = _happy_paths(tmp_path, reachable=False)
    exit_code = doctor_main(
        [
            "--clone",
            str(paths.clone_dir),
            "--repo-venv",
            str(paths.repo_venv_python),
            "--venv",
            str(paths.venv_python),
        ]
    )
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert exit_code != 0
    assert parsed["ok"] is False
    section = _section(parsed, "resolve_connection")
    assert section["code"] == "resolve-unreachable"
    assert "External scripting using = Local" in captured.err


# ─── Node runtime check (advanced server needs Node >= 20.9) ─────────────────


def test_parse_node_version_accepts_shapes() -> None:
    assert parse_node_version("v24.19.0\n") == (24, 19, 0)
    assert parse_node_version("20.9.0") == (20, 9, 0)
    assert parse_node_version("v20.9.0-nightly20260901") == (20, 9, 0)


def test_parse_node_version_rejects_garbage() -> None:
    assert parse_node_version("") is None
    assert parse_node_version("not-a-version") is None
    assert parse_node_version("v20.9") is None
    assert parse_node_version("v20.x.0") is None


def test_node_version_section_ok_on_current_machine_shape(tmp_path: Path) -> None:
    paths = _happy_paths(tmp_path)
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg, node_lookup=_found_node)
    parsed = json.loads(report.to_json())
    assert _section(parsed, "node_version") == {
        "check": "node_version",
        "status": "ok",
        "code": "ok",
        "detail": "node v24.19.0 satisfies >= 20.9",
    }
    assert report.ok is True


def test_node_version_boundary_exactly_20_9_is_ok(tmp_path: Path) -> None:
    paths = _happy_paths(tmp_path)
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg, node_lookup=lambda: "v20.9.0\n")
    assert report.ok is True
    assert _section(json.loads(report.to_json()), "node_version")["status"] == "ok"


def test_node_version_one_patch_below_minimum_fails(tmp_path: Path) -> None:
    paths = _happy_paths(tmp_path)
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg, node_lookup=lambda: "v20.8.1\n")
    assert report.ok is False
    assert report.exit_code != 0
    section = _section(json.loads(report.to_json()), "node_version")
    assert section["code"] == "node-version-too-old"
    assert "20.8.1" in section["detail"]
    assert "20.9" in section["detail"]


def test_node_version_major_below_minimum_fails(tmp_path: Path) -> None:
    paths = _happy_paths(tmp_path)
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg, node_lookup=lambda: "v18.17.0\n")
    section = _section(json.loads(report.to_json()), "node_version")
    assert section["status"] == "fail"
    assert section["code"] == "node-version-too-old"


def test_node_missing_fails_with_requirement_named(tmp_path: Path) -> None:
    paths = _happy_paths(tmp_path)
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg, node_lookup=lambda: None)
    section = _section(json.loads(report.to_json()), "node_version")
    assert section["status"] == "fail"
    assert section["code"] == "node-missing"
    assert ">= 20.9" in section["detail"]


def test_node_version_unreadable_output_fails_typed(tmp_path: Path) -> None:
    paths = _happy_paths(tmp_path)
    report = run_doctor(paths, ffmpeg_lookup=_found_ffmpeg, node_lookup=lambda: "unexpected")
    section = _section(json.loads(report.to_json()), "node_version")
    assert section["status"] == "fail"
    assert section["code"] == "node-version-unreadable"
