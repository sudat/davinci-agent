"""``python -m services.cli.v44_finishing`` — live Resolve finishing protocol (T13).

From a committed PREVIEW_READY-or-later episode (the PREVIEW_READY→
RESOLVE_BUILT gate-bypass edge, transitions.py) this runs the V44-2
finishing: finishing plans from the operator's recorded kit selections →
MCP execution-plan compile (failed capabilities route to their fallback
matrix rungs automatically) → executor (``--executor fake`` replays the
plan readbacks; ``--executor live`` probes the pinned MCP server first and
BLOCKS on refusal — episode0 precedent) → final preview render →
technical QC (``run_qc``; null-with-reason when no policy is supplied —
never a fabricated verdict) → editorial QC → the seven-domain quality
report + gate → ``FinishingRunReport`` (RUNTIME report, NOT an
authoritative artifact) at ``<episode-root>/finishing/finishing-run.json``.

Subcommands ``record-publishability`` (RECORDS the operator verdict into
the existing PublishabilityReviewV1; refuses on unfinished states) and
``record-time`` (bootstrap AHT phase log) complete the operator protocol;
``gate-summary`` (T16) derives the strict ``v44-gate-summary-v1`` from the
run evidence and refuses to emit ``passed`` without it.
Orchestration lives in ``_v44_finishing_run``; recorders in
``_v44_finishing_record``; the summary writer in ``_v44_gate_summary``.

Exit codes: 0 gate pass / 1 blocked (blocked domain names on stderr,
QualityGateResult reject semantics) / 2 malformed.

Single-writer note: this CLI is a MANUAL run surface — it holds no
runner.lock; do not run it while a cockpit runner is active on the same
episode (T9 issues.md lock-gap record).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.cli._v44_finishing_build import FinishingError, FinishingMalformedError
from services.cli._v44_finishing_record import record_publishability, record_time
from services.cli._v44_finishing_run import cmd_run
from services.cli._v44_gate_summary import cmd_gate_summary
from services.config.backends import BackendsConfigError

if TYPE_CHECKING:
    from services.final_review.publishability import PublishabilityReviewV1

EXIT_PASSED: Final = 0
EXIT_BLOCKED: Final = 1
EXIT_MALFORMED: Final = 2
DEFAULT_MCP_PIN: Final = Path("config/toolchains/davinci-resolve-mcp.pin.json")
DEFAULT_BACKENDS: Final = Path("config/backends.json")
DEFAULT_RUNTIME: Final = Path("config/editorial-runtime.json")

#: Subcommands that own their whole exit code (no recorder printing after).
_DIRECT_COMMANDS: Final[dict[str, Callable[[argparse.Namespace], int]]] = {
    "run": cmd_run,
    "gate-summary": cmd_gate_summary,
}


def _positive_float(value: str) -> float:
    minutes = float(value)
    if minutes <= 0:
        raise argparse.ArgumentTypeError(f"minutes must be > 0, got {value}")
    return minutes


def _parse_dimension_comments(raw: list[str] | None) -> dict[str, str]:
    comments: dict[str, str] = {}
    for item in raw or ():
        key, sep, text = item.partition("=")
        if not sep or not key.strip() or not text.strip():
            raise argparse.ArgumentTypeError(
                f"--dimension-comments expects key=text, got {item!r}"
            )
        comments[key.strip()] = text.strip()
    return comments


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="services.cli.v44_finishing",
        description="V44-2 live finishing + QC + publishability protocol harness",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="run the finishing protocol")
    run_parser.add_argument("--episode-root", type=Path, required=True)
    run_parser.add_argument(
        "--executor", choices=("fake", "live"), default="fake",
        help="fake replays plan readbacks; live requires the pinned MCP server",
    )
    run_parser.add_argument("--pin", type=Path, default=DEFAULT_MCP_PIN)
    run_parser.add_argument("--backends", type=Path, default=DEFAULT_BACKENDS)
    run_parser.add_argument("--editorial-runtime", type=Path, default=DEFAULT_RUNTIME)
    run_parser.add_argument(
        "--qc-policy", type=Path, default=None, help="resolved QC policy JSON"
    )
    run_parser.add_argument(
        "--episode-protocol", type=Path, default=None,
        help="v44 episode protocol JSON (title evidence) when episode.json is "
        "the chain manifest",
    )
    run_parser.add_argument(
        "--render", type=Path, default=None,
        help="QC target override (the live Resolve render); default: final preview",
    )
    run_parser.add_argument(
        "--audio-facts", type=Path, default=None,
        help="episode-scoped AudioFactsV1 JSON (already-good guard for the audio plan)",
    )
    pub_parser = sub.add_parser(
        "record-publishability", help="RECORD the operator verdict (never decides)"
    )
    pub_parser.add_argument("--episode-root", type=Path, required=True)
    pub_parser.add_argument(
        "--verdict",
        choices=("publishable", "publishable_after_fixes", "not_publishable"),
        required=True,
    )
    pub_parser.add_argument(
        "--viewed-render-sha256",
        required=True,
        help="sha256 of the render the operator actually watched; must equal "
        "the current render truth (native-render meta + QC report)",
    )
    pub_parser.add_argument("--comments", default=None)
    pub_parser.add_argument(
        "--dimension-comments", action="append", default=None, metavar="KEY=TEXT",
        help="one of the seven quality domains, repeatable",
    )
    time_parser = sub.add_parser("record-time", help="append a bootstrap AHT phase")
    time_parser.add_argument("--episode-root", type=Path, required=True)
    time_parser.add_argument(
        "--phase",
        choices=(
            "ordinary_review",
            "kit_bootstrap",
            "taste_calibration",
            "troubleshooting",
            "direct_resolve",
        ),
        required=True,
    )
    time_parser.add_argument("--minutes", type=_positive_float, required=True)
    gate_parser = sub.add_parser(
        "gate-summary",
        help="derive + write the v44-gate-summary-v1 (refuses to pass without evidence)",
    )
    gate_parser.add_argument("--episode-root", type=Path, required=True)
    gate_parser.add_argument(
        "--out", type=Path, default=None,
        help="output path; default <episode-root>/finishing/gate-summary.json",
    )
    gate_parser.add_argument(
        "--commit-sha", default=None, help="40-hex commit SHA; default git HEAD"
    )
    gate_parser.add_argument(
        "--subtitle-proof-ref", default=None,
        help="reference to the Japanese subtitle proof (when it ran)",
    )
    return parser


def _dispatch_record(args: argparse.Namespace) -> PublishabilityReviewV1 | None:
    if args.command == "record-publishability":
        return record_publishability(
            args.episode_root,
            args.verdict,
            comments=args.comments,
            dimension_comments=_parse_dimension_comments(args.dimension_comments),
            viewed_render_sha256=args.viewed_render_sha256,
        )
    record_time(args.episode_root, args.phase, args.minutes)
    return None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    review: PublishabilityReviewV1 | None = None
    try:
        if args.command in _DIRECT_COMMANDS:
            return _DIRECT_COMMANDS[args.command](args)
        review = _dispatch_record(args)
    except FinishingMalformedError as error:
        print(f"malformed: {error.code}: {error.detail}", file=sys.stderr)
        return EXIT_MALFORMED
    except FinishingError as error:
        print(f"{error.code}: {error.detail}", file=sys.stderr)
        print("BLOCKED — escalated to operator. Not continuing.", file=sys.stderr)
        return EXIT_BLOCKED
    except (argparse.ArgumentTypeError, BackendsConfigError) as error:
        print(f"malformed: {error}", file=sys.stderr)
        return EXIT_MALFORMED
    except Exception as error:  # noqa: BLE001 (CLI boundary funnel: named failure, exit 1)
        print(f"run_failed: {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_BLOCKED
    if review is not None:
        print(f"publishability recorded: {args.episode_root / 'review' / 'publishability.json'}")
        print(f"  episode={review.episode_id} run={review.run_id} verdict={review.publishable}")
    else:
        print(f"time recorded: {args.episode_root / 'time-log.jsonl'} ({args.phase})")
    return EXIT_PASSED


if __name__ == "__main__":
    raise SystemExit(main())
