# noqa: INP001 (evidence tree is not an importable package by design)
"""Evidence-tree ownership for Task 4 probe tooling: paths, resets, scrubbing.

Every regenerated artifact lives under the committed evidence directory;
renders and the MCP call ledger are wiped per run so a stale file or
appended old calls can never satisfy a new probe pass. Committed evidence
JSONs stay repository-relative (absolute host prefixes are scrubbed).
"""

from __future__ import annotations

import shutil
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent
VIDEO_PIPELINE = EVIDENCE.parents[3]
SCRATCH = EVIDENCE / ".scratch"
RENDER_DIR = EVIDENCE / "render"
LEDGER_DIR = EVIDENCE / "ledger"


def reset_dir(path: Path) -> None:
    """Regenerate ``path`` from empty: delete any prior tree, then create."""
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def scrub_repo_prefixes() -> None:
    """Committed evidence JSONs stay repo-relative: no absolute host paths."""
    prefix = f"{VIDEO_PIPELINE}/"
    for path in sorted(EVIDENCE.glob("*.json")):
        text = path.read_text()
        if prefix in text:
            path.write_text(text.replace(prefix, ""))


__all__ = [
    "EVIDENCE",
    "LEDGER_DIR",
    "RENDER_DIR",
    "SCRATCH",
    "VIDEO_PIPELINE",
    "reset_dir",
    "scrub_repo_prefixes",
]
