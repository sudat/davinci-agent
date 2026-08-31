# noqa: INP001 (evidence tree is not an importable package by design)
"""Synthetic pure-logic selfcheck for :mod:`caption_logic` (no Resolve, no
files). Every rule is exercised with fabricated data: canonicalization
determinism/ordering, invalid text/frame/overlap refusals, settings-candidate
classification, the committed-output allowlist, dual-run stability, and
duration parsing — each failure arm must refuse with a closed label.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Protocol

from caption_logic import (
    ProbeRefusalError,
    assert_sanitized,
    canonical_cues,
    classify_candidate,
    parse_duration_frames,
    stability_verdict,
)


class ExpectFn(Protocol):
    def __call__(self, label: str, *, passed: bool) -> None: ...


def _refused(fn: Callable[[], object], label: str) -> bool:
    try:
        fn()
    except ProbeRefusalError:
        return True
    print(f"[FAIL] selfcheck: {label} was NOT refused", flush=True)
    return False


def _check_canonicalization(expect: ExpectFn) -> None:
    rows_a = [{"text": "synth-alpha", "start": 10, "end": 20},
              {"text": "synth-beta", "start": 30, "end": 45}]
    rows_b = [{"text": "synth-beta", "start": 30, "end": 45},
              {"text": "synth-alpha", "start": 10, "end": 20}]
    cues_a, sha_a = canonical_cues(rows_a)
    _, sha_b = canonical_cues(rows_b)
    expect("canonicalization order-independent", passed=sha_a == sha_b)
    expect("validated cue tuple shape", passed=cues_a[0] == ("synth-alpha", 10, 20))
    _, sha_c = canonical_cues([{"text": "synth-alpha", "start": 10, "end": 21},
                               rows_a[1]])
    expect("canonicalization one-frame delta changes hash", passed=sha_a != sha_c)


def _check_invalid_rows(expect: ExpectFn) -> None:
    for label, bad in (("empty text", {"text": "", "start": 0, "end": 5}),
                       ("whitespace text", {"text": "  ", "start": 0, "end": 5}),
                       ("non-string text", {"text": 7, "start": 0, "end": 5}),
                       ("float frame", {"text": "row-a", "start": 1.5, "end": 5}),
                       ("bool frame", {"text": "row-a", "start": True, "end": 5}),
                       ("string frame", {"text": "row-a", "start": "1", "end": 5}),
                       ("reversed span", {"text": "row-a", "start": 9, "end": 9}),
                       ("negative span", {"text": "row-a", "start": 20, "end": 5}),
                       ("non-object row", "not-a-row")):
        refused = _refused(lambda bad=bad: canonical_cues([bad]), label)
        expect(f"{label} refused", passed=refused)

    def overlap() -> object:
        return canonical_cues([{"text": "row-a", "start": 0, "end": 30},
                               {"text": "row-b", "start": 20, "end": 40}])

    expect("overlapping spans refused", passed=_refused(overlap, "overlapping"))


def _check_classification(expect: ExpectFn) -> None:
    ok_shape = {"success": True, "would_generate": True}
    ignored = {**ok_shape, "settings": {}, "ignored_settings": ["language"]}
    empty = {**ok_shape, "settings": {}, "ignored_settings": []}
    accepted = {**ok_shape, "settings": {"0.0": 0.0}, "ignored_settings": []}
    expect("ignored key classified keys-ignored",
           passed=classify_candidate(ignored) == "keys-ignored")
    expect("empty echo classified keys-ignored",
           passed=classify_candidate(empty) == "keys-ignored")
    expect("echo carried classified accepted",
           passed=classify_candidate(accepted) == "accepted")
    expect("success=false echo never accepted",
           passed=classify_candidate({**accepted, "success": False})
           == "echo-invalid")
    expect("error-envelope echo never accepted",
           passed=classify_candidate(
               {**accepted, "error": {"message": "x", "code": -1}})
           == "echo-invalid")
    expect("missing would_generate echo never accepted",
           passed=classify_candidate(
               {"success": True, "settings": {"0.0": 0.0},
                "ignored_settings": []}) == "echo-invalid")
    expect("failed echo with settings dict never accepted",
           passed=classify_candidate(
               {"success": False, "would_generate": False,
                "settings": {"0.0": 0.0}, "ignored_settings": []})
           == "echo-invalid")


def _check_allowlist(expect: ExpectFn) -> None:
    good = {"schema_version": "resolve-auto-caption-capability-v1", "run": "r1",
            "verdict": "generated-and-read", "input_media_sha256": "a" * 64,
            "pin_commit": "b" * 40, "resolve_version": "21.0.4.5",
            "echo_keys": ["0.0"], "count": 3, "ok": True}
    try:
        assert_sanitized(good)
        expect("sanitized report passes allowlist", passed=True)
    except ProbeRefusalError:
        expect("sanitized report passes allowlist", passed=False)
    for label, bad in (
        ("caption prose refused",
         {**good, "verdict": "synthetic caption prose must be refused"}),
        ("absolute path refused",
         {**good, "note": str(PurePosixPath("/", "Users", "x", "private",
                                            "media.mov"))}),
        ("free-form error refused", {**good, "err": "boom at file /tmp/x"}),
        ("bad key refused", {**good, "BadKey": 1}),
    ):
        expect(f"allowlist {label}",
               passed=_refused(lambda bad=bad: assert_sanitized(bad), label))


def _check_stability(expect: ExpectFn) -> None:
    cues_a, sha_a = canonical_cues([{"text": "row-a", "start": 0, "end": 5}])
    cues_b, sha_b = canonical_cues([{"text": "row-a", "start": 0, "end": 5}])
    expect("identical dual-run readbacks stable",
           passed=stability_verdict(cues_a, sha_a, cues_b, sha_b)["stable"] is True)
    cues_d, sha_d = canonical_cues([{"text": "row-a", "start": 0, "end": 6}])
    expect("unstable dual-run readbacks detected",
           passed=stability_verdict(cues_a, sha_a, cues_d, sha_d)["stable"] is False)
    empty = stability_verdict([], hashlib.sha256(b"[]").hexdigest(),
                              [], hashlib.sha256(b"[]").hexdigest())
    expect("empty runs reported as counts", passed=empty["r1_item_count"] == 0)


def _check_duration(expect: ExpectFn) -> None:
    frames = 8467
    expect("duration parsed with string fps",
           passed=parse_duration_frames("00:04:42:07", "30") == frames)
    expect("duration parsed with native float fps",
           passed=parse_duration_frames("00:04:42:07", 30.0) == frames)
    for label, args in (("non-string duration", (7, "30")),
                        ("bad fps", ("00:04:42:07", "x")),
                        ("bool fps", ("00:04:42:07", True)),
                        ("short duration", ("04:42:07", "30")),
                        ("zero total", ("00:00:00:00", "30"))):
        expect(f"duration {label} refused",
               passed=_refused(lambda args=args: parse_duration_frames(*args), label))


def selfcheck() -> int:
    """Run every pure-rule check; 0 iff all checks pass."""
    failures: list[str] = []

    def expect(label: str, *, passed: bool) -> None:
        print(f"[{'ok' if passed else 'FAIL'}] selfcheck: {label}", flush=True)
        if not passed:
            failures.append(label)

    _check_canonicalization(expect)
    _check_invalid_rows(expect)
    _check_classification(expect)
    _check_allowlist(expect)
    _check_stability(expect)
    _check_duration(expect)
    try:
        ProbeRefusalError("not-a-label")
        closed = False
    except ValueError:
        closed = True
    expect("refusal reason labels closed", passed=closed)
    all_passed = len(failures) == 0
    print(f"selfcheck: {'PASS' if all_passed else f'FAIL ({len(failures)})'}",
          flush=True)
    return 0 if all_passed else 1


__all__ = ["selfcheck"]
