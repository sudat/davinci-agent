from __future__ import annotations

from pathlib import Path
from typing import Final

PIPELINE_ROOT: Final = Path(__file__).resolve().parents[2]
TOOLCHAIN_ROOT: Final = PIPELINE_ROOT / "config" / "toolchains"
OMO_MARKER: Final = ".omo" + "/"
LEGACY_OMO_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "phase-0a-v1.json",
        "phase-0b-v1.json",
        "phase-0c-v1.json",
        "phase-1-technical-v1.json",
        "phase-2-v1.json",
        "phase-3-v1.json",
        "pins/whisper-ja.json",
    }
)


def test_no_omo_references_in_non_legacy_pins() -> None:
    violations: list[str] = []
    for json_path in sorted(TOOLCHAIN_ROOT.rglob("*.json")):
        relative_path = json_path.relative_to(TOOLCHAIN_ROOT).as_posix()
        if relative_path in LEGACY_OMO_ALLOWLIST:
            continue
        if OMO_MARKER in json_path.read_text(encoding="utf-8"):
            violations.append(relative_path)
    assert not violations, (
        f"Non-legacy toolchain pins must not contain {OMO_MARKER} references: "
        + ", ".join(violations)
    )


def test_legacy_allowlist_is_closed_and_exact() -> None:
    assert {
        "phase-0a-v1.json",
        "phase-0b-v1.json",
        "phase-0c-v1.json",
        "phase-1-technical-v1.json",
        "phase-2-v1.json",
        "phase-3-v1.json",
        "pins/whisper-ja.json",
    } == LEGACY_OMO_ALLOWLIST
