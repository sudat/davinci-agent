"""Hermetic environment for cockpit tests.

Task 8 wires ``POST /review-chat`` through the tolerant LLM factory: with
the production editorial runtime config present (task 3) the route would
otherwise issue REAL model calls from unit tests — on the openai-api
transport via developer-shell credentials, and on the DEFAULT codex-exec
transport via the locally logged-in codex CLI (V44-1). Every cockpit test
therefore runs with the openai env-gate variables cleared AND the route's
factory stubbed to None (regex-only); live-path coverage is the gated
Tier C lane. Tests that WANT a fake LLM monkeypatch
``services.episode_cockpit.api.build_review_llm_call`` on top of this
stub (stacked monkeypatch restores compose correctly).

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

from services.episode_cockpit import api as cockpit_api
from services.episode_cockpit import api_consultation, episode_ops

_GATE_VARS = ("EDITORIAL_DIRECTOR_API_KEY", "EDITORIAL_DIRECTOR_NETWORK_ENABLED")


@pytest.fixture(autouse=True)
def _hermetic_editorial_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _GATE_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cockpit_api, "build_review_llm_call", lambda: None)
    # The consultation factory follows the same review-interpreter pattern;
    # without this stub a message-route test would probe the locally
    # logged-in codex CLI (production_model runtime is shipped ON).
    monkeypatch.setattr(api_consultation, "build_consultation_llm_call", lambda: None)


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
