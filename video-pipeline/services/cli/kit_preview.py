"""``python -m services.cli.kit_preview`` — render kit recipe A/B snippets.

Subcommands:
  render --episode-root <dir> [--domains subtitle,audio,color]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from services.production_kit.preview import (
    DEFAULT_DOMAINS,
    KitPreviewError,
    plan_previews,
)
from services.production_kit.registry import load_kit


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="kit_preview")
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render", help="render per-domain kit preview snippets")
    render.add_argument("--episode-root", type=Path, required=True)
    render.add_argument(
        "--domains",
        default=",".join(DEFAULT_DOMAINS),
        help=f"comma-separated domains (default: {','.join(DEFAULT_DOMAINS)})",
    )
    return parser.parse_args(argv)


def _cmd_render(args: argparse.Namespace) -> int:
    episode_root: Path = args.episode_root
    if not episode_root.is_dir():
        print(f"episode-root-not-found: {episode_root}", file=sys.stderr)
        return 2
    domains = [name.strip() for name in str(args.domains).split(",") if name.strip()]
    manifest = plan_previews(load_kit(), episode_root, domains)
    for domain in manifest.domains:
        for candidate in domain.candidates:
            print(
                f"{domain.domain}: {candidate.recipe_id} -> "
                f"{episode_root / 'kit-previews' / candidate.file} "
                f"params={candidate.resolved_params}"
            )
    print(f"manifest: {episode_root / 'kit-previews' / 'manifest.json'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return _cmd_render(args)
    except KitPreviewError as error:
        print(f"{error.code}: {error.detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
