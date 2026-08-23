"""Hermetic environment for cockpit tests.

Task 8 wires ``POST /review-chat`` through the tolerant LLM factory: with
the production editorial runtime config present (task 3) AND credentials
in the developer shell, the route would otherwise issue REAL network
calls from unit tests. Every cockpit test therefore runs with the two
env-gate variables cleared — the deterministic regex path is the only
behavior under test here; live-path coverage is the gated Tier C lane.

Task 7 additionally detaches a real pipeline runner from
``create_episode``; Tier A tests must not leak processes. The
``runner_spawn_calls`` autouse fixture stubs the ``subprocess.Popen``
reference inside ``episode_ops`` (surgical: only the spawn site sees the
fake) and records every spawn's argv + kwargs so the launch tests can
assert the Popen argument contract. Tests that need the recorded calls
request that fixture by name.
"""

from __future__ import annotations

import pytest

from services.episode_cockpit import episode_ops

_GATE_VARS = ("EDITORIAL_DIRECTOR_API_KEY", "EDITORIAL_DIRECTOR_NETWORK_ENABLED")


@pytest.fixture(autouse=True)
def _hermetic_editorial_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _GATE_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def runner_spawn_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> object:
        calls.append({"argv": list(argv), **kwargs})
        return object()

    class _SubprocessShim:
        Popen = staticmethod(fake_popen)

    monkeypatch.setattr(episode_ops, "subprocess", _SubprocessShim)
    return calls
