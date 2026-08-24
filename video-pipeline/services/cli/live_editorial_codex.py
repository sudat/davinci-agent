"""The env-gated LIVE editorial transport over ``codex exec`` (owner decision).

Lives in the CLI layer BY DESIGN, verbatim the ``services/cli/
live_editorial_v2.py`` discipline: ``services/**`` is a network-free
boundary, so the one bounded subprocess call against the codex CLI is
issued from here. The transport is DENIED BY DEFAULT and never fabricates:
:func:`make_codex_runner` verifies the codex binary AND the logged-in
state BEFORE any ``codex exec`` can run — a gate failure is a typed
:class:`CodexTransportGatedError` raised at construction time (zero
``codex exec`` by construction). Login state is probed on EVERY
construction, never cached.

This is a PURE LLM call, not an agent session: the subprocess runs with a
read-only sandbox (the model can neither write to the repo nor mutate the
pipeline state), the prompt rides via stdin, and the final message is
captured through ``--output-last-message`` — a file codex itself writes,
never scraped from interleaved logs. A timeout kills the subprocess and
surfaces as the seam-level ``model-timeout``; a non-zero exit is a typed
``production-model-unavailable`` carrying the stderr head.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Final

from services.editorial_v2.model_provider import CodexRunner, EditorialRuntimeError

CODEX_BINARY: Final = "codex"
#: Mirrors the openai transport budget (task 3): a hung model call fails
#: typed at 120 s — codex exec can hang on network; the child is killed.
REQUEST_TIMEOUT_SECONDS: Final = 120.0
_LOGIN_PROBE_TIMEOUT_SECONDS: Final = 15.0
_ERROR_OUTPUT_LIMIT: Final = 300


class CodexTransportGatedError(Exception):
    """Env gate unmet — raised BEFORE any ``codex exec``; never a fabricated response."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _login_probe(binary: str) -> tuple[bool, str]:
    """Cheap ``codex login status`` probe: (ok, combined output head)."""

    try:
        completed = subprocess.run(
            [binary, "login", "status"],
            capture_output=True,
            text=True,
            timeout=_LOGIN_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        return False, f"login probe failed: {error}"
    combined = f"{completed.stdout.strip()} {completed.stderr.strip()}".strip()
    if completed.returncode == 0 and "not logged in" not in combined.lower():
        return True, combined
    return False, combined[:_ERROR_OUTPUT_LIMIT]


def make_codex_runner(binary: str = CODEX_BINARY) -> CodexRunner:
    """Construct the codex-exec runner, or refuse at the gate BEFORE any exec.

    The gate (binary present via PATH lookup, then a live login probe) runs
    on EVERY construction — login state is never cached across runners.
    """

    resolved = shutil.which(binary)
    if resolved is None:
        raise CodexTransportGatedError(
            "codex-binary-missing",
            f"the codex CLI is not on PATH as {binary!r}; install codex-cli, or "
            "switch the editorial runtime transport to openai-api — refusing "
            "before any codex exec call",
        )
    ok, detail = _login_probe(resolved)
    if not ok:
        raise CodexTransportGatedError(
            "codex-not-logged-in",
            f"the codex CLI is not logged in ({detail or 'no status output'}); "
            "run `codex login` — refusing before any codex exec call",
        )
    return _CodexExecRunner(binary=resolved)


class _CodexExecRunner:
    """One bounded ``codex exec`` call → the assistant's final message text."""

    __slots__ = ("_binary",)

    def __init__(self, binary: str) -> None:
        self._binary = binary

    def __call__(
        self, prompt: str, *, model: str, images: tuple[Path, ...], timeout_s: float
    ) -> str:
        image_flags: list[str] = []
        for image in images:
            image_flags.extend(("-i", str(image)))
        with tempfile.TemporaryDirectory(prefix="codex-last-message-") as scratch:
            last_message = Path(scratch) / "last-message.txt"
            argv = [
                self._binary,
                "exec",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--color",
                "never",
                "-m",
                model,
                *image_flags,
                "--output-last-message",
                str(last_message),
                "-",  # instructions ride via stdin, never argv (argv is ps-visible)
            ]
            try:
                completed = subprocess.run(
                    argv,
                    input=prompt.encode("utf-8"),
                    capture_output=True,
                    timeout=timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise TimeoutError(
                    f"codex exec exceeded {timeout_s:.0f}s and was killed"
                ) from error
            except OSError as error:
                raise EditorialRuntimeError(
                    "production-model-unavailable",
                    f"codex exec could not be launched ({self._binary}): {error}",
                ) from error
            if completed.returncode != 0:
                stderr = completed.stderr.decode("utf-8", "replace").strip()
                raise EditorialRuntimeError(
                    "production-model-unavailable",
                    f"codex exec exited {completed.returncode} for model {model!r}: "
                    f"{stderr[:_ERROR_OUTPUT_LIMIT] or 'no stderr output'}",
                )
            if not last_message.is_file():
                raise EditorialRuntimeError(
                    "model-bad-response",
                    "codex exec exited 0 but wrote no --output-last-message file",
                )
            message = last_message.read_text(encoding="utf-8")
        if not message.strip():
            raise EditorialRuntimeError(
                "model-bad-response",
                "codex exec final message is empty",
            )
        return message


__all__ = [
    "CODEX_BINARY",
    "REQUEST_TIMEOUT_SECONDS",
    "CodexTransportGatedError",
    "make_codex_runner",
]
