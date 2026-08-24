"""The codex-exec editorial transport (owner decision, v44-first-publish-delta).

Tier A — NO codex calls anywhere: every test injects fakes or monkeypatches
the subprocess boundary inside ``services/cli/live_editorial_codex``. The
two editorial env vars are force-cleared (autouse) so a leaked developer
shell can never flip an outcome, and the codex gate/probe is always faked —
a logged-in machine must not make these tests live.

Proves:
(a) gate: binary missing / not logged in → typed
    ``CodexTransportGatedError`` with ZERO subprocess runs (sentinel);
(b) the login probe is consulted on EVERY runner construction (no cached
    state) and classifies honest outcomes;
(c) the real runner's argv contract: pinned codex binary + ``exec``,
    read-only sandbox, ``-m`` model, one ``-i`` pair per image,
    ``--output-last-message`` capture file, ``-`` (stdin prompt), prompt
    bytes on stdin, timeout propagated;
(d) rc!=0 → typed ``production-model-unavailable`` with the stderr head;
    timeout → ``TimeoutError`` (the seam maps it to ``model-timeout``);
    missing/empty capture file → typed ``model-bad-response``;
(e) ``v44_product_proof._try_production_gate`` per transport: openai-api
    keeps the env gate; codex-exec gates on the codex probe (blocked exit,
    operator-actionable ``codex login`` message; quiet pass on probe ok).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast, no_type_check

import pytest

from services.cli import live_editorial_codex as codex
from services.cli import v44_product_proof as product_proof
from services.cli.live_editorial_codex import (
    CodexTransportGatedError,
    make_codex_runner,
)
from services.editorial_v2.model_provider import EditorialRuntimeError

_GATE_VARS = ("EDITORIAL_DIRECTOR_API_KEY", "EDITORIAL_DIRECTOR_NETWORK_ENABLED")


@pytest.fixture(autouse=True)
def _hermetic_editorial_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _GATE_VARS:
        monkeypatch.delenv(var, raising=False)


class _SubprocessRecorder:
    """Records every subprocess.run call; delegates to a handler or refuses.

    ``handler=None`` means any subprocess run fails the test (zero-subprocess
    sentinel); a handler lets argv-contract tests fake one CompletedProcess.
    """

    def __init__(
        self, handler: Callable[..., subprocess.CompletedProcess[bytes]] | None = None
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self._handler = handler

    @no_type_check
    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        self.calls.append({"argv": list(argv), **kwargs})
        if self._handler is None:
            raise AssertionError("a codex subprocess ran in a hermetic test")
        return self._handler(argv, **kwargs)


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch, sentinel: _SubprocessRecorder
) -> None:
    monkeypatch.setattr(
        codex,
        "subprocess",
        SimpleNamespace(run=sentinel, TimeoutExpired=subprocess.TimeoutExpired),
    )


# ------------------------------------------------------------- (a) the gate


def test_gate_refuses_when_binary_missing_zero_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex.shutil, "which", lambda _name: None)
    sentinel = _SubprocessRecorder()
    _patch_subprocess(monkeypatch, sentinel)

    with pytest.raises(CodexTransportGatedError) as error:
        make_codex_runner()

    assert error.value.code == "codex-binary-missing"
    assert "codex-cli" in error.value.detail
    assert sentinel.calls == []


def test_gate_refuses_when_not_logged_in_zero_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given: binary present but the login probe says logged out; Then: typed
    gate refusal and zero subprocess of any kind (the probe itself is faked)."""

    monkeypatch.setattr(codex.shutil, "which", lambda _name: "/fake/codex")
    monkeypatch.setattr(
        codex, "_login_probe", lambda _binary: (False, "Not logged in (run codex login)")
    )
    sentinel = _SubprocessRecorder()
    _patch_subprocess(monkeypatch, sentinel)

    with pytest.raises(CodexTransportGatedError) as error:
        make_codex_runner()

    assert error.value.code == "codex-not-logged-in"
    assert "codex login" in error.value.detail
    assert sentinel.calls == []


