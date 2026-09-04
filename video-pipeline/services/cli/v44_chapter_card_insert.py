"""CLI entry: operator-approved chapter-card insertion render + evidence."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from services.cli._v44_chapter_card_media import ChapterCardMediaError
from services.cli._v44_chapter_card_plan import ChapterCardPlanError
from services.cli._v44_chapter_card_qa import ChapterCardQAError
from services.cli._v44_chapter_card_run import (
    EVIDENCE_NAME,
    MASTER_NAME,
    ChapterCardInsertError,
    InsertionRequest,
    common_root,
    hash_protected,
    protected_paths,
    run_insertion,
)
from services.cli.review_common import load_tools
from services.foundation_io import sha256_file
from services.preview.models import PreviewToolchainError

__all__ = [
    "EVIDENCE_NAME",
    "MASTER_NAME",
    "hash_protected",
    "main",
    "protected_paths",
]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m services.cli.v44_chapter_card_insert")
    for flag, help_text in (
        ("--source", "approved theme-full source MP4"),
        ("--proposal", "runtime chapter-title-proposal.json"),
        ("--font", "bold Japanese font file"),
        ("--output-dir", "chapter-card-insertion output directory"),
        ("--episode-root", "cockpit episode root holding review/store and runtime"),
        ("--diag-finishing-root", "diag finishing root holding the approved renders"),
    ):
        parser.add_argument(flag, required=True, type=Path, help=help_text)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        try:
            tools = load_tools()
        except PreviewToolchainError as error:
            raise ChapterCardInsertError("toolchain-unavailable", str(error)) from error
        paths = protected_paths(
            episode_root=args.episode_root, diag_finishing_root=args.diag_finishing_root
        )
        root = common_root(args.episode_root, args.diag_finishing_root)
        before = hash_protected(paths, root=root)
        master = run_insertion(
            tools,
            InsertionRequest(
                source=args.source,
                proposal=args.proposal,
                font=args.font,
                output_dir=args.output_dir,
                episode_root=args.episode_root,
                diag_finishing_root=args.diag_finishing_root,
                protected_before=before,
            ),
        )
    except (
        ChapterCardInsertError,
        ChapterCardPlanError,
        ChapterCardMediaError,
        ChapterCardQAError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 2
    except OSError as error:
        # last resort after every boundary translated its own OSErrors
        print(f"fs-failure: {error}", file=sys.stderr)
        return 2
    try:
        digest = sha256_file(master)
    except OSError as error:
        print(f"fs-stat-failed: cannot hash published master {master}: {error}", file=sys.stderr)
        return 2
    try:
        print(f"{master} sha256={digest}")
    except OSError as error:
        print(f"fs-failure: cannot report the published master: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
