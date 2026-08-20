"""Shared adversarial-test support: state snapshots and attack payloads.

Controller-owned rules only: nothing here trusts a product-side guard's
verdict. ``snapshot_tree`` captures files (hash), symlinks (target), and
directory shape so every attack can prove ZERO side effects by diffing
pre/post snapshots of the area under attack.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

# A marker secret/PII pair injected into untrusted inputs; every log /
# evidence / stdout surface must prove it never appears.
MARKER_NEEDLE = "sk-live-todo66-marker-7f3a"
MARKER_PII = "operator-todo66@example.invalid"

# The canonical prompt-injection payload (EN + JA variants read from
# fixtures so the exact bytes under test are auditable artifacts).
INSTRUCTION_PAYLOADS = {
    "en": FIXTURES / "injection-en.txt",
    "ja": FIXTURES / "injection-ja.txt",
    "ocr": FIXTURES / "injection-ocr.txt",
}


def payload(name: str) -> str:
    """Read one injection payload fixture verbatim."""

    return INSTRUCTION_PAYLOADS[name].read_text(encoding="utf-8").strip()


def snapshot_tree(root: Path) -> dict[str, str]:
    """Map every entry under ``root``: files→sha256, symlinks→target, dirs→'/'."""

    captured: dict[str, str] = {}
    real_root = os.path.realpath(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        real_dir = os.path.realpath(dirpath)
        if not (real_dir == real_root or real_dir.startswith(real_root + os.sep)):
            # a symlinked directory would appear here via followlinks=False only
            # as a filename entry; directories that resolve outside are refused
            # by the guards under test and never reached in a snapshot walk
            continue
        for name in sorted(dirnames):
            candidate = Path(dirpath) / name
            if candidate.is_symlink():
                captured[os.path.relpath(candidate, root).replace(os.sep, "/")] = (
                    f"symlink:{candidate.readlink()}"
                )
            else:
                captured[os.path.relpath(candidate, root).replace(os.sep, "/")] = "dir"
        for name in sorted(filenames):
            candidate = Path(dirpath) / name
            relative = os.path.relpath(candidate, root).replace(os.sep, "/")
            if candidate.is_symlink():
                captured[relative] = f"symlink:{candidate.readlink()}"
            else:
                captured[relative] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    return captured


def assert_zero_side_effects(root: Path, before: dict[str, str]) -> None:
    """Fail with a diff unless ``root`` is byte-identical to ``before``."""

    after = snapshot_tree(root)
    assert after == before, (
        "side effects detected under "
        f"{root}: added={sorted(set(after) - set(before))} "
        f"removed={sorted(set(before) - set(after))} "
        f"changed={sorted(k for k in set(before) & set(after) if before[k] != after[k])}"
    )


def file_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")
