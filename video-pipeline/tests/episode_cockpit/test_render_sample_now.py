"""Wave-1 ``render_sample_now`` regressions: the idempotent owner state machine.

Real dirs, real locks, real atomic rename — only the render itself and
the context resolution are faked (injected seams). These tests pin the
RIGHT contracts (P1-1/P1-2/P1-3/P1-5): the entry requires the open
reserve's attempt and verifies ownership before any action;
stored/recovering/in_progress return WITHOUT entering the render
function and WITHOUT budget writes; two concurrent renders call the
renderer EXACTLY ONCE with exactly one reserve+settle; the reserve
secures wall + preview seconds in one line so a parallel request
refuses pre-render; each crash point recovers append-once per journal;
tampered manifests stop typed with zero writes.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import threading
import time as time_module
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from services.cli.episode_runner_rebuild import RebuildStageError
from services.cli.sample_owned import (
    _atomic_sample_reserve_locked,
    _clamp_render_timeout,
)
from services.cli.sample_render import render_sample_now
from services.cli.sample_resolve import SampleRenderContext, build_sample_identity
from services.compile.sample_projection import project_sample_ir
from services.contracts.primitives import RecordFrameSpan
from services.episode_cockpit.consultation_selection_budget import (
    attempt_for,
    load_selection_budget_entries,
    reserve_preview,
    selection_budget_used,
    settle_preview,
)
from services.episode_cockpit.consultation_store import consume_budget, load_budget_limits
from services.episode_cockpit.errors import CockpitConflictError, CockpitUnprocessableError
from services.episode_cockpit.presentation_overrides import (
    PresentationOverrideSet,
    PresentationSettingOverride,
    UnimplementedPresentationNote,
)
from services.episode_cockpit.sample_complete import write_published_manifest
from services.episode_cockpit.sample_identity import (
    SAMPLE_MANIFEST_NAME,
    SAMPLE_PREVIEW_NAME,
    SAMPLE_RENDER_LOCK_NAME,
    SampleRequestIdentityV1,
    derive_sample_id,
    derive_sample_lineage,
)
from services.episode_cockpit.sample_journal import request_sample, sample_dir
from services.foundation_io import canonical_model_bytes
from tests.compile.test_sample_projection import _full_ir

if TYPE_CHECKING:
    from services.contracts.timeline_ir import TimelineIr0C
    from services.outputs.geometry import OutputId
    from services.preview.models import PresentationRenderSettings, TracePresentation

VIDEO_V1 = b"rendered-sample-v1"
VIDEO_V2 = b"rendered-sample-v2"


def _write_full_ir(tmp_path: Path) -> str:
    raw = canonical_model_bytes(_full_ir())
    path = tmp_path / "review" / "store" / "ir-v1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _identity(
    tmp_path: Path,
    operation_id: str = "op-render-1",
    *,
    output_id: OutputId = "landscape",
    windows: tuple[tuple[int, int], ...] = ((0, 30),),
) -> Any:
    return SampleRequestIdentityV1(
        episode_id="ep-r1",
        consultation_id="c1",
        judgment_id="j1",
        base_version="v1",
        base_plan_sha256="a" * 64,
        policy_sha256="b" * 64,
        output_id=output_id,
        windows=tuple(
            RecordFrameSpan(start_frame=s, end_frame=e) for s, e in windows
        ),
        operation_id=operation_id,
        full_ir_sha256=_write_full_ir(tmp_path),
    )


class _Resolver:
    """Fake context resolution with a call counter and an optional stale flip."""

    def __init__(
        self,
        mezzanine: Path,
        *,
        total_seconds: float = 2.0,
        presentation: PresentationOverrideSet | None = None,
        second: tuple[str, str] | None = None,
    ) -> None:
        self.mezzanine = mezzanine
        self.total_seconds = total_seconds
        self.presentation = presentation
        self.second = second
        self.calls = 0

    def __call__(self, episode_root: Path, identity: Any) -> SampleRenderContext:
        self.calls += 1
        head_version = "v1"
        plan_sha256 = "a" * 64
        if self.second is not None and self.calls >= 2:
            plan_sha256, head_version = self.second
        sample_ir = project_sample_ir(_full_ir(), list(identity.windows))
        return SampleRenderContext(
            head_version=head_version,
            plan_sha256=plan_sha256,
            policy_sha256="b" * 64,
            full_ir_sha256=identity.full_ir_sha256,
            sample_ir=sample_ir,
            total_seconds=self.total_seconds,
            mezzanine=self.mezzanine,
            presentation=self.presentation,
        )


class _FakeRender:
    """Fake renderer: records its controls, writes the preview bytes."""

    def __init__(self, video: bytes = VIDEO_V1, *, fail: Exception | None = None) -> None:
        self.video = video
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def __call__(  # noqa: PLR0913 (fake render seam mirrors the real adapter)
        self,
        sample_ir: TimelineIr0C,
        mezzanine: Path,
        out_dir: Path,
        *,
        timeout_seconds: float | None,
        presentation: PresentationRenderSettings | None,
        presentation_trace: TracePresentation | None,
    ) -> None:
        self.calls.append(
            {
                "timeout_seconds": timeout_seconds,
                "presentation": presentation,
                "presentation_trace": presentation_trace,
            }
        )
        if self.fail is not None:
            raise self.fail
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / SAMPLE_PREVIEW_NAME).write_bytes(self.video)


def _phases(tmp_path: Path) -> list[tuple[str, str | None]]:
    return [(e.phase, e.result) for e in load_selection_budget_entries(tmp_path)]


def _samples_bytes(tmp_path: Path) -> bytes:
    return (tmp_path / "consultation" / "samples.jsonl").read_bytes()


def _budget_bytes(tmp_path: Path) -> bytes:
    return (tmp_path / "consultation" / "selection-budget.jsonl").read_bytes()


def _sample_children(tmp_path: Path) -> list[str]:
    """Sample dir entries minus the persistent episode render lock."""
    return sorted(
        p.name for p in (tmp_path / "consultation" / "samples").iterdir()
        if p.name != SAMPLE_RENDER_LOCK_NAME
    )


def _tamper_manifest(tmp_path: Path, sample_id: str, **fields: Any) -> None:
    path = sample_dir(tmp_path, sample_id) / SAMPLE_MANIFEST_NAME
    raw = json.loads(path.read_bytes())
    raw.update(fields)
    path.write_bytes(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode())


def test_success_path_publishes_end_to_end(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    request = request_sample(tmp_path, identity)
    resolver = _Resolver(tmp_path / "mezzanine.mov")
    fake = _FakeRender()
    result = render_sample_now(
        tmp_path,
        identity,
        sample_attempt_id=request["sample_attempt_id"],
        resolve_fn=resolver,
        render_fn=fake,
    )
    assert result["state"] == "published"
    assert result["presentation_notes"] == []
    manifest = result["manifest"]
    assert manifest.status == "published"
    final_dir = sample_dir(tmp_path, derive_sample_id(identity))
    assert (final_dir / SAMPLE_PREVIEW_NAME).read_bytes() == VIDEO_V1
    assert manifest.content_sha256 == hashlib.sha256(VIDEO_V1).hexdigest()
    assert manifest.total_seconds == 2.0
    assert manifest.full_ir_sha256 == identity.full_ir_sha256
    assert manifest.sample_attempt_id == request["sample_attempt_id"]
    assert manifest.budget_entry_id == "selection-1"
    assert manifest.budget_reservation_sequence == 1
    assert manifest.run_id
    assert manifest.wall_seconds_used > 0
    assert tuple(manifest.lineage) == derive_sample_lineage(
        project_sample_ir(_full_ir(), list(identity.windows)))
    journal = (tmp_path / "consultation" / "samples.jsonl").read_bytes()
    assert journal.count(b"sample_reserved") == 1
    assert journal.count(b"sample_success") == 1
    assert _phases(tmp_path) == [
        ("preview_reserved", None),
        ("preview_settled", "succeeded"),
    ]
    entries = load_selection_budget_entries(tmp_path)
    assert entries[0].attempt_id == entries[1].attempt_id
    assert entries[1].preview_seconds_used == 2.0
    assert entries[1].wall_seconds_used == manifest.wall_seconds_used
    reserve_line = json.loads(_budget_bytes(tmp_path).splitlines()[0])
    assert reserve_line["preview_seconds_reserved"] == 2.0
    assert reserve_line["wall_seconds_reserved"] == (
        load_budget_limits().wall_seconds_limit)
    # The renderer timeout IS the reservation: the full remaining wall,
    # so a short render on a small remainder still executes and the
    # serialized measured total stays within the consultation cap.
    assert fake.calls[0]["timeout_seconds"] == reserve_line["wall_seconds_reserved"]
    assert fake.calls[0]["presentation"] is None
    assert fake.calls[0]["presentation_trace"] is None
    assert resolver.calls == 2  # pre-render resolve + under-lock re-verify
    assert _sample_children(tmp_path) == [
        derive_sample_id(identity)
    ]  # no temp leftovers


def test_second_render_over_published_is_stored_with_zero_work(tmp_path: Path) -> None:
    """P1-1: a resend after publish never re-enters the renderer."""
    identity = _identity(tmp_path, "op-twice")
    request = request_sample(tmp_path, identity)
    render_sample_now(
        tmp_path, identity, sample_attempt_id=request["sample_attempt_id"],
        resolve_fn=_Resolver(tmp_path / "m.mov"), render_fn=_FakeRender(),
    )
    samples_before = _samples_bytes(tmp_path)
    budget_before = _budget_bytes(tmp_path)
    resolver = _Resolver(tmp_path / "m.mov")
    fake = _FakeRender(video=VIDEO_V2)
    second = render_sample_now(
        tmp_path, identity, sample_attempt_id=request["sample_attempt_id"],
        resolve_fn=resolver, render_fn=fake,
    )
    assert second["state"] == "stored"
    assert fake.calls == []  # zero render work
    assert _samples_bytes(tmp_path) == samples_before  # zero journal writes
    assert _budget_bytes(tmp_path) == budget_before  # zero budget writes
    final_dir = sample_dir(tmp_path, derive_sample_id(identity))
    assert (final_dir / SAMPLE_PREVIEW_NAME).read_bytes() == VIDEO_V1
    assert _phases(tmp_path) == [
        ("preview_reserved", None),
        ("preview_settled", "succeeded"),
    ]


def test_concurrent_renders_yield_single_owner(tmp_path: Path) -> None:
    """P1-1: two concurrent renders → ONE render call, ONE reserve+settle."""
    identity = _identity(tmp_path, "op-race")
    request = request_sample(tmp_path, identity)
    attempt = request["sample_attempt_id"]
    entered = threading.Event()
    release = threading.Event()

    class _BlockingRender(_FakeRender):
        def __call__(self, *args: Any, **kwargs: Any) -> None:
            self.calls.append({"timeout_seconds": kwargs.get("timeout_seconds")})
            entered.set()
            assert release.wait(timeout=30)
            args[2].mkdir(parents=True, exist_ok=True)
            (args[2] / SAMPLE_PREVIEW_NAME).write_bytes(self.video)

    fake_a = _BlockingRender()
    resolver_a = _Resolver(tmp_path / "m.mov")
    results: dict[str, Any] = {}

    def _run_a() -> None:
        results["a"] = render_sample_now(
            tmp_path, identity, sample_attempt_id=attempt,
            resolve_fn=resolver_a, render_fn=fake_a,
        )

    worker = threading.Thread(target=_run_a)
    worker.start()
    assert entered.wait(timeout=30)  # A holds the owner lock, budget reserved
    ledger_before = _budget_bytes(tmp_path)
    journal_before = _samples_bytes(tmp_path)
    join = request_sample(tmp_path, identity)
    assert join["sample_attempt_id"] == attempt  # same open attempt
    resolver_b = _Resolver(tmp_path / "m.mov")
    fake_b = _FakeRender(video=VIDEO_V2)
    out_b = render_sample_now(
        tmp_path, identity, sample_attempt_id=attempt,
        resolve_fn=resolver_b, render_fn=fake_b,
    )
    assert out_b["state"] == "in_progress"
    assert fake_b.calls == []
    assert resolver_b.calls == 0  # refused before resolve, let alone render
    assert _budget_bytes(tmp_path) == ledger_before
    assert _samples_bytes(tmp_path) == journal_before
    release.set()
    worker.join(timeout=30)
    assert results["a"]["state"] == "published"
    assert len(fake_a.calls) == 1  # the renderer ran EXACTLY ONCE
    assert resolver_a.calls == 2
    assert _phases(tmp_path) == [
        ("preview_reserved", None),
        ("preview_settled", "succeeded"),
    ]
    journal = _samples_bytes(tmp_path)
    assert journal.count(b"sample_reserved") == 1
    assert journal.count(b"sample_success") == 1
    samples_after = _samples_bytes(tmp_path)
    budget_after = _budget_bytes(tmp_path)
    out_c = render_sample_now(
        tmp_path, identity, sample_attempt_id=attempt,
        resolve_fn=_Resolver(tmp_path / "m.mov"), render_fn=_FakeRender(video=VIDEO_V2),
    )
    assert out_c["state"] == "stored"
    assert _samples_bytes(tmp_path) == samples_after
    assert _budget_bytes(tmp_path) == budget_after


def test_render_without_open_reserve_refuses(tmp_path: Path) -> None:
    """P1-1: ownership is verified BEFORE any action — no reserve, no render."""
    identity = _identity(tmp_path, "op-stray")
    resolver = _Resolver(tmp_path / "m.mov")
    fake = _FakeRender()
    with pytest.raises(RebuildStageError, match="sample-attempt-not-open") as exc_info:
        render_sample_now(
            tmp_path, identity, sample_attempt_id="sample-attempt-999",
            resolve_fn=resolver, render_fn=fake,
        )
    assert exc_info.value.code == "sample-attempt-not-open"
    assert fake.calls == []
    assert resolver.calls == 0


def test_serialized_renders_never_overlap_and_stay_within_cap(tmp_path: Path) -> None:
    """Sample renders serialize per episode: the second waits for the
    first, each reserves the full remaining wall at its turn, and the
    combined measured total stays within the 600 s consultation cap."""
    first = _identity(tmp_path, "op-a", windows=((0, 30),))
    second = _identity(tmp_path, "op-b", windows=((60, 90),))
    req_a = request_sample(tmp_path, first)
    req_b = request_sample(tmp_path, second)
    entered = threading.Event()
    release = threading.Event()
    spans: dict[str, tuple[float, float]] = {}

    class _BlockingRender(_FakeRender):
        def __call__(self, *args: Any, **kwargs: Any) -> None:
            self.calls.append({"timeout_seconds": kwargs.get("timeout_seconds")})
            entered.set()
            assert release.wait(timeout=30)
            spans["a"] = (time_module.monotonic(), time_module.monotonic())
            args[2].mkdir(parents=True, exist_ok=True)
            (args[2] / SAMPLE_PREVIEW_NAME).write_bytes(self.video)

    class _TimedRender(_FakeRender):
        def __call__(self, *args: Any, **kwargs: Any) -> None:
            started = time_module.monotonic()
            super().__call__(*args, **kwargs)
            spans["b"] = (started, time_module.monotonic())

    fake_a = _BlockingRender()
    results: dict[str, Any] = {}
    errors: dict[str, Exception] = {}

    def _run_a() -> None:
        try:
            results["a"] = render_sample_now(
                tmp_path, first, sample_attempt_id=req_a["sample_attempt_id"],
                resolve_fn=_Resolver(tmp_path / "m.mov"), render_fn=fake_a,
            )
        except Exception as error:  # noqa: BLE001 (thread result capture)
            errors["a"] = error

    def _run_b() -> None:
        try:
            results["b"] = render_sample_now(
                tmp_path, second, sample_attempt_id=req_b["sample_attempt_id"],
                resolve_fn=_Resolver(tmp_path / "m.mov"),
                render_fn=_TimedRender(video=VIDEO_V2),
            )
        except Exception as error:  # noqa: BLE001 (thread result capture)
            errors["b"] = error

    worker_a = threading.Thread(target=_run_a)
    worker_a.start()
    assert entered.wait(timeout=30)
    worker_b = threading.Thread(target=_run_b)
    worker_b.start()
    assert "b" not in results  # serialized behind the first render
    release.set()
    worker_a.join(timeout=30)
    worker_b.join(timeout=30)
    assert not errors
    assert results["a"]["state"] == "published"
    assert results["b"]["state"] == "published"
    assert spans["a"][1] <= spans["b"][0]  # no render overlap
    reserve_lines = [
        json.loads(line) for line in _budget_bytes(tmp_path).splitlines()
        if json.loads(line)["phase"] == "preview_reserved"
    ]
    assert len(reserve_lines) == 2
    limit = load_budget_limits().wall_seconds_limit
    assert reserve_lines[0]["wall_seconds_reserved"] == limit
    assert reserve_lines[1]["wall_seconds_reserved"] == pytest.approx(
        limit - results["a"]["manifest"].wall_seconds_used)
    assert fake_a.calls[0]["timeout_seconds"] == limit
    measured = (
        results["a"]["manifest"].wall_seconds_used
        + results["b"]["manifest"].wall_seconds_used
    )
    assert measured <= limit  # serialized measured totals stay within the cap


def test_serialized_second_render_sees_settled_remainder(tmp_path: Path) -> None:
    """The second serialized render reserves the remainder left after the
    first settles — the open full-wall reservation never strands the
    episode, it folds back into the wall on settle."""
    consume_budget(tmp_path, "c1", llm_calls=0, intervals=0, wall_seconds=400.0)
    first = _identity(tmp_path, "op-a", windows=((0, 30),))
    second = _identity(tmp_path, "op-b", windows=((60, 90),))
    req_a = request_sample(tmp_path, first)
    req_b = request_sample(tmp_path, second)
    entered = threading.Event()
    release = threading.Event()

    class _BlockingRender(_FakeRender):
        def __call__(self, *args: Any, **kwargs: Any) -> None:
            self.calls.append({"timeout_seconds": kwargs.get("timeout_seconds")})
            entered.set()
            assert release.wait(timeout=30)
            args[2].mkdir(parents=True, exist_ok=True)
            (args[2] / SAMPLE_PREVIEW_NAME).write_bytes(self.video)

    fake_a = _BlockingRender()
    results: dict[str, Any] = {}
    errors: dict[str, Exception] = {}

    def _run_a() -> None:
        try:
            results["a"] = render_sample_now(
                tmp_path, first, sample_attempt_id=req_a["sample_attempt_id"],
                resolve_fn=_Resolver(tmp_path / "m.mov"), render_fn=fake_a,
            )
        except Exception as error:  # noqa: BLE001 (thread result capture)
            errors["a"] = error

    def _run_b() -> None:
        try:
            results["b"] = render_sample_now(
                tmp_path, second, sample_attempt_id=req_b["sample_attempt_id"],
                resolve_fn=_Resolver(tmp_path / "m.mov"),
                render_fn=_FakeRender(video=VIDEO_V2),
            )
        except Exception as error:  # noqa: BLE001 (thread result capture)
            errors["b"] = error

    worker_a = threading.Thread(target=_run_a)
    worker_a.start()
    assert entered.wait(timeout=30)
    worker_b = threading.Thread(target=_run_b)
    worker_b.start()
    release.set()
    worker_a.join(timeout=30)
    worker_b.join(timeout=30)
    assert not errors
    assert results["a"]["state"] == "published"
    assert results["b"]["state"] == "published"
    reserve_lines = [
        json.loads(line) for line in _budget_bytes(tmp_path).splitlines()
        if json.loads(line)["phase"] == "preview_reserved"
    ]
    assert len(reserve_lines) == 2
    assert reserve_lines[0]["wall_seconds_reserved"] == pytest.approx(200.0)
    assert fake_a.calls[0]["timeout_seconds"] == pytest.approx(200.0)
    assert reserve_lines[1]["wall_seconds_reserved"] == pytest.approx(
        200.0 - results["a"]["manifest"].wall_seconds_used)


def test_short_render_executes_on_small_remainder(tmp_path: Path) -> None:
    """A 119 s remainder is no refusal: the short render reserves the
    full remainder as its timeout and executes."""
    consume_budget(tmp_path, "c1", llm_calls=0, intervals=0, wall_seconds=481.0)
    identity = _identity(tmp_path, "op-small")
    request = request_sample(tmp_path, identity)
    fake = _FakeRender()
    result = render_sample_now(
        tmp_path, identity, sample_attempt_id=request["sample_attempt_id"],
        resolve_fn=_Resolver(tmp_path / "m.mov"), render_fn=fake,
    )
    assert result["state"] == "published"
    reserve_line = json.loads(_budget_bytes(tmp_path).splitlines()[0])
    assert reserve_line["wall_seconds_reserved"] == pytest.approx(119.0)
    assert fake.calls[0]["timeout_seconds"] == pytest.approx(119.0)


def test_exhausted_wall_refuses_before_render(tmp_path: Path) -> None:
    """A fully consumed wall refuses typed before any render or reserve."""
    consume_budget(tmp_path, "c1", llm_calls=0, intervals=0, wall_seconds=600.0)
    identity = _identity(tmp_path, "op-spent")
    request = request_sample(tmp_path, identity)
    fake = _FakeRender()
    with pytest.raises(
        RebuildStageError, match="consultation-selection-deadline-exceeded"
    ) as exc_info:
        render_sample_now(
            tmp_path, identity, sample_attempt_id=request["sample_attempt_id"],
            resolve_fn=_Resolver(tmp_path / "m.mov"), render_fn=fake,
        )
    assert exc_info.value.code == "consultation-selection-deadline-exceeded"
    assert fake.calls == []  # refused BEFORE any render
    assert not (tmp_path / "consultation" / "selection-budget.jsonl").exists()


def test_render_timeout_never_exceeds_reservation() -> None:
    """P1-1: a render attempt whose requested wall exceeds its reservation
    is clamped to the reservation — the renderer never gets more time
    than the ledger holds for it."""
    assert _clamp_render_timeout(600.0, 120.0) == 120.0
    assert _clamp_render_timeout(120.0, 120.0) == 120.0
    assert _clamp_render_timeout(60.0, 120.0) == 60.0


def _drop_lines(path: Path, keep: Any) -> None:
    path.write_bytes(b"\n".join(
        line for line in path.read_bytes().splitlines() if keep(line)) + b"\n")


def test_crash_after_rename_before_success_recovers(tmp_path: Path) -> None:
    """P1-3a: final without success/settle → resend completes EACH append-once."""
    identity = _identity(tmp_path, "op-crash-a")
    request = request_sample(tmp_path, identity)
    attempt = request["sample_attempt_id"]
    result = render_sample_now(
        tmp_path, identity, sample_attempt_id=attempt,
        resolve_fn=_Resolver(tmp_path / "m.mov"), render_fn=_FakeRender(),
    )
    assert result["state"] == "published"
    _drop_lines(
        tmp_path / "consultation" / "samples.jsonl",
        lambda line: b"sample_success" not in line)
    _drop_lines(
        tmp_path / "consultation" / "selection-budget.jsonl",
        lambda line: b"preview_settled" not in line)
    samples_before = _samples_bytes(tmp_path)
    assert b"sample_success" not in samples_before
    fake = _FakeRender(video=VIDEO_V2)
    resolver = _Resolver(tmp_path / "m.mov")
    recovered = render_sample_now(
        tmp_path, identity, sample_attempt_id=attempt,
        resolve_fn=resolver, render_fn=fake,
    )
    assert recovered["state"] == "recovering"
    assert fake.calls == []  # no re-render
    assert (sample_dir(tmp_path, derive_sample_id(identity))
            / SAMPLE_PREVIEW_NAME).read_bytes() == VIDEO_V1
    assert _samples_bytes(tmp_path).count(b"sample_success") == 1
    assert _phases(tmp_path) == [
        ("preview_reserved", None),
        ("preview_settled", "succeeded"),
    ]
    assert load_selection_budget_entries(tmp_path)[-1].preview_seconds_used == 2.0


def test_crash_after_success_before_settle_recovers(tmp_path: Path) -> None:
    """P1-3b: success present but settle missing → only the settle completes."""
    identity = _identity(tmp_path, "op-crash-b")
    request = request_sample(tmp_path, identity)
    attempt = request["sample_attempt_id"]
    render_sample_now(
        tmp_path, identity, sample_attempt_id=attempt,
        resolve_fn=_Resolver(tmp_path / "m.mov"), render_fn=_FakeRender(),
    )
    _drop_lines(
        tmp_path / "consultation" / "selection-budget.jsonl",
        lambda line: b"preview_settled" not in line)
    journal_before = _samples_bytes(tmp_path)
    assert journal_before.count(b"sample_success") == 1
    fake = _FakeRender(video=VIDEO_V2)
    recovered = render_sample_now(
        tmp_path, identity, sample_attempt_id=attempt,
        resolve_fn=_Resolver(tmp_path / "m.mov"), render_fn=fake,
    )
    assert recovered["state"] == "recovering"
    assert fake.calls == []
    assert _samples_bytes(tmp_path) == journal_before  # success NOT duplicated
    assert _phases(tmp_path) == [
        ("preview_reserved", None),
        ("preview_settled", "succeeded"),
    ]


def test_crash_mid_render_conserves_orphan_allowance(tmp_path: Path) -> None:
    """P1-3c: an open reserve from a crashed render stays conservatively consumed."""
    identity = _identity(tmp_path, "op-crash-c")
    request = request_sample(tmp_path, identity)
    orphan = _atomic_sample_reserve_locked(tmp_path, identity, 2.0, 50.0)
    assert orphan.attempt_id == "selection-1"
    result = render_sample_now(
        tmp_path, identity, sample_attempt_id=request["sample_attempt_id"],
        resolve_fn=_Resolver(tmp_path / "m.mov"), render_fn=_FakeRender(),
    )
    assert result["state"] == "published"
    assert result["manifest"].budget_entry_id == "selection-2"
    orphans = [
        e for e in load_selection_budget_entries(tmp_path)
        if e.attempt_id == "selection-1"
    ]
    assert [e.phase for e in orphans] == ["preview_reserved"]  # never replayed free
    used = selection_budget_used(tmp_path)
    assert used.preview_seconds == pytest.approx(4.0)  # orphan 2.0 + real 2.0
    assert used.wall_seconds == pytest.approx(
        50.0 + result["manifest"].wall_seconds_used)


def _fresh_published(tmp_path: Path, operation_id: str) -> tuple[Any, Any]:
    identity = _identity(tmp_path, operation_id)
    request = request_sample(tmp_path, identity)
    result = render_sample_now(
        tmp_path, identity, sample_attempt_id=request["sample_attempt_id"],
        resolve_fn=_Resolver(tmp_path / "m.mov"), render_fn=_FakeRender(),
    )
    assert result["state"] == "published"
    return identity, result["manifest"]


def _assert_tamper_stops(
    tmp_path: Path, identity: Any, manifest: Any, **fields: Any
) -> None:
    sample_id = derive_sample_id(identity)
    _tamper_manifest(tmp_path, sample_id, **fields)
    samples_before = _samples_bytes(tmp_path)
    budget_before = _budget_bytes(tmp_path)
    resolver = _Resolver(tmp_path / "m.mov")
    fake = _FakeRender(video=VIDEO_V2)
    with pytest.raises(
        CockpitUnprocessableError, match="sample-recovery-verification-failed"
    ) as exc_info:
        render_sample_now(
            tmp_path, identity, sample_attempt_id=manifest.sample_attempt_id,
            resolve_fn=resolver, render_fn=fake,
        )
    assert exc_info.value.code == "sample-recovery-verification-failed"
    assert fake.calls == []  # NO success completion path ran
    assert resolver.calls == 0  # stopped before resolution
    assert _samples_bytes(tmp_path) == samples_before  # zero journal writes
    assert _budget_bytes(tmp_path) == budget_before
    assert (sample_dir(tmp_path, sample_id) / SAMPLE_PREVIEW_NAME).read_bytes() == VIDEO_V1


def test_render_tampered_sample_sha_stops_with_zero_writes(tmp_path: Path) -> None:
    """P1-5: the re-derived sample sha beats the stored manifest."""
    identity, manifest = _fresh_published(tmp_path, "op-tamper-sha")
    _assert_tamper_stops(tmp_path, identity, manifest, sample_ir_sha256="0" * 64)


def test_render_tampered_lineage_stops_with_zero_writes(tmp_path: Path) -> None:
    """P1-5: lineage is re-derived — a stored lie never completes."""
    identity, manifest = _fresh_published(tmp_path, "op-tamper-lin")
    tampered = manifest.model_dump(mode="json")
    tampered["lineage"] = [{"source_item_id": "evil", "sample_item_id": "evil.s0"}]
    path = sample_dir(tmp_path, derive_sample_id(identity)) / SAMPLE_MANIFEST_NAME
    path.write_bytes(json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode())
    samples_before = _samples_bytes(tmp_path)
    budget_before = _budget_bytes(tmp_path)
    with pytest.raises(
        CockpitUnprocessableError, match="sample-recovery-verification-failed"
    ):
        render_sample_now(
            tmp_path, identity, sample_attempt_id=manifest.sample_attempt_id,
            resolve_fn=_Resolver(tmp_path / "m.mov"), render_fn=_FakeRender(video=VIDEO_V2),
        )
    assert _samples_bytes(tmp_path) == samples_before
    assert _budget_bytes(tmp_path) == budget_before


def test_render_tampered_budget_entry_stops_with_zero_writes(tmp_path: Path) -> None:
    """P1-5: the budget entry must name a real ledger line for this judgment."""
    identity, manifest = _fresh_published(tmp_path, "op-tamper-budget")
    _assert_tamper_stops(tmp_path, identity, manifest, budget_entry_id="selection-999")


def test_render_tampered_run_id_stops_with_zero_writes(tmp_path: Path) -> None:
    """P1-5: a blanked run id never completes."""
    identity, manifest = _fresh_published(tmp_path, "op-tamper-run")
    _assert_tamper_stops(tmp_path, identity, manifest, run_id="")


def test_unverified_final_is_409_and_dir_untouched(tmp_path: Path) -> None:
    """A final dir that appears mid-render goes down the verified path only."""
    identity = _identity(tmp_path, "op-tamper")
    request = request_sample(tmp_path, identity)
    attempt = request["sample_attempt_id"]
    entered = threading.Event()
    release = threading.Event()

    class _BlockingRender(_FakeRender):
        def __call__(self, *args: Any, **kwargs: Any) -> None:
            self.calls.append({"timeout_seconds": kwargs.get("timeout_seconds")})
            entered.set()
            assert release.wait(timeout=30)
            args[2].mkdir(parents=True, exist_ok=True)
            (args[2] / SAMPLE_PREVIEW_NAME).write_bytes(self.video)

    results: dict[str, Any] = {}
    error: dict[str, Any] = {}

    def _run() -> None:
        try:
            results["out"] = render_sample_now(
                tmp_path, identity, sample_attempt_id=attempt,
                resolve_fn=_Resolver(tmp_path / "m.mov"), render_fn=_BlockingRender(),
            )
        except CockpitConflictError as exc:
            error["exc"] = exc

    worker = threading.Thread(target=_run)
    worker.start()
    assert entered.wait(timeout=30)
    final_dir = sample_dir(tmp_path, derive_sample_id(identity))
    final_dir.mkdir(parents=True, exist_ok=True)
    (final_dir / SAMPLE_PREVIEW_NAME).write_bytes(b"tampered-bytes")
    write_published_manifest(
        final_dir, identity, sample_ir=project_sample_ir(_full_ir(), list(identity.windows)),
        total_seconds=2.0, content_sha256=hashlib.sha256(VIDEO_V1).hexdigest(),
        sample_attempt_id=attempt, budget_entry_id="selection-1",
        budget_reservation_sequence=1, run_id="run-race-1", wall_seconds_used=0.05,
    )
    release.set()
    worker.join(timeout=30)
    assert "out" not in results
    assert isinstance(error["exc"], CockpitConflictError)
    assert error["exc"].code == "sample-publish-conflict"
    assert (final_dir / SAMPLE_PREVIEW_NAME).read_bytes() == b"tampered-bytes"
    phases = _phases(tmp_path)
    assert phases[-2:] == [("preview_reserved", None), ("preview_settled", "failed")]
    assert load_selection_budget_entries(tmp_path)[-1].failure_code == (
        "sample-publish-conflict"
    )
    journal = (tmp_path / "consultation" / "samples.jsonl").read_bytes()
    assert journal.count(b"sample_failed") == 1
    assert b"sample_success" not in journal
    assert _sample_children(tmp_path) == [
        derive_sample_id(identity)
    ]


def test_tampered_video_stops_at_entry_with_zero_writes(tmp_path: Path) -> None:
    """Entry verification (P1-1) stops before resolve/render with zero writes."""
    identity = _identity(tmp_path, "op-tamper-entry")
    request = request_sample(tmp_path, identity)
    render_sample_now(
        tmp_path, identity, sample_attempt_id=request["sample_attempt_id"],
        resolve_fn=_Resolver(tmp_path / "m.mov"), render_fn=_FakeRender(),
    )
    final_dir = sample_dir(tmp_path, derive_sample_id(identity))
    (final_dir / SAMPLE_PREVIEW_NAME).write_bytes(b"tampered-bytes")
    samples_before = _samples_bytes(tmp_path)
    budget_before = _budget_bytes(tmp_path)
    resolver = _Resolver(tmp_path / "m.mov")
    fake = _FakeRender(video=VIDEO_V2)
    with pytest.raises(
        CockpitUnprocessableError, match="sample-recovery-verification-failed"
    ):
        render_sample_now(
            tmp_path, identity, sample_attempt_id=request["sample_attempt_id"],
            resolve_fn=resolver, render_fn=fake,
        )
    assert fake.calls == []
    assert resolver.calls == 0
    assert _samples_bytes(tmp_path) == samples_before
    assert _budget_bytes(tmp_path) == budget_before
    assert (final_dir / SAMPLE_PREVIEW_NAME).read_bytes() == b"tampered-bytes"


def test_render_failure_settles_once_and_leaves_no_final(tmp_path: Path) -> None:
    identity = _identity(tmp_path, "op-boom")
    request = request_sample(tmp_path, identity)
    with pytest.raises(
        RebuildStageError, match="sample-render-failed"
    ) as exc_info:
        render_sample_now(
            tmp_path, identity, sample_attempt_id=request["sample_attempt_id"],
            resolve_fn=_Resolver(tmp_path / "m.mov"),
            render_fn=_FakeRender(fail=RuntimeError("boom")),
        )
    assert exc_info.value.code == "sample-render-failed"
    assert _phases(tmp_path) == [
        ("preview_reserved", None),
        ("preview_settled", "failed"),
    ]
    assert load_selection_budget_entries(tmp_path)[-1].failure_code == (
        "sample-render-failed"
    )
    journal = (tmp_path / "consultation" / "samples.jsonl").read_bytes()
    assert journal.count(b"sample_failed") == 1
    assert b"boom" in journal
    assert not sample_dir(tmp_path, derive_sample_id(identity)).exists()
    assert _sample_children(tmp_path) == []


def test_output_refusal_happens_before_render(tmp_path: Path) -> None:
    identity = _identity(tmp_path, "op-vertical", output_id="vertical")
    resolver = _Resolver(tmp_path / "m.mov")
    fake = _FakeRender()
    with pytest.raises(
        RebuildStageError, match="sample-output-unsupported"
    ) as exc_info:
        render_sample_now(
            tmp_path, identity, sample_attempt_id="sample-attempt-1",
            resolve_fn=resolver, render_fn=fake,
        )
    assert exc_info.value.code == "sample-output-unsupported"
    assert fake.calls == []
    assert resolver.calls == 0
    assert load_selection_budget_entries(tmp_path) == []
    journal = (tmp_path / "consultation" / "samples.jsonl").read_bytes()
    assert journal.count(b"sample_failed") == 1


def test_stale_base_between_render_and_publish_refuses(tmp_path: Path) -> None:
    identity = _identity(tmp_path, "op-stale")
    request = request_sample(tmp_path, identity)
    resolver = _Resolver(tmp_path / "m.mov", second=("0" * 64, "v1"))
    with pytest.raises(
        RebuildStageError, match="sample-base-changed"
    ) as exc_info:
        render_sample_now(
            tmp_path, identity, sample_attempt_id=request["sample_attempt_id"],
            resolve_fn=resolver, render_fn=_FakeRender(),
        )
    assert exc_info.value.code == "sample-base-changed"
    assert resolver.calls == 2
    assert _phases(tmp_path) == [
        ("preview_reserved", None),
        ("preview_settled", "failed"),
    ]
    journal = (tmp_path / "consultation" / "samples.jsonl").read_bytes()
    assert journal.count(b"sample_failed") == 1
    assert not sample_dir(tmp_path, derive_sample_id(identity)).exists()


def test_runner_active_refusal_settles_once(tmp_path: Path) -> None:
    identity = _identity(tmp_path, "op-locked")
    request = request_sample(tmp_path, identity)
    lock_path = tmp_path / "runner.lock"
    with lock_path.open("a+b") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        try:
            with pytest.raises(
                RebuildStageError, match="runner-active"
            ) as exc_info:
                render_sample_now(
                    tmp_path, identity, sample_attempt_id=request["sample_attempt_id"],
                    resolve_fn=_Resolver(tmp_path / "m.mov"), render_fn=_FakeRender(),
                )
            assert exc_info.value.code == "runner-active"
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
    assert _phases(tmp_path) == [
        ("preview_reserved", None),
        ("preview_settled", "failed"),
    ]
    journal = (tmp_path / "consultation" / "samples.jsonl").read_bytes()
    assert journal.count(b"sample_failed") == 1
    assert not sample_dir(tmp_path, derive_sample_id(identity)).exists()


def test_presentation_overrides_forwarded_and_notes_surfaced(tmp_path: Path) -> None:
    identity = _identity(tmp_path, "op-pres")
    request = request_sample(tmp_path, identity)
    override_set = PresentationOverrideSet(
        overrides=(
            PresentationSettingOverride(
                command_id="cmd-1",
                command_kind="subtitle_shorter",
                setting="subtitle_max_chars_per_line",
                value=16,
            ),
        ),
        notes=(
            UnimplementedPresentationNote(
                command_id="cmd-2",
                command_kind="match_color",
                detail="no review-plane knob for color matching",
            ),
        ),
    )
    fake = _FakeRender()
    result = render_sample_now(
        tmp_path, identity, sample_attempt_id=request["sample_attempt_id"],
        resolve_fn=_Resolver(tmp_path / "m.mov", presentation=override_set),
        render_fn=fake,
    )
    assert result["state"] == "published"
    settings = fake.calls[0]["presentation"]
    assert settings is not None
    assert settings.subtitle_max_chars_per_line == 16
    trace = fake.calls[0]["presentation_trace"]
    assert trace is not None
    assert [note.command_kind for note in trace.notes] == ["match_color"]
    assert result["presentation_notes"][0]["command_kind"] == "match_color"


def test_preview_budget_exhaustion_refuses_before_render(tmp_path: Path) -> None:
    identity = _identity(tmp_path, "op-poor")
    request = request_sample(tmp_path, identity)
    prefill = attempt_for(1, "c1", "j1")
    reserve_preview(tmp_path, prefill, 30.0)
    settle_preview(
        tmp_path, prefill, preview_seconds=30.0, wall_elapsed=0.1, result="succeeded",
    )
    fake = _FakeRender()
    with pytest.raises(
        RebuildStageError, match="consultation-preview-budget-exhausted"
    ) as exc_info:
        render_sample_now(
            tmp_path, identity, sample_attempt_id=request["sample_attempt_id"],
            resolve_fn=_Resolver(tmp_path / "m.mov"), render_fn=fake,
        )
    assert exc_info.value.code == "consultation-preview-budget-exhausted"
    assert fake.calls == []
    assert len(load_selection_budget_entries(tmp_path)) == 2  # prefill only
    journal = (tmp_path / "consultation" / "samples.jsonl").read_bytes()
    assert journal.count(b"sample_failed") == 1


def test_reorder_windows_resolve_to_same_sample(tmp_path: Path) -> None:
    forward = SampleRequestIdentityV1(
        episode_id="ep-r1",
        consultation_id="c1",
        judgment_id="j1",
        base_version="v1",
        base_plan_sha256="a" * 64,
        policy_sha256="b" * 64,
        output_id="landscape",
        windows=(
            RecordFrameSpan(start_frame=0, end_frame=30),
            RecordFrameSpan(start_frame=60, end_frame=90),
        ),
        operation_id="op-reorder",
        full_ir_sha256=_write_full_ir(tmp_path),
    )
    reordered = forward.model_copy(
        update={
            "windows": (
                RecordFrameSpan(start_frame=60, end_frame=90),
                RecordFrameSpan(start_frame=0, end_frame=30),
            )
        }
    )
    first = request_sample(tmp_path, forward)
    result = render_sample_now(
        tmp_path, reordered, sample_attempt_id=first["sample_attempt_id"],
        resolve_fn=_Resolver(tmp_path / "m.mov"), render_fn=_FakeRender(),
    )
    assert result["state"] == "published"
    assert result["manifest"].sample_id == first["manifest"].sample_id


def test_build_sample_identity_refuses_without_bundle(tmp_path: Path) -> None:
    """P1-5: server-side identity construction fails closed with no store."""
    with pytest.raises(RebuildStageError, match="sample-bundle-unreadable"):
        build_sample_identity(
            tmp_path, consultation_id="c1", judgment_id="j1",
            windows=(RecordFrameSpan(start_frame=0, end_frame=30),),
            operation_id="op-nostore",
        )
