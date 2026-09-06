"""ascii_entry: numeric typing runs inside the IME pair; non-ASCII refused.

Covers safeguard C (suda order 2026-09-06): the send callback executes
inside ``english_typing`` (switch to ASCII before, restore after — the
fake swift runner records the order), the journal record carries the
live-read original source, a body exception still restores, and
non-ASCII text is refused BEFORE any switch. No real swift, IME, or GUI
is ever contacted.
"""

from __future__ import annotations

import pytest

from services.cu_client import ascii_entry, ime
from services.cu_client.ascii_entry import type_ascii

KOTOERI = "com.apple.inputmethod.Kotoeri.Romaji"


class FakeSwiftIme:
    """In-memory input-source state + call recording (same seam as test_ime)."""

    def __init__(self, current: str) -> None:
        self.current = current
        self.calls: list[str] = []

    def __call__(self, source: str, args: list[str]) -> tuple[int, str]:
        if source == ime._INPUT_SOURCE_SWIFT:
            self.calls.append("read")
            return 0, f"id: {self.current} | lang: x | type: y | local: Z"
        target = args[0]
        self.calls.append(f"select:{target}")
        self.current = target
        return 0, f"selected {target}"


def test_send_runs_inside_the_ascii_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSwiftIme(KOTOERI)
    monkeypatch.setattr(ime, "_run_swift", fake)
    sent: list[str] = []
    record = type_ascii("-6", sent.append)
    assert sent == ["-6"]
    assert record["original_source"] == KOTOERI
    assert record["layout"] == "ascii"
    assert fake.current == KOTOERI  # restored afterwards
    assert fake.calls[0] == "read"  # original read live first
    assert any(c == f"select:{ime.ENGLISH_SOURCE_ID}" for c in fake.calls)


def test_body_exception_still_restores(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSwiftIme(KOTOERI)
    monkeypatch.setattr(ime, "_run_swift", fake)

    def boom(_text: str) -> None:
        raise RuntimeError("synthetic send failure")

    with pytest.raises(RuntimeError, match="synthetic send failure"):
        type_ascii("0.05", boom)
    assert fake.current == KOTOERI


def test_non_ascii_refused_before_any_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSwiftIme(KOTOERI)
    monkeypatch.setattr(ime, "_run_swift", fake)
    with pytest.raises(ValueError, match="non-ASCII"):
        type_ascii("二〇", lambda _t: None)
    assert fake.calls == []  # no switch attempted
    assert ascii_entry.__all__ == ["type_ascii"]
