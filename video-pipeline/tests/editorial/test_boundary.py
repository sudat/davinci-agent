"""Structural boundary guards for the Editorial Director (Todo 39).

AST-level proof that ``services/editorial`` holds no forbidden authority:
no job-runner / review-command / artifact-store import, no duckdb/shell/
socket/write handle anywhere, the media-query surface used is exactly the
frozen seven-method allowlist, and the frozen proposal schema structurally
cannot carry decision/commit/Resolve fields.
"""

from __future__ import annotations

import ast
from pathlib import Path

from services.contracts.editorial_model import EditorialSelectionProposal
from services.editorial.director import EditorialDirector
from services.editorial.transport import ReplayTransport
from services.media_query.api import MediaQueryApi
from services.media_query.api_models import (
    FROZEN_METHOD_ALLOWLIST,
    FROZEN_PUBLIC_SURFACE,
    LIFECYCLE_SURFACE,
)

EDITORIAL_PACKAGE = Path("services/editorial")
FORBIDDEN_IMPORT_ROOTS = (
    "services.job_runner",
    "services.review_command",
    "services.artifact_store",
    "services.artifact_registry",
    "services.build",
    "services.resolve_bridge",
)
FORBIDDEN_MODULE_NAMES = {
    "duckdb",
    "subprocess",
    "socket",
    "http",
    "urllib",
    "requests",
    "shutil",
    "os.system",
}
FORBIDDEN_CALL_NAMES = {"system", "popen", "exec", "eval", "run"}


def _imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.add(node.module)
    return found


def _editorial_sources() -> dict[Path, str]:
    return {
        path: path.read_text(encoding="utf-8")
        for path in sorted(EDITORIAL_PACKAGE.glob("*.py"))
    }


def test_no_forbidden_authority_imports() -> None:
    for path, source in _editorial_sources().items():
        imports = _imports(ast.parse(source, filename=str(path)))
        for root in FORBIDDEN_IMPORT_ROOTS:
            assert not any(
                name == root or name.startswith(root + ".") for name in imports
            ), f"{path} imports forbidden authority module {root}"


def test_no_duckdb_shell_or_network_handles() -> None:
    for path, source in _editorial_sources().items():
        imports = _imports(ast.parse(source, filename=str(path)))
        for name in imports:
            root = name.split(".")[0]
            assert root not in FORBIDDEN_MODULE_NAMES, f"{path} imports {name}"
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert (
                    node.func.id not in FORBIDDEN_CALL_NAMES
                ), f"{path} calls forbidden builtin {node.func.id}"


def test_no_file_write_surface() -> None:
    for path, source in _editorial_sources().items():
        assert "write_text" not in source, f"{path} writes files"
        assert "write_bytes" not in source, f"{path} writes files"
        assert "atomic_write" not in source, f"{path} writes files"
        assert "mkdir" not in source, f"{path} mutates the filesystem"


def test_director_instance_holds_no_runtime_handles() -> None:
    director = EditorialDirector(transport=ReplayTransport({}))
    for attribute in dir(director):
        assert attribute not in {
            "connection", "execute", "cursor", "shell", "socket", "subprocess"
        }, f"director exposes forbidden handle {attribute}"
    assert not hasattr(director, "run_gate")
    assert not hasattr(director, "commit")


def test_media_query_surface_is_exactly_the_frozen_seven_methods() -> None:
    public = {
        name for name in dir(MediaQueryApi) if not name.startswith("_")
    } - LIFECYCLE_SURFACE
    assert public == FROZEN_METHOD_ALLOWLIST
    assert FROZEN_PUBLIC_SURFACE == FROZEN_METHOD_ALLOWLIST | LIFECYCLE_SURFACE
    assert len(FROZEN_METHOD_ALLOWLIST) == 7


def test_evidence_module_calls_only_allowlisted_api_methods() -> None:
    source = (EDITORIAL_PACKAGE / "evidence.py").read_text(encoding="utf-8")
    called = {
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.value is not None
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "api"
    }
    assert called <= FROZEN_METHOD_ALLOWLIST, called - FROZEN_METHOD_ALLOWLIST
    assert called == {"episode_summary", "search_transcripts", "silence_ranges"}


def test_proposal_schema_cannot_carry_commit_or_resolve_fields() -> None:
    forbidden = {
        "commit", "committed", "decision", "approved", "approval", "retry",
        "track_index", "record_frame", "resolve_track", "timeline_item",
    }
    assert not forbidden & set(EditorialSelectionProposal.model_fields)
    assert EditorialSelectionProposal.model_fields["actor_intent"].annotation


def test_proposal_actor_intent_is_model_only() -> None:
    annotation = EditorialSelectionProposal.model_fields["actor_intent"].annotation
    assert annotation is not None
    assert "model" in str(annotation)
    assert "human" not in str(annotation).lower()
