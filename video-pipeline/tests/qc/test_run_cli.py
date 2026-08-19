"""End-to-end CLI: canonical reports, exit codes, malformed inputs, privacy."""

from __future__ import annotations

import json
from pathlib import Path

from services.foundation_io import canonical_model_bytes, sha256_file
from services.qc.models import CapabilityMatrixBinding, QcPolicy
from services.qc.policy_build import build_policy
from services.qc.run import main
from services.qc.tools import load_qc_tools
from tests.qc.support import (
    BLACK_SOURCES,
    RenderSpec,
    base_render,
    clean_policy,
    preset,
    rehash,
    timeline_ir,
)


def _write_policy(path: Path, policy: QcPolicy) -> Path:
    path.write_bytes(canonical_model_bytes(policy))
    return path


def test_cli_clean_render_passes_and_is_byte_deterministic(tmp_path: Path) -> None:
    tools = load_qc_tools()
    render = base_render(tools.ffmpeg, tmp_path, RenderSpec(), name="final.mp4")
    policy = clean_policy(preset(RenderSpec()))
    policy_path = _write_policy(tmp_path / "policy.json", policy)
    out1 = tmp_path / "report-1.json"
    out2 = tmp_path / "report-2.json"
    assert main(["run", "--render", str(render), "--policy", str(policy_path),
                "--out", str(out1)]) == 0
    assert main(["run", "--render", str(render), "--policy", str(policy_path),
                "--out", str(out2)]) == 0
    assert out1.read_bytes() == out2.read_bytes()
    report = json.loads(out1.read_bytes())
    assert report["verdict"] == "passed"
    assert report["schema_version"] == "qc-report-v1"
    assert report["issues"] == []
    assert report["unresolved_human_gates"] == []
    assert report["inputs"][0]["kind"] == "render"
    assert report["inputs"][0]["sha256"] == sha256_file(render)
    assert report["tool_versions"]["qc_engine"] == "todo52-v1"


def test_cli_black_render_exits_blocked(tmp_path: Path) -> None:
    tools = load_qc_tools()
    spec = RenderSpec(video_sources=BLACK_SOURCES)
    render = base_render(tools.ffmpeg, tmp_path, spec, name="black.mp4")
    policy_path = _write_policy(tmp_path / "policy.json", clean_policy(preset(spec)))
    out = tmp_path / "report.json"
    assert main(["run", "--render", str(render), "--policy", str(policy_path),
                "--out", str(out)]) == 1
    report = json.loads(out.read_bytes())
    assert report["verdict"] == "blocked"
    assert any(issue["rule_id"] == "video_black_span" for issue in report["issues"])


def test_cli_declared_privacy_issue_blocks_pending_operator(tmp_path: Path) -> None:
    tools = load_qc_tools()
    render = base_render(tools.ffmpeg, tmp_path, RenderSpec(), name="privacy.mp4")
    policy_path = _write_policy(tmp_path / "policy.json", clean_policy(preset(RenderSpec())))
    privacy = tmp_path / "privacy.json"
    privacy.write_bytes(
        json.dumps(
            {
                "schema_version": "privacy-declarations-v1",
                "declared_issues": [
                    {
                        "issue_id": "face-001",
                        "category": "privacy",
                        "declared_by": "operator",
                        "detail": "bystander face in b-roll",
                        "fixture_only": True,
                        "resolution": None,
                    }
                ],
            }
        ).encode()
    )
    out = tmp_path / "report.json"
    assert main(["run", "--render", str(render), "--policy", str(policy_path),
                 "--privacy", str(privacy), "--out", str(out)]) == 1
    report = json.loads(out.read_bytes())
    assert report["verdict"] == "blocked"
    assert report["unresolved_human_gates"][0]["gate_id"] == "privacy-rights-face-001"


def test_cli_resolved_privacy_issue_passes(tmp_path: Path) -> None:
    tools = load_qc_tools()
    render = base_render(tools.ffmpeg, tmp_path, RenderSpec(), name="resolved.mp4")
    policy_path = _write_policy(tmp_path / "policy.json", clean_policy(preset(RenderSpec())))
    privacy = tmp_path / "privacy.json"
    privacy.write_bytes(
        json.dumps(
            {
                "schema_version": "privacy-declarations-v1",
                "declared_issues": [
                    {
                        "issue_id": "bgm-001",
                        "category": "rights",
                        "declared_by": "operator",
                        "detail": "bgm license cleared",
                        "fixture_only": True,
                        "resolution": {
                            "resolved_by": "operator",
                            "decision": "cleared_for_publication",
                            "record_sha256": "a" * 64,
                            "fixture_only": True,
                        },
                    }
                ],
            }
        ).encode()
    )
    out = tmp_path / "report.json"
    assert main(["run", "--render", str(render), "--policy", str(policy_path),
                 "--privacy", str(privacy), "--out", str(out)]) == 0
    report = json.loads(out.read_bytes())
    assert report["verdict"] == "passed"
    assert report["issues"][0]["rule_id"] == "privacy_rights_resolved"
    assert report["issues"][0]["severity"] == "minor"


