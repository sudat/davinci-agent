"""Session fixtures: pinned QC tools from the frozen phase-2 toolchain lock."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from services.qc.tools import QcTools, load_qc_tools


@dataclass(frozen=True, slots=True)
class PinnedQcStack:
    ffmpeg: Path
    ffprobe: Path
    tools: QcTools


@pytest.fixture(scope="session")
def qc_tools() -> QcTools:
    return load_qc_tools()


@pytest.fixture(scope="session")
def pinned_qc(qc_tools: QcTools) -> PinnedQcStack:
    return PinnedQcStack(
        ffmpeg=qc_tools.ffmpeg, ffprobe=qc_tools.ffprobe, tools=qc_tools
    )
