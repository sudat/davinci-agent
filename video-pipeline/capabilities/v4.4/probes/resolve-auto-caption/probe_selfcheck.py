# noqa: INP001 (evidence tree is not an importable package by design)
"""Scripted-flow selfcheck for the auto-caption probe (no Resolve, no files).

Drives ``run_flow`` through every arm — happy path, timeout at generate,
mid-flow exception, media mismatch, pre-existing project, vendor
``success=false``, empty readback, invalid rows, all candidates ignored —
and proves the DISPOSAL contract: cleanup happens first, the session
``close`` runs last on every path, including cleanup failures and skipped
deletes. Every committed-shape report must pass the allowlist.
"""

from __future__ import annotations

import caption_logic as logic
from caption_selfcheck import selfcheck as pure_selfcheck
from probe_fixtures import FAKE_ITEM_COUNT, FakeCall, FakeSession, fake_script, with_generation
from probe_flow import run_flow
from probe_live import finalize_with_disposal
from probe_seam import (
    MediaInput,
    NullSink,
    classify_exception,
    dispose,
    project_name_for,
)


def _check_dispose_order(checks: list[tuple[bool, str]]) -> None:
    """Cleanup before close; close ALWAYS runs; skips never delete."""
    session = FakeSession(FakeCall(fake_script()))
    receipt = dispose(session, project_name_for("r1"))
    calls = session.fake.calls
    checks.append((receipt["delete_success"] is True
                   and session.closed is True
                   and calls[-1] == ("<session>", "close")
                   and ("project_manager", "delete") in calls,
                   "dispose cleans up then closes, in that order"))
    skip_session = FakeSession(FakeCall(fake_script()))
    skip_receipt = dispose(skip_session, project_name_for("r1"),
                           skip_delete_reason="project-exists")
    checks.append((skip_session.closed is True
                   and not skip_session.fake.deleted
                   and skip_receipt["skipped_reason"] == "project-exists",
                   "skipped delete still closes and never deletes"))
    boom = FakeSession(FakeCall(
        fake_script(),
        raise_if=lambda tool, action, _params: (
            (tool, action) == ("project_manager", "delete")),
        exc=RuntimeError("delete-boom")))
    raised = False
    try:
        dispose(boom, project_name_for("r1"))
    except RuntimeError:
        raised = True
    checks.append((raised and boom.closed
                   and boom.fake.calls[-1] == ("<session>", "close"),
                   "cleanup exception still closes the session last"))


def _check_invalid_readback(media: MediaInput,
                            checks: list[tuple[bool, str]]) -> None:
    """Invalid subtitle rows (empty text, reversed span) -> readback-invalid."""
    base = fake_script()
    responder = base[("timeline", "get_items_in_track")]
    if not callable(responder):
        raise TypeError("fixture responder missing")
    rows_obj = responder({"track_type": "subtitle"})
    if not isinstance(rows_obj, dict):
        raise TypeError("fixture responder returned no dict")
    rows_raw = rows_obj.get("items")
    if not isinstance(rows_raw, list):
        raise TypeError("fixture responder returned no item list")
    rows = [dict(row) for row in rows_raw]
    for label, mutate in (
        ("empty text", lambda it: {**it, "name": ""}),
        ("reversed span", lambda it: {**it, "start": it["end"], "end": it["start"]}),
    ):
        script = fake_script()
        bad_rows = [mutate(row) for row in rows]

        def bad_items(params: dict[str, object],
                      _fallback: object = responder,
                      _rows: list[dict[str, object]] = bad_rows,
                      ) -> dict[str, object]:
            if params.get("track_type") == "subtitle":
                return {"items": _rows}
            proxied = _fallback(params) if callable(_fallback) else {}
            return proxied if isinstance(proxied, dict) else {}

        script[("timeline", "get_items_in_track")] = bad_items
        report = run_flow(FakeCall(script), NullSink(), media, "r1")
        checks.append((report.get("verdict") == "readback-invalid",
                       f"invalid readback ({label}) refused"))


def _check_cleanup_failure(checks: list[tuple[bool, str]]) -> None:
    """A cleanup exception must produce a counts-only failure report."""
    report: dict[str, object] = {
        "schema_version": "resolve-auto-caption-capability-v1", "run": "r1",
        "project_name": "probe-resolve-autocap-r1", "verdict": "generated-and-read",
        "input_media_sha256": "c" * 64}
    boom = FakeSession(FakeCall(
        fake_script(),
        raise_if=lambda tool, action, _params: (
            (tool, action) == ("project_manager", "delete")),
        exc=RuntimeError("delete-boom")))
    sink = NullSink()
    finalize_with_disposal(report, boom, [], sink, project_name_for("r1"))
    receipt = report.get("cleanup")
    sanitized = True
    try:
        logic.assert_sanitized(report)
    except logic.ProbeRefusalError:
        sanitized = False
    checks.append((report.get("verdict") != "generated-and-read"
                   and isinstance(receipt, dict)
                   and receipt.get("cleanup_failure") == "error"
                   and receipt.get("delete_success") is False
                   and boom.closed
                   and "steps.json" in sink.names
                   and sanitized,
                   "cleanup failure keeps a counts-only failure report"))
    ok_report: dict[str, object] = {"verdict": "generated-and-read"}
    ok_session = FakeSession(FakeCall(fake_script()))
    finalize_with_disposal(ok_report, ok_session, [], NullSink(),
                           project_name_for("r1"))
    checks.append((ok_report.get("verdict") == "generated-and-read"
                   and ok_report.get("session_closed") is True,
                   "successful disposal keeps the success verdict"))