def test_cli_malformed_policy_exits_two(tmp_path: Path) -> None:
    tools = load_qc_tools()
    render = base_render(tools.ffmpeg, tmp_path, RenderSpec(), name="x.mp4")
    policy = tmp_path / "policy.json"
    policy.write_bytes(b"{not json")
    out = tmp_path / "report.json"
    assert main(["run", "--render", str(render), "--policy", str(policy),
                 "--out", str(out)]) == 2
    assert not out.is_file()


def test_cli_tampered_policy_hash_exits_two(tmp_path: Path) -> None:
    tools = load_qc_tools()
    render = base_render(tools.ffmpeg, tmp_path, RenderSpec(), name="x.mp4")
    policy = clean_policy(preset(RenderSpec()))
    tampered = policy.model_copy(update={"threshold_version": "evil-v1"})
    policy_path = _write_policy(tmp_path / "policy.json", tampered)
    out = tmp_path / "report.json"
    assert main(["run", "--render", str(render), "--policy", str(policy_path),
                 "--out", str(out)]) == 2


def test_cli_missing_render_exits_two(tmp_path: Path) -> None:
    policy_path = _write_policy(
        tmp_path / "policy.json", clean_policy(preset(RenderSpec()))
    )
    out = tmp_path / "report.json"
    assert main(["run", "--render", str(tmp_path / "ghost.mp4"),
                 "--policy", str(policy_path), "--out", str(out)]) == 2


def test_cli_unsupported_capability_marker_blocks(tmp_path: Path) -> None:
    tools = load_qc_tools()
    render = base_render(tools.ffmpeg, tmp_path, RenderSpec(), name="cap.mp4")
    matrix = tmp_path / "capability-matrix.json"
    matrix.write_bytes(
        json.dumps(
            {
                "capabilities": [
                    {
                        "capability": "time_travel",
                        "api_available": False,
                        "live_verified": False,
                    }
                ]
            }
        ).encode()
    )
    policy = clean_policy(preset(RenderSpec()))
    bound = rehash(
        policy.model_copy(
            update={
                "required_capabilities": ("time_travel",),
                "capability_matrix": CapabilityMatrixBinding(
                    path=str(matrix),
                    sha256=sha256_file(matrix),
                ),
            }
        )
    )
    policy_path = _write_policy(tmp_path / "policy.json", bound)
    out = tmp_path / "report.json"
    assert main(["run", "--render", str(render), "--policy", str(policy_path),
                 "--out", str(out)]) == 1
    report = json.loads(out.read_bytes())
    assert any(
        issue["rule_id"] == "qc_capability_unsupported" for issue in report["issues"]
    )


def test_cli_ir_gap_binding_blocks(tmp_path: Path) -> None:
    tools = load_qc_tools()
    render = base_render(tools.ffmpeg, tmp_path, RenderSpec(), name="gap.mp4")
    ir = timeline_ir(((0, 60), (90, 120)))
    ir_path = tmp_path / "ir.json"
    ir_path.write_bytes(canonical_model_bytes(ir))
    policy = clean_policy(preset(RenderSpec()))
    policy_path = _write_policy(tmp_path / "policy.json", policy)
    out = tmp_path / "report.json"
    assert main(["run", "--render", str(render), "--policy", str(policy_path),
                 "--ir", str(ir_path), "--out", str(out)]) == 1
    report = json.loads(out.read_bytes())
    assert any(issue["rule_id"] == "ir_span_gap" for issue in report["issues"])


def test_cli_build_policy_authors_canonical_passing_policy(tmp_path: Path) -> None:
    tools = load_qc_tools()
    render = base_render(tools.ffmpeg, tmp_path, RenderSpec(), name="final.mp4")
    out = tmp_path / "resolved-qc-policy.json"
    assert main(["build-policy", "--render", str(render), "--out", str(out)]) == 0
    policy = json.loads(out.read_bytes())
    assert policy["schema_version"] == "resolved-qc-policy-v1"
    rebuilt = build_policy(render)
    assert out.read_bytes() == canonical_model_bytes(rebuilt)
    report = tmp_path / "qc-report.json"
    assert main(["run", "--render", str(render), "--policy", str(out),
                 "--out", str(report)]) == 0