def test_gate_probes_login_on_every_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given: consecutive constructions; Then: the probe runs each time —
    login state is read per construction, never cached."""

    monkeypatch.setattr(codex.shutil, "which", lambda _name: "/fake/codex")
    probes: list[str] = []

    def counting_probe(binary: str) -> tuple[bool, str]:
        probes.append(binary)
        return (True, "Logged in using ChatGPT")

    monkeypatch.setattr(codex, "_login_probe", counting_probe)

    assert callable(make_codex_runner())
    assert callable(make_codex_runner())
    assert probes == ["/fake/codex", "/fake/codex"]


# ------------------------------------------------- (b) the login probe itself


def _completed(returncode: int, out: str = "") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=["codex", "login", "status"],
        returncode=returncode,
        stdout=out.encode(),
        stderr=b"",
    )


def _replay(
    result: subprocess.CompletedProcess[bytes],
) -> Callable[..., subprocess.CompletedProcess[bytes]]:
    def handler(_argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return result

    return handler


def test_login_probe_classifies_honest_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    cases = [
        (_completed(0, "Logged in using ChatGPT"), True),
        (_completed(1, "Not logged in"), False),
        (_completed(0, "Not logged in on this account"), False),
    ]
    for index, (result, expected_ok) in enumerate(cases):
        _patch_subprocess(monkeypatch, _SubprocessRecorder(_replay(result)))
        ok, _detail = codex._login_probe("/fake/codex")
        assert ok is expected_ok, f"case {index}: {result.stdout}"


def test_login_probe_wraps_transport_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def hung(argv: list[str], **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(argv, 15)

    monkeypatch.setattr(
        codex, "subprocess", SimpleNamespace(run=hung, TimeoutExpired=subprocess.TimeoutExpired)
    )
    ok, detail = codex._login_probe("/fake/codex")
    assert ok is False
    assert "login probe failed" in detail


# --------------------------------------------- (c,d) the real runner contract


def _patch_runner_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stderr: str = "",
    write_capture: bool = True,
    capture_text: str = '{"ok": true}',
) -> _SubprocessRecorder:
    def handler(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        capture_index = argv.index("--output-last-message") + 1
        if write_capture:
            Path(argv[capture_index]).write_text(capture_text, encoding="utf-8")
        return subprocess.CompletedProcess(
            args=list(argv), returncode=returncode, stdout=b"", stderr=stderr.encode()
        )

    sentinel = _SubprocessRecorder(handler)
    _patch_subprocess(monkeypatch, sentinel)
    return sentinel


def test_runner_argv_contract_and_final_message_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sentinel = _patch_runner_subprocess(monkeypatch)
    runner = codex._CodexExecRunner(binary="/fake/codex")
    frame_one = tmp_path / "frame-1.png"
    frame_two = tmp_path / "frame-2.png"

    message = runner(
        "PROMPT-BYTES",
        model="gpt-5.6-sol",
        images=(frame_one, frame_two),
        timeout_s=90.0,
    )

    assert message == '{"ok": true}'
    call = sentinel.calls[0]
    argv = cast("list[str]", call["argv"])
    assert argv[:3] == ["/fake/codex", "exec", "--sandbox"]
    assert argv[3] == "read-only"
    assert argv[argv.index("-m") + 1] == "gpt-5.6-sol"
    image_args = [argv[i + 1] for i, flag in enumerate(argv) if flag == "-i"]
    assert image_args == [str(frame_one), str(frame_two)]
    assert argv[-1] == "-"  # instructions ride via stdin, never argv
    assert call["input"] == b"PROMPT-BYTES"
    assert call["timeout"] == 90.0


def test_runner_nonzero_exit_is_typed_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner_subprocess(monkeypatch, returncode=2, stderr="model not found: nope")
    runner = codex._CodexExecRunner(binary="/fake/codex")
    with pytest.raises(EditorialRuntimeError) as error:
        runner("p", model="gpt-5.6-sol", images=(), timeout_s=10.0)
    assert error.value.code == "production-model-unavailable"
    assert "exited 2" in error.value.detail
    assert "model not found" in error.value.detail


def test_runner_timeout_is_timeout_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def hung(argv: list[str], **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(argv, 120)

    monkeypatch.setattr(
        codex, "subprocess", SimpleNamespace(run=hung, TimeoutExpired=subprocess.TimeoutExpired)
    )
    runner = codex._CodexExecRunner(binary="/fake/codex")
    with pytest.raises(TimeoutError) as error:
        runner("p", model="gpt-5.6-sol", images=(), timeout_s=120.0)
    assert "killed" in str(error.value)


def test_runner_missing_or_empty_capture_is_typed_bad_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner_subprocess(monkeypatch, write_capture=False)
    runner = codex._CodexExecRunner(binary="/fake/codex")
    with pytest.raises(EditorialRuntimeError) as error:
        runner("p", model="gpt-5.6-sol", images=(), timeout_s=10.0)
    assert error.value.code == "model-bad-response"
    assert "no --output-last-message file" in error.value.detail

    _patch_runner_subprocess(monkeypatch, capture_text="   ")
    with pytest.raises(EditorialRuntimeError) as error:
        runner("p", model="gpt-5.6-sol", images=(), timeout_s=10.0)
    assert error.value.code == "model-bad-response"
    assert "empty" in error.value.detail


# --------------------------------------- (e) the product-proof gate per transport


def test_product_proof_gate_openai_without_env_blocks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        product_proof._try_production_gate("openai-api")
    assert error.value.code == 1
    assert "production-model-unavailable" in capsys.readouterr().err


def test_product_proof_gate_openai_with_env_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EDITORIAL_DIRECTOR_API_KEY", "sk-test")
    monkeypatch.setenv("EDITORIAL_DIRECTOR_NETWORK_ENABLED", "1")
    product_proof._try_production_gate("openai-api")


def test_product_proof_gate_codex_gated_blocks_with_actionable_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def gated() -> object:
        raise CodexTransportGatedError(
            "codex-not-logged-in", "Not logged in — run `codex login`"
        )

    monkeypatch.setattr("services.cli.live_editorial_codex.make_codex_runner", gated)
    with pytest.raises(SystemExit) as error:
        product_proof._try_production_gate("codex-exec")
    assert error.value.code == 1
    err = capsys.readouterr().err
    assert "production-model-unavailable" in err
    assert "codex login" in err


def test_product_proof_gate_codex_probe_ok_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("services.cli.live_editorial_codex.make_codex_runner", object)
    product_proof._try_production_gate("codex-exec")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__]))
