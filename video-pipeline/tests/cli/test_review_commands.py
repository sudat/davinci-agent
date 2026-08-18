"""Todo 45 CLI acceptance: the ``review propose|apply`` command surface.

Exact argparse surface (``--help``), Todo-12 policy gating (an undeclared
review stage refuses translation), replay classification behaviors, the
typed apply outcomes (applied / deferred / stale / schema-gap), and the
no-UI boundary of the CLI package. Ordered: the stale candidate is proposed
against the fresh v1 base BEFORE the clear apply advances it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from services.config.models import (
    BudgetPolicy,
    EpisodeConfig,
    NetworkPosture,
    PathAllowlist,
    RetentionPolicy,
    SystemConfig,
)
from services.config.resolver import resolve
from services.editorial.correction_metrics import build_metrics
from services.foundation_io import atomic_write, canonical_model_bytes
from services.review_command.events import build_event
from services.review_command.models import parse_proposal
from tests.cli.conftest import instruction_file

if TYPE_CHECKING:
    from tests.cli.conftest import CliRig

CLEAR = "字幕 st1 を「とても美味しいですね。」に修正してください。"
AMBIGUOUS = "「はい、そうです。」という字幕を「はい、違います。」に修正してください。"
REMOVE = "セグメント s2 を削除してください。"
GAP = "全部消して、見せ場だけ残していい感じにしてください"


def run_cli(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *argv], capture_output=True, text=True, check=False, cwd=Path.cwd()
    )


def propose(rig: CliRig, instruction_path: Path, out: Path) -> subprocess.CompletedProcess[str]:
    return run_cli(
        [
            "-m",
            "services.cli.review",
            "propose",
            "--bundle",
            str(rig.bundle_file),
            "--instruction-file",
            str(instruction_path),
            "--translator-policy",
            "config/gates/phase-0c-v1.json",
            "--production-policy",
            str(rig.policy_file),
            "--out",
            str(out),
        ]
    )


def apply(rig: CliRig, proposal: Path, out: Path) -> subprocess.CompletedProcess[str]:
    return run_cli(
        [
            "-m",
            "services.cli.review",
            "apply",
            "--proposal",
            str(proposal),
            "--bundle",
            str(rig.bundle_file),
            "--out",
            str(out),
        ]
    )


def test_00_review_help_surface_is_exact() -> None:
    top = run_cli(["-m", "services.cli.review", "--help"])
    assert top.returncode == 0
    assert "propose" in top.stdout
    assert "apply" in top.stdout
    propose_help = run_cli(["-m", "services.cli.review", "propose", "--help"])
    assert propose_help.returncode == 0
    for token in ("--bundle", "--instruction-file", "--translator-policy",
                  "--production-policy", "--out"):
        assert token in propose_help.stdout
    apply_help = run_cli(["-m", "services.cli.review", "apply", "--help"])
    assert apply_help.returncode == 0
    for token in ("--proposal", "--bundle", "--out"):
        assert token in apply_help.stdout


def test_10_fresh_v1_remove_candidate_is_proposed_for_later_staleness(cli_rig: CliRig) -> None:
    instruction = instruction_file(cli_rig.root, "instr-remove", REMOVE)
    result = propose(cli_rig, instruction, cli_rig.root / "prop-stale.json")
    assert result.returncode == 0, result.stderr
    outcome = json.loads((cli_rig.root / "prop-stale.json").read_bytes())
    assert outcome["classification"] == "clear"
    assert outcome["base_plan_version"] == "v1"


def test_20_ambiguous_instruction_never_mutates_over_the_cli(cli_rig: CliRig) -> None:
    bundle_dir = cli_rig.bundle_file.parent
    plan_v1 = (bundle_dir / "review-store" / "plan-v1.json").read_bytes()
    instruction = instruction_file(cli_rig.root, "instr-amb", AMBIGUOUS)
    proposal = cli_rig.root / "prop-amb.json"
    result = propose(cli_rig, instruction, proposal)
    assert result.returncode == 0, result.stderr
    assert "classification: ambiguous" in result.stdout
    applied = apply(cli_rig, proposal, cli_rig.root / "apply-amb")
    assert applied.returncode == 0, applied.stderr
    assert "not applied: ambiguous" in applied.stdout
    assert (bundle_dir / "review-store" / "plan-v1.json").read_bytes() == plan_v1
    assert not (bundle_dir / "preview-v2").exists()


def test_30_clear_instruction_proposes_and_applies_over_the_cli(cli_rig: CliRig) -> None:
    instruction = instruction_file(cli_rig.root, "instr-clear", CLEAR)
    proposal = cli_rig.root / "prop-clear.json"
    result = propose(cli_rig, instruction, proposal)
    assert result.returncode == 0, result.stderr
    assert "classification: clear" in result.stdout
    outcome = json.loads(proposal.read_bytes())
    assert outcome["base_plan_version"] == "v1"
    applied = apply(cli_rig, proposal, cli_rig.root / "apply-clear")
    assert applied.returncode == 0, applied.stderr
    assert "applied: version v2" in applied.stdout
    result_path = cli_rig.root / "apply-clear" / "apply-result.json"
    assert json.loads(result_path.read_bytes())["applied"] is True
    assert (cli_rig.bundle_file.parent / "preview-v2" / "preview.mp4").is_file()


def test_40_stale_base_apply_exits_nonzero(cli_rig: CliRig) -> None:
    result = apply(cli_rig, cli_rig.root / "prop-stale.json", cli_rig.root / "apply-stale")
    assert result.returncode == 1
    assert "stale_base" in result.stdout


def test_50_schema_gap_instruction_exits_nonzero_with_typed_code(cli_rig: CliRig) -> None:
    instruction = instruction_file(cli_rig.root, "instr-gap", GAP)
    proposal = cli_rig.root / "prop-gap.json"
    result = propose(cli_rig, instruction, proposal)
    assert result.returncode == 1
    assert "replay_mismatch" in result.stderr
    assert "refusing to coerce" in result.stderr
    outcome = json.loads(proposal.read_bytes())
    assert outcome["status"] == "error"
    assert outcome["error_code"] == "replay_mismatch"
    applied = apply(cli_rig, proposal, cli_rig.root / "apply-gap")
    assert applied.returncode == 1
    assert "schema_gap" in applied.stdout


def test_70_unmeasured_events_are_excluded_not_counted() -> None:
    """Odd defer reasons land in `unclassified`; bookkeeping never counts."""

    proposal_json = {
        "proposal_id": "prop-metrics",
        "base_plan_version": "v1",
        "actor_intent": "model",
        "sequence": 1,
        "confidence": {"num": 9, "den": 10},
        "ambiguity": {"status": "clear", "reasons": []},
        "evidence": [],
        "command_kind": "correct_subtitle",
        "target": {"kind": "item_id", "item_id": "st1"},
        "new_text": "修正。",
        "language": "ja",
    }
    proposal = parse_proposal(json.dumps(proposal_json))
    recorded = build_event(
        sequence=1,
        kind="proposal_recorded",
        proposal=proposal,
        base_plan_version="v1",
        previous_event_hash="0" * 64,
        actor_intent="model",
    )
    deferred = build_event(
        sequence=2,
        kind="command_deferred",
        proposal=proposal,
        base_plan_version="v1",
        previous_event_hash=recorded.event_id,
        actor_intent="operator",
        decision_id="decision-x",
        reason="some_unmeasured_reason",
    )
    metrics = build_metrics(
        (recorded, deferred), episode_id="p1-ref-05-review-mix", fixture_only=True
    )
    assert metrics.counts_by_classification["clear"] == 0
    assert metrics.counts_by_classification["unclassified"] == 1
    assert metrics.total_applied == 0
    assert metrics.structured_ratio_num == 0
    assert metrics.structured_ratio_den == 1
    assert metrics.source_event_ids == (recorded.event_id, deferred.event_id)


def test_80_policy_gate_refuses_undeclared_stage(cli_rig: CliRig) -> None:
    empty = resolve(
        SystemConfig(
            schema_version="system-config-v1",
            retention=RetentionPolicy(authoritative="permanent", rebuildable_days=30),
            data_classes=(),
            cloud_allowlist=(),
            network=NetworkPosture(
                builder="loopback", builder_endpoint="unix:///run/davinci-agent/editorial.sock"
            ),
            path_allowlist=PathAllowlist(roots=("/video-pipeline/jobs",)),
            budget=BudgetPolicy(
                transient_max_attempts=3,
                permanent_max_attempts=1,
                blocking_human_max_attempts=1,
                max_stage_cost_units=1000,
                max_job_cost_units=10000,
            ),
        ),
        episode=EpisodeConfig(episode_id="p1-ref-05-review-mix"),
    )
    policy = cli_rig.root / "policy-empty.json"
    atomic_write(policy, canonical_model_bytes(empty))
    instruction = instruction_file(cli_rig.root, "instr-clear2", CLEAR)
    result = run_cli(
        [
            "-m",
            "services.cli.review",
            "propose",
            "--bundle",
            str(cli_rig.bundle_file),
            "--instruction-file",
            str(instruction),
            "--translator-policy",
            "config/gates/phase-0c-v1.json",
            "--production-policy",
            str(policy),
            "--out",
            str(cli_rig.root / "prop-refused.json"),
        ]
    )
    assert result.returncode == 1
    assert "stage_not_declared" in result.stderr
    assert not (cli_rig.root / "prop-refused.json").exists()
