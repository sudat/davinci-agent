from __future__ import annotations

from pathlib import Path

from services.execution.work_init import restore_for_plan
from tests.work_init_support import seed_work_init


def test_seeded_work_initialization_stays_under_supplied_root(tmp_path: Path) -> None:
    # Given a temporary root.
    root = tmp_path / "work"

    # When work initialization is seeded and restored.
    ledger, plan = seed_work_init(root)
    record = restore_for_plan(ledger, plan, "")

    # Then every mutable path stays below the root and the plan copy is exact.
    assert record.attempt_dir.is_relative_to(root), (
        f"attempt_dir escaped root: {record.attempt_dir}"
    )
    assert record.plan_input.is_relative_to(root)
    assert record.execution_ledger.is_relative_to(root)
    assert ledger.is_relative_to(root)
    assert plan.is_relative_to(root)
    assert plan.read_bytes() == record.plan_input.read_bytes()


def test_independent_seed_roots_share_no_mutable_paths(tmp_path: Path) -> None:
    # Given two independent roots.
    roots = (tmp_path / "first", tmp_path / "second")

    # When both work initializations are seeded and restored.
    restored = []
    for root in roots:
        ledger, plan = seed_work_init(root)
        record = restore_for_plan(ledger, plan, "")
        restored.append(
            {
                ledger,
                record.attempt_dir,
                record.plan_input,
                record.execution_ledger,
            }
        )

    # Then both restores succeed and none of their mutable paths overlap.
    assert restored[0].isdisjoint(restored[1])


def test_var_alias_root_restores_with_canonical_paths(tmp_path: Path) -> None:
    # Given the /var spelling of a macOS temporary root.
    root = Path(str(tmp_path).removeprefix("/private")) / "alias"
    assert str(root).startswith("/var/")

    # When work initialization is seeded and restored.
    ledger, plan = seed_work_init(root)
    record = restore_for_plan(ledger, plan, "")

    # Then every returned path uses the canonical filesystem spelling.
    canonical_root = root.resolve()
    assert ledger == ledger.resolve()
    assert plan == plan.resolve()
    assert record.attempt_dir.is_relative_to(canonical_root)
    assert record.plan_input.is_relative_to(canonical_root)
    assert record.execution_ledger.is_relative_to(canonical_root)


def test_reused_root_removes_stale_attempt_outputs(tmp_path: Path) -> None:
    # Given stale mutable outputs in a reused attempt area.
    root = tmp_path / "reused"
    attempt_dir = root / ".omo" / "start-work/attempts/seeded"
    stale_ledger = attempt_dir / "execution-ledger.jsonl"
    stale_output = attempt_dir / "nested/stale.json"
    stale_ledger.parent.mkdir(parents=True)
    stale_output.parent.mkdir(parents=True)
    stale_ledger.write_text("stale\n", encoding="utf-8")
    stale_output.write_text("stale\n", encoding="utf-8")

    # When the same root is seeded again.
    ledger, plan = seed_work_init(root)
    record = restore_for_plan(ledger, plan, "")

    # Then only current seed state remains in the attempt area.
    assert not stale_ledger.exists()
    assert not stale_output.exists()
    assert record.plan_input.read_bytes() == plan.read_bytes()