def flow_selfcheck() -> int:
    """Pure gate + scripted-flow verification; 0 iff all checks pass."""
    failures = pure_selfcheck()
    checks: list[tuple[bool, str]] = []
    sha = "c" * 64
    media = MediaInput(path="fixture-edit-source-r1.mov",
                       expected_sha256=sha, actual_sha256=sha)

    report = run_flow(FakeCall(fake_script()), NullSink(), media, "r1")
    readback = report.get("readback")
    checks.append((report.get("verdict") == "generated-and-read"
                   and isinstance(readback, dict)
                   and readback.get("item_count") == FAKE_ITEM_COUNT,
                   "flow happy path generates and reads"))
    _check_dispose_order(checks)
    fake_timeout = type(logic.TIMEOUT_TYPE_NAME, (Exception,), {})
    fake_gen = FakeCall(
        fake_script(),
        raise_if=lambda tool, action, params: (
            (tool, action) == ("timeline", "subtitle_generation_probe")
            and params.get("allow_generate") is True),
        exc=fake_timeout())
    checks.append((run_flow(fake_gen, NullSink(), media, "r1")
                   .get("verdict") == "timeout",
                   "timeout at generate classified"))
    fake_boom = FakeCall(
        fake_script(),
        raise_if=lambda tool, action, _params: (
            (tool, action) == ("media_pool", "safe_import_media")),
        exc=RuntimeError("boom"))
    try:
        run_flow(fake_boom, NullSink(), media, "r1")
        classified = ""
    except RuntimeError:
        classified = classify_exception(RuntimeError())
    checks.append((classified == "error",
                   "mid-flow exception propagates, classified error"))

    fake_mismatch = FakeCall(fake_script())
    mismatch_media = MediaInput(path=media.path, expected_sha256=sha,
                                actual_sha256="d" * 64)
    checks.append((run_flow(fake_mismatch, NullSink(), mismatch_media, "r1")
                   .get("verdict") == "media-mismatch"
                   and not fake_mismatch.calls,
                   "media mismatch refused with zero MCP calls"))
    fake_exists = FakeCall(fake_script(), existing_project=True)
    checks.append((run_flow(fake_exists, NullSink(), media, "r1")
                   .get("verdict") == "project-exists"
                   and not fake_exists.deleted,
                   "pre-existing project refused without deletion"))

    reports = {"generation-failed": run_flow(FakeCall(with_generation(
        fake_script(0),
        {"success": False, "verified": False,
         "subtitle_tracks_before": 0, "subtitle_tracks_after": 0,
         "settings": {"0.0": 0.0}, "ignored_settings": []})),
        NullSink(), media, "r1")}
    reports["readback-empty"] = run_flow(FakeCall(with_generation(
        fake_script(0),
        {"success": True, "verified": True,
         "subtitle_tracks_before": 0, "subtitle_tracks_after": 1,
         "settings": {"0.0": 0.0}, "ignored_settings": []})),
        NullSink(), media, "r1")
    checks.append((reports["generation-failed"].get("verdict") == "generation-failed",
                   "vendor success=false with zero items -> generation-failed"))
    checks.append((reports["readback-empty"].get("verdict") == "readback-empty",
                   "ok generation with zero items -> readback-empty"))
    # stale/non-empty rows must NEVER rescue a failed generation
    for label, generation in (
        ("success=false", {"success": False, "verified": False,
                           "subtitle_tracks_before": 0, "subtitle_tracks_after": 1,
                           "settings": {"0.0": 0.0}, "ignored_settings": []}),
        ("verified=false", {"success": True, "verified": False,
                            "subtitle_tracks_before": 0, "subtitle_tracks_after": 1,
                            "settings": {"0.0": 0.0}, "ignored_settings": []}),
    ):
        stale = with_generation(fake_script(), generation)
        stale_report = run_flow(FakeCall(stale), NullSink(), media, "r1")
        checks.append((stale_report.get("verdict") == "generation-failed",
                       f"stale rows do not rescue generation {label}"))
    _check_cleanup_failure(checks)
    _check_invalid_readback(media, checks)
    reject_script = fake_script(0)
    reject_script[("timeline", "subtitle_generation_probe")] = {
        "success": True, "would_generate": True, "settings": {},
        "ignored_settings": ["language"]}
    reports["settings-rejected"] = run_flow(
        FakeCall(reject_script), NullSink(), media, "r1")
    candidates = reports["settings-rejected"].get("settings_candidates")
    checks.append((reports["settings-rejected"].get("verdict") == "settings-rejected"
                   and isinstance(candidates, list)
                   and len(candidates) == len(logic.LANGUAGE_CANDIDATES),
                   "all candidates keys-ignored -> settings-rejected"))

    sanitized = True
    for one in (report, reports["generation-failed"], reports["readback-empty"],
                reports["settings-rejected"]):
        try:
            logic.assert_sanitized(one)
        except logic.ProbeRefusalError:
            sanitized = False
    checks.append((sanitized, "flow reports pass committed-output allowlist"))
    for passed, label in checks:
        print(f"[{'ok' if passed else 'FAIL'}] selfcheck: {label}", flush=True)
        if not passed:
            failures += 1
    all_passed = failures == 0
    print(f"selfcheck: {'PASS' if all_passed else f'FAIL ({failures})'}", flush=True)
    return 0 if all_passed else 1


__all__ = ["flow_selfcheck"]
