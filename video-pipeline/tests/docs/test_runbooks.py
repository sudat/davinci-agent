"""Todo 65: runbooks are executable documentation.

Every ``bash``-fenced block in ``docs/runbooks/*.md`` is extracted and run
from ``$PIPELINE_ROOT`` with the locked absolute ``uv`` expanded — the blocks
must be self-contained (their own scratch setup, fixtures from the repo, no
embedded ``cd``) and must terminate within the per-block timeout.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from services.execution.work_init import restore_for_plan

RUNBOOKS = Path("docs/runbooks")
EXPECTED_RUNBOOKS = (
    "phase-0a.md",
    "phase-0b.md",
    "phase-0c.md",
    "control-plane.md",
    "phase-1.md",
    "phase-2.md",
    "phase-3.md",
    "ops-retention.md",
    "ops-metrics.md",
    "ops-final-approval.md",
)
BLOCK_PATTERN = re.compile(r"```bash\n(.*?)```", re.DOTALL)


def fenced_bash_blocks(document: str) -> list[str]:
    return [match.group(1) for match in BLOCK_PATTERN.finditer(document)]


@pytest.fixture(scope="session")
def uv_bin() -> Path:
    workspace = Path.cwd().resolve().parent
    record = restore_for_plan(
        workspace / ".omo/start-work/ledger.jsonl",
        workspace / ".omo/plans/foundation-video-pipeline.md",
        "",
    )
    return Path(record.uv_bin)


def runbooks() -> list[Path]:
    assert RUNBOOKS.is_dir(), "docs/runbooks must exist"
    return sorted(RUNBOOKS.glob("*.md"))


def test_10_every_expected_runbook_exists() -> None:
    names = {path.name for path in runbooks()}
    for expected in EXPECTED_RUNBOOKS:
        assert expected in names, f"missing runbook {expected}"


def test_20_bash_blocks_never_embed_cd() -> None:
    for path in runbooks():
        for block in fenced_bash_blocks(path.read_text(encoding="utf-8")):
            assert not re.search(r"(^|[\s;&|])cd\s", block), f"{path.name} embeds cd"


@pytest.mark.parametrize("path", runbooks(), ids=lambda path: path.name)
def test_30_bash_blocks_execute_without_hidden_setup(
    path: Path, uv_bin: Path
) -> None:
    blocks = fenced_bash_blocks(path.read_text(encoding="utf-8"))
    assert blocks, f"{path.name} must contain at least one executable bash block"
    for block in blocks:
        expanded = re.sub(r"(^|\n)uv ", rf"\1{uv_bin} ", block)
        result = subprocess.run(
            ["bash", "-c", expanded],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        assert result.returncode == 0, f"{path.name} block failed:\n{block}\n{result.stderr}"
