"""ime paired switch/restore contract against an injected fake swift runner.

Covers the Opus-ordered requirements (2026-09-06 01:35Z): paired restore
on the success/exception paths, readback verification after every select
(never the exit code alone), the original recorded live (never
hardcoded), no-switch when already English, the
restore-only-if-switch-succeeded policy, and typed errors for every
failure mode. No real swift, IME, or GUI is ever contacted: the fake
replaces the module-level ``_run_swift`` seam.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from services.cu_client import ENGLISH_SOURCE_ID, current_input_source, english_typing, ime
from services.cu_client.errors import (
    ImeReadError,
    ImeRestoreError,
    ImeSelectError,
)

KOTOERI = "com.apple.inputmethod.Kotoeri.Romaji"
"""Plausible non-ASCII original (the real operator default is read live, never assumed)."""


class FakeSwiftIme:
    """Fake ``_run_swift``: in-memory input-source state + call recording.

    Knobs: ``available`` (ids the select script finds), ``lie_for`` (ids
    whose select exits 0 WITHOUT taking effect — simulates the untrusted
    success return), and ``read_exit`` / ``read_stdout`` (read-side
    failures).
    """

    def __init__(
        self,
        *,
        current: str,
        available: Iterable[str] = (),
        lie_for: Iterable[str] = (),
        read_exit: int = 0,
        read_stdout: str | None = None,
    ) -> None:
        self.current = current
        self.available = {current, *available}
        self.lie_for = set(lie_for)
        self.read_exit = read_exit
        self.read_stdout = read_stdout
        self.calls: list[str] = []

    def __call__(self, source: str, args: list[str]) -> tuple[int, str]:
        if source == ime._INPUT_SOURCE_SWIFT:
            self.calls.append("read")
            stdout = (
                self.read_stdout
                if self.read_stdout is not None
                else f"id: {self.current} | lang: en | type: keyboard layout | local: X"
            )
            return self.read_exit, stdout
        if source == ime._IM_SELECT_SWIFT:
            target = args[0]
            self.calls.append(f"select:{target}")
            if target not in self.available:
                return 2, f"not found: {target}"
            if target not in self.lie_for:
                self.current = target
            return 0, f"selected {target}"
        raise AssertionError(f"unexpected swift source: {source[:40]!r}")


def test_switch_and_restore_are_a_readback_verified_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSwiftIme(current=KOTOERI, available=(ENGLISH_SOURCE_ID,))
    monkeypatch.setattr(ime, "_run_swift", fake)
    with english_typing() as original:
        assert original == KOTOERI  # original recorded live, yielded to the body
        assert fake.current == ENGLISH_SOURCE_ID
    assert fake.current == KOTOERI  # restored
    assert fake.calls == [
        "read",  # record the original (never hardcoded)
        f"select:{ENGLISH_SOURCE_ID}",
        "read",  # verify the switch took effect
        f"select:{KOTOERI}",
        "read",  # verify the restore took effect
    ]


def test_already_english_never_switches_but_still_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSwiftIme(current=ENGLISH_SOURCE_ID)
    monkeypatch.setattr(ime, "_run_swift", fake)
    with english_typing() as original:
        assert original == ENGLISH_SOURCE_ID
        assert fake.calls == ["read"]  # no select happened inside the body
    assert fake.calls == ["read", "read"]  # leave-state still readback-verified
    assert fake.current == ENGLISH_SOURCE_ID


def test_body_raising_still_restores_and_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSwiftIme(current=KOTOERI, available=(ENGLISH_SOURCE_ID,))
    monkeypatch.setattr(ime, "_run_swift", fake)
    with pytest.raises(RuntimeError, match="boom"), english_typing():
        raise RuntimeError("boom")
    assert fake.current == KOTOERI
    assert fake.calls == [
        "read",
        f"select:{ENGLISH_SOURCE_ID}",
        "read",
        f"select:{KOTOERI}",
        "read",
    ]


def test_failed_initial_switch_never_yields_and_never_restores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ABC is not in `available` -> the select script exits 2 (not found).
    fake = FakeSwiftIme(current=KOTOERI)
    monkeypatch.setattr(ime, "_run_swift", fake)
    entered = False
    with pytest.raises(ImeSelectError, match="exited 2"), english_typing():
        entered = True
    assert not entered  # the context manager never yielded
    assert fake.current == KOTOERI  # nothing of ours to undo; source unchanged
    assert fake.calls == ["read", f"select:{ENGLISH_SOURCE_ID}"]  # no restore attempt


def test_restore_readback_mismatch_raises_ime_restore_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The restore select LIES: exit 0 but the source stays on English.
    fake = FakeSwiftIme(
        current=KOTOERI, available=(ENGLISH_SOURCE_ID,), lie_for=(KOTOERI,)
    )
    monkeypatch.setattr(ime, "_run_swift", fake)
    with pytest.raises(ImeRestoreError) as excinfo, english_typing():
        pass
    message = str(excinfo.value)
    assert KOTOERI in message  # both ids carried
    assert ENGLISH_SOURCE_ID in message
    assert fake.current == ENGLISH_SOURCE_ID  # the drift is recorded, not masked


def test_unparseable_read_is_ime_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSwiftIme(current=KOTOERI, read_stdout="garbage with no id line")
    monkeypatch.setattr(ime, "_run_swift", fake)
    with pytest.raises(ImeReadError, match="cannot parse"):
        current_input_source()


def test_nonzero_read_exit_is_ime_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSwiftIme(current=KOTOERI, read_exit=1)
    monkeypatch.setattr(ime, "_run_swift", fake)
    with pytest.raises(ImeReadError, match="exited 1"):
        current_input_source()


def test_switch_readback_mismatch_is_ime_select_error_with_both_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The initial select LIES: exit 0 but the source never leaves Kotoeri.
    fake = FakeSwiftIme(
        current=KOTOERI, available=(ENGLISH_SOURCE_ID,), lie_for=(ENGLISH_SOURCE_ID,)
    )
    monkeypatch.setattr(ime, "_run_swift", fake)
    entered = False
    with pytest.raises(ImeSelectError) as excinfo, english_typing():
        entered = True
    assert not entered
    message = str(excinfo.value)
    assert ENGLISH_SOURCE_ID in message  # both ids carried
    assert KOTOERI in message
    assert fake.calls == ["read", f"select:{ENGLISH_SOURCE_ID}", "read"]  # no restore


def test_restore_failure_never_swallows_body_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSwiftIme(
        current=KOTOERI, available=(ENGLISH_SOURCE_ID,), lie_for=(KOTOERI,)
    )
    monkeypatch.setattr(ime, "_run_swift", fake)
    with pytest.raises(ImeRestoreError) as excinfo, english_typing():
        raise RuntimeError("boom")
    # The body error is replaced by the (louder) restore failure but stays
    # in the chain, never swallowed silently: ImeRestoreError <- ImeSelectError
    # (the failed restore) <- RuntimeError (the in-flight body error).
    restore_cause = excinfo.value.__cause__
    assert isinstance(restore_cause, ImeSelectError)
    assert isinstance(restore_cause.__context__, RuntimeError)


def test_already_english_drift_during_body_is_ime_restore_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSwiftIme(current=ENGLISH_SOURCE_ID)
    monkeypatch.setattr(ime, "_run_swift", fake)
    with pytest.raises(ImeRestoreError, match="drifted") as excinfo, english_typing():
        fake.current = KOTOERI  # the body (CU agent / app) switched the IME
    message = str(excinfo.value)
    assert KOTOERI in message
    assert ENGLISH_SOURCE_ID in message
